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
import sys, json, re, os
from collections import defaultdict
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime, timezone, timedelta

PORT = int(os.environ.get("DASHBOARD_PORT", "8899"))
REASONIX_HOME = Path.home() / ".reasonix"
CONFIG_PATH = REASONIX_HOME / "config.toml"
PROJECTS_DIR = REASONIX_HOME / "projects"

# ── Load pricing from Reasonix config ────────────────────────────────

def load_pricing(config_path: Path = CONFIG_PATH) -> dict:
    """Parse reasonix config.toml and return per-model pricing dict."""
    pricing: dict[str, dict] = {}
    if not config_path.is_file():
        return pricing
    try:
        raw = config_path.read_text()
        # Simple TOML parser for the [[providers]] sections
        in_providers = False
        current_provider = ""
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("[[providers]]"):
                in_providers = True
                current_provider = ""
                continue
            if in_providers:
                m = re.match(r'^\s*name\s*=\s*"([^"]+)"', line)
                if m:
                    current_provider = m.group(1)
                # Check for per-model prices: [providers.prices.deepseek-v4-flash]
                pm = re.match(r'^\s*\[providers\.prices\.([^\]]+)\]', line)
                if not pm:
                    pm = re.match(r'^\s*\[prices\.([^\]]+)\]', line)
                m2 = re.match(r'prices\s*=\s*\{([^}]+)\}', line)
                if m2:
                    # Parse key-value pairs
                    price_str = m2.group(1)
                    pairs = {}
                    for kv in price_str.split(","):
                        kv = kv.strip()
                        if "=" in kv:
                            k, v = kv.split("=", 1)
                            k = k.strip().strip('"\'')
                            v = v.strip().strip('"\'')
                            try:
                                pairs[k] = float(v)
                            except ValueError:
                                pairs[k] = v
                    if current_provider:
                        pricing[current_provider] = {
                            "input": pairs.get("input", 1.0) * 1e-6,
                            "output": pairs.get("output", 2.0) * 1e-6,
                            "cache_hit": pairs.get("cache_hit", 0.02) * 1e-6,
                            "currency": pairs.get("currency", "¥"),
                        }
        # Fallback defaults
        if not pricing:
            pricing["default"] = {"input": 1e-6, "output": 2e-6, "cache_hit": 2e-8, "currency": "¥"}
        return pricing
    except Exception:
        return {"default": {"input": 1e-6, "output": 2e-6, "cache_hit": 2e-8, "currency": "¥"}}

PRICING = load_pricing()

# ── Scan all telemetry files ─────────────────────────────────────────

def project_name(dirname: str) -> str:
    """Convert directory name like '-Users-z1-Documents-New project-private_equity_fund_automation' to readable name."""
    name = dirname.strip("-")
    # Remove leading paths that look like filesystem paths
    parts = name.split("-")
    # Try to find the meaningful project name (after common prefixes)
    skip_patterns = {"Users", "z1", "Documents", "Library", "Application Support"}
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
                # Extract timestamp from filename or file content
                ts = _extract_timestamp(tf, data)
                model = _extract_model(tf, data)
                session_id = tf.stem.replace(".jsonl.telemetry", "")

                sources = u.get("sources", {})
                executor = sources.get("executor", {})
                subagent = sources.get("subagent", {})
                compaction = sources.get("compaction", {})

                record = {
                    "project": proj,
                    "session_id": session_id,
                    "timestamp": ts,
                    "model": model,
                    "prompt_tokens": u.get("promptTokens", 0),
                    "completion_tokens": u.get("completionTokens", 0),
                    "total_tokens": u.get("totalTokens", 0),
                    "reasoning_tokens": u.get("reasoningTokens", 0),
                    "cache_hit_tokens": u.get("cacheHitTokens", 0),
                    "cache_miss_tokens": u.get("cacheMissTokens", 0),
                    "request_count": u.get("requestCount", 0),
                    "elapsed_ms": u.get("elapsedMs", 0),
                    "sources": {
                        "executor": {
                            "prompt_tokens": executor.get("promptTokens", 0),
                            "completion_tokens": executor.get("completionTokens", 0),
                            "request_count": executor.get("requestCount", 0),
                        },
                        "subagent": {
                            "prompt_tokens": subagent.get("promptTokens", 0),
                            "completion_tokens": subagent.get("completionTokens", 0),
                            "request_count": subagent.get("requestCount", 0),
                        } if subagent else None,
                        "compaction": {
                            "prompt_tokens": compaction.get("promptTokens", 0),
                            "completion_tokens": compaction.get("completionTokens", 0),
                            "request_count": compaction.get("requestCount", 0),
                        } if compaction else None,
                    },
                }
                # Compute cost
                cost = _compute_cost(record, model, PRICING)
                record["cost_input"] = cost["input"]
                record["cost_cache"] = cost["cache"]
                record["cost_output"] = cost["output"]
                record["cost_total"] = cost["total"]
                sessions.append(record)
            except (json.JSONDecodeError, OSError, KeyError):
                continue
    return sessions

