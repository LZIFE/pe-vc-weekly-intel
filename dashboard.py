#!/usr/bin/env python3
"""
Reasonix 全系统 API 用量 & 费用仪表盘
读取 ~/.reasonix/projects/*/sessions/*.telemetry.json + config.toml 定价
展示所有项目、所有会话的 token 消耗和费用估算。
"""

import json
import os
import re
import sys
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime, timezone

PORT = int(os.environ.get("DASHBOARD_PORT", "8899"))
REASONIX_HOME = Path.home() / ".reasonix"
CONFIG_PATH = REASONIX_HOME / "config.toml"
PROJECTS_DIR = REASONIX_HOME / "projects"

# ── Load pricing from Reasonix config.toml ───────────────────────────

PRICING_CACHE = None  # type: dict[str, dict] | None


def load_pricing() -> dict[str, dict]:
    """Parse reasonix config.toml using tomli and return per-model pricing dict."""
    global PRICING_CACHE
    if PRICING_CACHE is not None:
        return PRICING_CACHE

    pricing: dict[str, dict] = {}
    if not CONFIG_PATH.is_file():
        pricing["default"] = {"input": 1e-6, "output": 2e-6, "cache_hit": 2e-8, "currency": "¥"}
        PRICING_CACHE = pricing
        return pricing

    try:
        import tomli
        with open(CONFIG_PATH, "rb") as f:
            config = tomli.load(f)
    except Exception:
        # Fallback defaults
        pricing["default"] = {"input": 1e-6, "output": 2e-6, "cache_hit": 2e-8, "currency": "¥"}
        PRICING_CACHE = pricing
        return pricing

    # Process all [[providers]] sections
    for provider in config.get("providers", []):
        provider_name = provider.get("name", "")
        fallback = provider.get("price", {})
        per_model = provider.get("prices", {})

        if per_model:
            # Per-model pricing takes precedence
            for model_name, model_price in per_model.items():
                if isinstance(model_price, dict):
                    pricing[model_name] = {
                        "input": float(model_price.get("input", fallback.get("input", 1.0))) * 1e-6,
                        "output": float(model_price.get("output", fallback.get("output", 2.0))) * 1e-6,
                        "cache_hit": float(model_price.get("cache_hit", fallback.get("cache_hit", 0.02))) * 1e-6,
                        "currency": model_price.get("currency", fallback.get("currency", "¥")),
                        "provider": provider_name,
                    }
        elif fallback:
            # Provider-level fallback pricing
            pricing[provider_name] = {
                "input": float(fallback.get("input", 1.0)) * 1e-6,
                "output": float(fallback.get("output", 2.0)) * 1e-6,
                "cache_hit": float(fallback.get("cache_hit", 0.02)) * 1e-6,
                "currency": fallback.get("currency", "¥"),
                "provider": provider_name,
            }

    if not pricing:
        pricing["default"] = {"input": 1e-6, "output": 2e-6, "cache_hit": 2e-8, "currency": "¥"}

    PRICING_CACHE = pricing
    return pricing


def get_pricing_for_model(model: str) -> dict:
    """Get pricing dict for a specific model name."""
    pricing = load_pricing()
    # Try exact match first
    for key in pricing:
        if key == model:
            return pricing[key]
    # Try substring match
    for key in pricing:
        if key in model or model in key:
            return pricing[key]
    # Fallback
    return pricing.get("default", pricing.get("deepseek", {"input": 1e-6, "output": 2e-6, "cache_hit": 2e-8, "currency": "¥"}))


# ── Scan all telemetry files ─────────────────────────────────────────

def project_name(dirname: str) -> str:
    """Convert directory name like '-Users-z1-Documents-New project-private_equity_fund_automation' to readable name."""
    name = dirname.strip("-")
    parts = name.split("-")
    skip_patterns = {"Users", "z1", "Documents", "Library", "Application Support", "reasonix"}
    meaningful = [p for p in parts if p not in skip_patterns and not p.startswith("reasonix")]
    if meaningful:
        return "/".join(meaningful)
    return name