def _extract_timestamp(tf: Path, data: dict) -> str:
    """Extract session timestamp from filename or readFiles."""
    # Try from filename: YYYYMMDD-HHMMSS
    m = re.search(r"(\d{8})-(\d{6})", tf.stem)
    if m:
        try:
            dt = datetime.strptime(f"{m.group(1)}-{m.group(2)}", "%Y%m%d-%H%M%S")
            return dt.isoformat()
        except ValueError:
            pass
    # Try from first readFile entry
    files = data.get("readFiles", [])
    if files and "time" in files[0]:
        try:
            dt = datetime.fromtimestamp(files[0]["time"] / 1000, tz=timezone.utc)
            return dt.isoformat()
        except (OSError, ValueError):
            pass
    return ""

def _extract_model(tf: Path, data: dict) -> str:
    """Extract model name from filename or fallback."""
    # Try from filename pattern
    m = re.search(r"(deepseek-\w+)", tf.stem)
    if m:
        return m.group(1)
    return "unknown"

def _compute_cost(record: dict, model: str, pricing: dict) -> dict:
    """Compute cost for a session based on token usage and pricing."""
    p = pricing.get("deepseek", pricing.get("default", {"input": 1e-6, "output": 2e-6, "cache_hit": 2e-8, "currency": "¥"}))
    # Try to match model to pricing
    for key in pricing:
        if key in model or model in key:
            p = pricing[key]
            break

    prompt = record.get("prompt_tokens", 0)
    completion = record.get("completion_tokens", 0)
    cache_hit = record.get("cache_hit_tokens", 0)
    effective_input = max(0, prompt - cache_hit)

    cost_input = effective_input * p["input"]
    cost_cache = cache_hit * p["cache_hit"]
    cost_output = completion * p["output"]
    cost_total = cost_input + cost_cache + cost_output

    return {"input": cost_input, "cache": cost_cache, "output": cost_output, "total": cost_total}


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
        "total_cost": sum(s.get("cost_total", 0) for s in sessions),
        "projects": defaultdict(lambda: {"sessions": 0, "calls": 0, "prompt": 0, "completion": 0, "cache_hit": 0, "cost": 0.0}),
        "models": defaultdict(lambda: {"sessions": 0, "calls": 0, "prompt": 0, "completion": 0, "cache_hit": 0, "cost": 0.0}),
        "sessions": [],
    }

    for s in sessions:
        proj = s.get("project", "unknown")
        model = s.get("model", "unknown")
        total["projects"][proj]["sessions"] += 1
        total["projects"][proj]["calls"] += s.get("request_count", 0)
        total["projects"][proj]["prompt"] += s.get("prompt_tokens", 0)
        total["projects"][proj]["completion"] += s.get("completion_tokens", 0)
        total["projects"][proj]["cache_hit"] += s.get("cache_hit_tokens", 0)
        total["projects"][proj]["cost"] += s.get("cost_total", 0)

        total["models"][model]["sessions"] += 1
        total["models"][model]["calls"] += s.get("request_count", 0)
        total["models"][model]["prompt"] += s.get("prompt_tokens", 0)
        total["models"][model]["completion"] += s.get("completion_tokens", 0)
        total["models"][model]["cache_hit"] += s.get("cache_hit_tokens", 0)
        total["models"][model]["cost"] += s.get("cost_total", 0)

        total["sessions"].append({
            "project": proj,
            "model": model,
            "timestamp": s.get("timestamp", ""),
            "calls": s.get("request_count", 0),
            "prompt": s.get("prompt_tokens", 0),
            "completion": s.get("completion_tokens", 0),
            "cache_hit": s.get("cache_hit_tokens", 0),
            "cost": round(s.get("cost_total", 0), 4),
        })

    # Sort sessions by timestamp (newest first)
    total["sessions"].sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    # Round
    total["total_cost"] = round(total["total_cost"], 4)
    for p in total["projects"].values():
        p["cost"] = round(p["cost"], 4)
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
  {label:'缓存命中',value:fmt(d.total_cache_hit)+' tokens',sub:'',cls:'cache'},
  {label:'总费用',value:fmtCost(d.total_cost),sub:'',cls:'cost'},
]
cards.forEach(c=>{const div=document.createElement('div');div.className='card';if(c.cls)div.classList.add(c.cls)
div.innerHTML='<div class="label">'+c.label+'</div><div class="value">'+c.value+'</div>'+(c.sub?'<div class="sub">'+c.sub+'</div>':'')
s.appendChild(div)})