def scan_telemetry() -> list[dict]:
    """Read all telemetry files and return list of session records."""
    sessions: list[dict] = []
    if not PROJECTS_DIR.is_dir():
        return sessions

    for proj_dir in sorted(PROJECTS_DIR.iterdir()):
        if not proj_dir.is_dir():
            continue
        proj = project_name(proj_dir.name)
        for tf in sorted(proj_dir.glob("sessions/*.telemetry.json")):
            # Skip recovery files (they contain overlapping data)
            if "-recovery-" in tf.stem:
                continue
            try:
                data = json.loads(tf.read_text())
                u = data.get("usage", {})
                if not u:
                    continue
                ts = _extract_timestamp(tf, data)
                model = _extract_model(tf, data)
                session_id = tf.stem.replace(".jsonl.telemetry", "")

                sources = u.get("sources", {})
                executor = sources.get("executor", {})
                subagent = sources.get("subagent", {})
                compaction = sources.get("compaction", {})

                prompt = u.get("promptTokens", 0)
                completion = u.get("completionTokens", 0)
                cache_hit = u.get("cacheHitTokens", 0)
                cache_miss = u.get("cacheMissTokens", 0)

                # Compute cost with cache accounting
                ppm = get_pricing_for_model(model)
                cost = _compute_cost(prompt, completion, cache_hit, cache_miss, ppm)

                record = {
                    "project": proj,
                    "session_id": session_id,
                    "timestamp": ts,
                    "model": model,
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": u.get("totalTokens", prompt + completion),
                    "reasoning_tokens": u.get("reasoningTokens", 0),
                    "cache_hit_tokens": cache_hit,
                    "cache_miss_tokens": cache_miss,
                    "request_count": u.get("requestCount", 0),
                    "elapsed_ms": u.get("elapsedMs", 0),
                    "cost_input": cost["input"],
                    "cost_cache": cost["cache"],
                    "cost_output": cost["output"],
                    "cost_total": cost["total"],
                    "cache_rate": _cache_rate(cache_hit, prompt),
                    "pricing": ppm,
                    "sources": {
                        "executor": {
                            "prompt": executor.get("promptTokens", 0),
                            "completion": executor.get("completionTokens", 0),
                            "requests": executor.get("requestCount", 0),
                        },
                        "subagent": {
                            "prompt": subagent.get("promptTokens", 0),
                            "completion": subagent.get("completionTokens", 0),
                            "requests": subagent.get("requestCount", 0),
                        } if subagent else None,
                        "compaction": {
                            "prompt": compaction.get("promptTokens", 0),
                            "completion": compaction.get("completionTokens", 0),
                            "requests": compaction.get("requestCount", 0),
                        } if compaction else None,
                    },
                }
                sessions.append(record)
            except (json.JSONDecodeError, OSError, KeyError) as e:
                continue
    return sessions


def _cache_rate(cache_hit: int, cache_miss: int) -> float:
    """Cache hit rate as percentage of total cache operations (hit / (hit + miss))."""
    total = cache_hit + cache_miss
    if total <= 0:
        return 0.0
    return round(cache_hit / total * 100, 1)


def _compute_cost(prompt: int, completion: int, cache_hit: int, cache_miss: int, p: dict) -> dict:
    """
    Compute cost based on DeepSeek pricing model:
      - cache_hit/(hit+miss) ratio applied to prompt tokens determines cached portion
      - cached tokens billed at discounted cache_hit rate
      - non-cached tokens billed at input rate
      - output tokens billed at output rate
    """
    input_rate = p.get("input", 1e-6)
    output_rate = p.get("output", 2e-6)
    cache_rate = p.get("cache_hit", 2e-8)

    # Cache ratio from actual hit/miss counts
    cache_ratio = _cache_rate(cache_hit, cache_miss) / 100.0
    estimated_cached = round(prompt * cache_ratio)
    estimated_noncached = prompt - estimated_cached

    cost_input = estimated_noncached * input_rate
    cost_cache = estimated_cached * cache_rate
    cost_output = completion * output_rate
    cost_total = cost_input + cost_cache + cost_output

    return {
        "input": cost_input,
        "cache": cost_cache,
        "output": cost_output,
        "total": cost_total,
    }


def _extract_timestamp(tf: Path, data: dict) -> str:
    m = re.search(r"(\d{8})-(\d{6})", tf.stem)
    if m:
        try:
            dt = datetime.strptime(f"{m.group(1)}-{m.group(2)}", "%Y%m%d-%H%M%S")
            return dt.isoformat()
        except ValueError:
            pass
    files = data.get("readFiles", [])
    if files and "time" in files[0]:
        try:
            dt = datetime.fromtimestamp(files[0]["time"] / 1000, tz=timezone.utc)
            return dt.isoformat()
        except (OSError, ValueError):
            pass
    return ""