// Projects table
const pdiv=document.getElementById('projects')
let phtml='<table><thead><tr><th>项目</th><th>会话</th><th>调用次数</th><th>输入 Tokens</th><th>输出 Tokens</th><th>缓存命中</th><th>费用</th></tr></thead><tbody>'
for(const[name,data]of Object.entries(d.projects)){phtml+='<tr><td><strong>'+name+'</strong></td><td>'+data.sessions+'</td><td>'+fmt(data.calls)+'</td><td>'+fmt(data.prompt)+'</td><td>'+fmt(data.completion)+'</td><td>'+fmt(data.cache_hit)+'</td><td>'+fmtCost(data.cost)+'</td></tr>'}
phtml+='</tbody></table>';pdiv.innerHTML=phtml

// Models table
const mdiv=document.getElementById('models')
let mhtml='<table><thead><tr><th>模型</th><th>会话</th><th>调用次数</th><th>输入 Tokens</th><th>输出 Tokens</th><th>缓存命中</th><th>费用</th></tr></thead><tbody>'
for(const[name,data]of Object.entries(d.models)){mhtml+='<tr><td><strong>'+name+'</strong></td><td>'+data.sessions+'</td><td>'+fmt(data.calls)+'</td><td>'+fmt(data.prompt)+'</td><td>'+fmt(data.completion)+'</td><td>'+fmt(data.cache_hit)+'</td><td>'+fmtCost(data.cost)+'</td></tr>'}
mhtml+='</tbody></table>';mdiv.innerHTML=mhtml

// Sessions table
const sdiv=document.getElementById('sessions')
let shtml='<table><thead><tr><th>时间</th><th>项目</th><th>模型</th><th>调用次数</th><th>输入</th><th>输出</th><th>缓存命中</th><th>费用</th></tr></thead><tbody>'
const recent=d.sessions.slice(0,20)
for(const s of recent){shtml+='<tr><td>'+(s.timestamp.slice(0,19)||'')+'</td><td>'+s.project+'</td><td>'+s.model+'</td><td>'+s.calls+'</td><td>'+fmt(s.prompt)+'</td><td>'+fmt(s.completion)+'</td><td>'+fmt(s.cache_hit)+'</td><td>'+fmtCost(s.cost)+'</td></tr>'}
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