def _extract_model(tf: Path, data: dict) -> str:
    m = re.search(r"(deepseek-[\w-]+)", tf.stem)
    if m:
        return m.group(1)
    return "unknown"


# ── Aggregation ──────────────────────────────────────────────────────

def compute_summary(sessions: list[dict]) -> dict:
    """Aggregate all sessions into summary."""
    total = {
        "total_sessions": len(sessions),
        "total_calls": sum(s.get("request_count", 0) for s in sessions),
        "total_prompt": sum(s.get("prompt_tokens", 0) for s in sessions),
        "total_completion": sum(s.get("completion_tokens", 0) for s in sessions),
        "total_tokens": sum(s.get("total_tokens", 0) for s in sessions),
        "total_cache_hit": sum(s.get("cache_hit_tokens", 0) for s in sessions),
        "total_cache_rate": 0.0,
        "total_cost": sum(s.get("cost_total", 0) for s in sessions),
        "cost_breakdown": {
            "input": sum(s.get("cost_input", 0) for s in sessions),
            "cache": sum(s.get("cost_cache", 0) for s in sessions),
            "output": sum(s.get("cost_output", 0) for s in sessions),
        },
        "projects": defaultdict(lambda: {"sessions": 0, "calls": 0, "prompt": 0, "completion": 0, "cache_hit": 0, "cache_miss": 0, "cost": 0.0, "cost_input": 0.0, "cost_cache": 0.0, "cost_output": 0.0}),
        "models": defaultdict(lambda: {"sessions": 0, "calls": 0, "prompt": 0, "completion": 0, "cache_hit": 0, "cost": 0.0}),
        "sessions": [],
    }

    total_prompt_all = sum(s.get("prompt_tokens", 0) for s in sessions)
    total_cache_all = sum(s.get("cache_hit_tokens", 0) for s in sessions)
    total_miss_all = sum(s.get("cache_miss_tokens", 0) for s in sessions)
    total["total_cache_rate"] = _cache_rate(total_cache_all, total_miss_all)

    for s in sessions:
        proj = s.get("project", "unknown")
        model = s.get("model", "unknown")
        p = total["projects"][proj]
        p["sessions"] += 1
        p["calls"] += s.get("request_count", 0)
        p["prompt"] += s.get("prompt_tokens", 0)
        p["completion"] += s.get("completion_tokens", 0)
        p["cache_hit"] += s.get("cache_hit_tokens", 0)
        p["cache_miss"] += s.get("cache_miss_tokens", 0)
        p["cost"] += s.get("cost_total", 0)
        p["cost_input"] += s.get("cost_input", 0)
        p["cost_cache"] += s.get("cost_cache", 0)
        p["cost_output"] += s.get("cost_output", 0)

        m = total["models"][model]
        m["sessions"] += 1
        m["calls"] += s.get("request_count", 0)
        m["prompt"] += s.get("prompt_tokens", 0)
        m["completion"] += s.get("completion_tokens", 0)
        m["cache_hit"] += s.get("cache_hit_tokens", 0)
        m["cost"] += s.get("cost_total", 0)

        total["sessions"].append({
            "project": proj,
            "model": model,
            "timestamp": s.get("timestamp", ""),
            "calls": s.get("request_count", 0),
            "prompt": s.get("prompt_tokens", 0),
            "completion": s.get("completion_tokens", 0),
            "cache_hit": s.get("cache_hit_tokens", 0),
            "cache_miss": s.get("cache_miss_tokens", 0),
            "cache_rate": s.get("cache_rate", 0.0),
            "cost": round(s.get("cost_total", 0), 4),
            "cost_input": round(s.get("cost_input", 0), 4),
            "cost_cache": round(s.get("cost_cache", 0), 4),
            "cost_output": round(s.get("cost_output", 0), 4),
        })

    total["sessions"].sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    total["total_cost"] = round(total["total_cost"], 4)
    total["cost_breakdown"]["input"] = round(total["cost_breakdown"]["input"], 4)
    total["cost_breakdown"]["cache"] = round(total["cost_breakdown"]["cache"], 4)
    total["cost_breakdown"]["output"] = round(total["cost_breakdown"]["output"], 4)

    for p in total["projects"].values():
        p["cost"] = round(p["cost"], 4)
        p["cost_input"] = round(p["cost_input"], 4)
        p["cost_cache"] = round(p["cost_cache"], 4)
        p["cost_output"] = round(p["cost_output"], 4)
        p["cache_rate"] = _cache_rate(p["cache_hit"], p["cache_miss"])
    for m in total["models"].values():
        m["cost"] = round(m["cost"], 4)

    total["projects"] = dict(sorted(total["projects"].items()))
    total["models"] = dict(sorted(total["models"].items()))

    return total


# ── HTML Dashboard ──────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="zh-Hans">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reasonix 全系统费用仪表盘</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Noto Sans SC',sans-serif;background:#f5f5f7;color:#1d1d1f;padding:24px;max-width:1200px;margin:auto}
h1{font-size:24px;font-weight:600;margin-bottom:4px;letter-spacing:-0.3px}
h2{font-size:16px;font-weight:500;color:#86868b;margin-bottom:24px}
.subtitle{font-size:13px;color:#86868b;margin-bottom:24px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:24px}
.card{background:#fff;border-radius:12px;padding:20px;box-shadow:0 1px 3px rgba(0,0,0,0.06);transition:box-shadow 0.2s}
.card:hover{box-shadow:0 4px 12px rgba(0,0,0,0.1)}
.card .label{font-size:12px;font-weight:500;text-transform:uppercase;color:#86868b;margin-bottom:6px;letter-spacing:0.5px}
.card .value{font-size:28px;font-weight:600;line-height:1.2}
.card .sub{font-size:13px;color:#86868b;margin-top:4px}
.cost{color:#1a5276}
.tokens{color:#2d6a4f}
.cache{color:#7b2d8e}
.sessions{color:#8b4513}
table{width:100%;border-collapse:collapse;margin-top:16px;font-size:13px}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid #e8e8ec}
th{font-weight:500;color:#86868b;text-transform:uppercase;font-size:11px;letter-spacing:0.5px}
td:last-child,th:last-child{text-align:right}
tr:last-child td{border-bottom:none}
.section{margin-top:32px}
.section h3{font-size:14px;font-weight:600;margin-bottom:12px;color:#1d1d1f}
.footer{text-align:center;font-size:12px;color:#aeaeb2;margin-top:32px;padding-top:16px;border-top:1px solid #e8e8ec}
.refresh{display:inline-block;margin-top:8px;font-size:12px;color:#1a5276;text-decoration:none;cursor:pointer}
.progress-bar{height:6px;border-radius:3px;background:#e8e8ec;margin-top:4px;overflow:hidden}
.progress-fill{height:100%;border-radius:3px;background:#7b2d8e;transition:width 0.3s}
</style>
</head>
<body>
<h1>Reasonix 全系统费用监控</h1>
<h2>所有项目 · 所有会话 · API 用量 &amp; 成本估算</h2>
<div id="root"><div class="grid" id="summary"></div>
<div class="section"><h3>各项目明细</h3><div id="projects"></div></div>
<div class="section"><h3>各模型明细</h3><div id="models"></div></div>
<div class="section"><h3>会话历史（最近 20 条）</h3><div id="sessions"></div></div></div>
<div class="footer">
  <span id="last-updated">加载中…</span>
  <a class="refresh" onclick="location.reload()">↻ 刷新</a>
</div>
<script>
async function load(){const r=await fetch('/api/summary');const d=await r.json();render(d);document.getElementById('last-updated').textContent='最后更新: '+new Date().toLocaleString('zh-CN')}
function fmt(n){return n.toLocaleString('zh-CN',{maximumFractionDigits:2})}
function fmtCost(n){return '¥'+n.toFixed(2)}
function render(d){const s=document.getElementById('summary');s.innerHTML=''
const cards=[
  {label:'会话总数',value:d.total_sessions,sub:'',cls:'sessions'},
  {label:'API 调用次数',value:fmt(d.total_calls),sub:'',cls:''},
  {label:'总 Token 消耗',value:fmt(d.total_tokens),sub:'输入 '+fmt(d.total_prompt)+' / 输出 '+fmt(d.total_completion),cls:'tokens'},
  {label:'缓存命中率',value:d.total_cache_rate+'%',sub:fmt(d.total_cache_hit)+' tokens 命中',cls:'cache'},
  {label:'总费用',value:fmtCost(d.total_cost),sub:'输入 ¥'+d.cost_breakdown.input.toFixed(2)+' / 缓存 ¥'+d.cost_breakdown.cache.toFixed(2)+' / 输出 ¥'+d.cost_breakdown.output.toFixed(2),cls:'cost'},
]
cards.forEach(c=>{const div=document.createElement('div');div.className='card';if(c.cls)div.classList.add(c.cls)
div.innerHTML='<div class="label">'+c.label+'</div><div class="value">'+c.value+'</div>'+(c.sub?'<div class="sub">'+c.sub+'</div>':'')
s.appendChild(div)})

// Projects table
const pdiv=document.getElementById('projects')
let phtml='<table><thead><tr><th>项目</th><th>会话</th><th>调用</th><th>输入 Tokens</th><th>输出 Tokens</th><th>缓存命中</th><th>缓存率</th><th>费用</th><th>输入费</th><th>缓存费</th><th>输出费</th></tr></thead><tbody>'
for(const[name,data]of Object.entries(d.projects)){
  const cacheRate=data.cache_rate||0
  phtml+='<tr><td><strong>'+name+'</strong></td><td>'+data.sessions+'</td><td>'+fmt(data.calls)+'</td><td>'+fmt(data.prompt)+'</td><td>'+fmt(data.completion)+'</td><td>'+fmt(data.cache_hit)+'</td><td>'+cacheRate+'%</td><td>'+fmtCost(data.cost)+'</td><td>'+fmtCost(data.cost_input)+'</td><td>'+fmtCost(data.cost_cache)+'</td><td>'+fmtCost(data.cost_output)+'</td></tr>'}
phtml+='</tbody></table>';pdiv.innerHTML=phtml

// Models table
const mdiv=document.getElementById('models')
let mhtml='<table><thead><tr><th>模型</th><th>会话</th><th>调用</th><th>输入 Tokens</th><th>输出 Tokens</th><th>缓存命中</th><th>费用</th></tr></thead><tbody>'
for(const[name,data]of Object.entries(d.models)){mhtml+='<tr><td><strong>'+name+'</strong></td><td>'+data.sessions+'</td><td>'+fmt(data.calls)+'</td><td>'+fmt(data.prompt)+'</td><td>'+fmt(data.completion)+'</td><td>'+fmt(data.cache_hit)+'</td><td>'+fmtCost(data.cost)+'</td></tr>'}
mhtml+='</tbody></table>';mdiv.innerHTML=mhtml

// Sessions table
const sdiv=document.getElementById('sessions')
let shtml='<table><thead><tr><th>时间</th><th>项目</th><th>模型</th><th>调用</th><th>输入</th><th>输出</th><th>缓存命中</th><th>缓存率</th><th>费用</th></tr></thead><tbody>'
const recent=d.sessions.slice(0,20)
for(const s of recent){
  const barWidth=Math.min(s.cache_rate,100)
  shtml+='<tr><td>'+(s.timestamp.slice(0,19)||'')+'</td><td>'+s.project+'</td><td>'+s.model+'</td><td>'+s.calls+'</td><td>'+fmt(s.prompt)+'</td><td>'+fmt(s.completion)+'</td><td>'+fmt(s.cache_hit)+'</td><td>'+s.cache_rate+'%<div class="progress-bar"><div class="progress-fill" style="width:'+barWidth+'%"></div></div></td><td>'+fmtCost(s.cost)+'</td></tr>'}
shtml+='</tbody></table>';sdiv.innerHTML=shtml}
load()
</script>
</body>
</html>"""


# ── HTTP Server ──────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/summary":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            sessions = scan_telemetry()
            summary = compute_summary(sessions)
            self.wfile.write(json.dumps(summary, ensure_ascii=False).encode("utf-8"))
        elif self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass


def main():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"📊 Reasonix 全系统费用仪表盘: http://127.0.0.1:{PORT}")
    print(f"   Telemetry 目录: {PROJECTS_DIR}")
    print(f"   定价配置: {CONFIG_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()