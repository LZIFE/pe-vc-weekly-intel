#!/usr/bin/env python3
"""
Cost & Usage Dashboard — serves a web UI showing DeepSeek API usage, token counts,
cache hit rates, and estimated costs.  Run on any port (default 8899).
"""

import json
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Ensure sibling modules are importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cost_tracker

PORT = int(os.environ.get("DASHBOARD_PORT", "8899"))
HTML = """<!DOCTYPE html>
<html lang="zh-Hans">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Cost Dashboard — PE/VC Weekly</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Noto Sans SC',sans-serif;background:#f5f5f7;color:#1d1d1f;padding:24px;max-width:1000px;margin:auto}
h1{font-size:24px;font-weight:600;margin-bottom:8px;letter-spacing:-0.3px}
h2{font-size:16px;font-weight:500;color:#86868b;margin-bottom:24px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:24px}
.card{background:#fff;border-radius:12px;padding:20px;box-shadow:0 1px 3px rgba(0,0,0,0.06);transition:box-shadow 0.2s}
.card:hover{box-shadow:0 4px 12px rgba(0,0,0,0.1)}
.card .label{font-size:12px;font-weight:500;text-transform:uppercase;color:#86868b;margin-bottom:6px;letter-spacing:0.5px}
.card .value{font-size:28px;font-weight:600;line-height:1.2}
.card .sub{font-size:13px;color:#86868b;margin-top:4px}
.cost{color:#1a5276}
.tokens{color:#2d6a4f}
.cache{color:#7b2d8e}
.errors{color:#c0392b}
table{width:100%;border-collapse:collapse;margin-top:16px;font-size:13px}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid #e8e8ec}
th{font-weight:500;color:#86868b;text-transform:uppercase;font-size:11px;letter-spacing:0.5px}
td:last-child,th:last-child{text-align:right}
tr:last-child td{border-bottom:none}
.footer{text-align:center;font-size:12px;color:#aeaeb2;margin-top:32px;padding-top:16px;border-top:1px solid #e8e8ec}
.refresh{display:inline-block;margin-top:8px;font-size:12px;color:#1a5276;text-decoration:none;cursor:pointer}
</style>
</head>
<body>
<h1>AI 费用监控</h1>
<h2>DeepSeek API 用量 &amp; 成本估算</h2>
<div id="root"><div class="grid" id="summary"></div><h2>各模型明细</h2><div id="models"></div></div>
<div class="footer">
  <span id="last-updated">加载中…</span>
  <a class="refresh" onclick="location.reload()">↻ 刷新</a>
</div>
<script>
async function load(){const r=await fetch('/api/summary');const d=await r.json();render(d);document.getElementById('last-updated').textContent='最后更新: '+new Date().toLocaleString('zh-CN')}
function render(d){const s=document.getElementById('summary');s.innerHTML=''
const cards=[
  {label:'总调用次数',value:d.total_calls,sub:'成功 '+d.success_calls+' / 失败 '+d.error_calls,cls:''},
  {label:'总 Token 消耗',value:d.total_tokens.toLocaleString(),sub:'输入 '+d.total_prompt.toLocaleString()+' / 输出 '+d.total_completion.toLocaleString(),cls:'tokens'},
  {label:'缓存命中率',value:d.cache_rate+'%',sub:'命中 '+d.total_cache_hit.toLocaleString()+' tokens',cls:'cache'},
  {label:'总费用 (¥)',value:'¥'+d.total_cost.toFixed(2),sub:'近24h ¥'+d.recent_24h_cost.toFixed(2),cls:'cost'},
]
cards.forEach(c=>{const div=document.createElement('div');div.className='card';if(c.cls)div.classList.add(c.cls)
div.innerHTML='<div class="label">'+c.label+'</div><div class="value">'+c.value+'</div>'+(c.sub?'<div class="sub">'+c.sub+'</div>':'')
s.appendChild(div)})
const m=document.getElementById('models');m.innerHTML=''
if(Object.keys(d.models).length===0){m.innerHTML='<p style="color:#86868b">暂无数据</p>';return}
let html='<table><thead><tr><th>模型</th><th>调用次数</th><th>输入 Tokens</th><th>输出 Tokens</th><th>缓存命中</th><th>费用 (¥)</th></tr></thead><tbody>'
for(const[name,data]of Object.entries(d.models)){html+='<tr><td><strong>'+name+'</strong></td><td>'+data.calls+'</td><td>'+data.prompt_tokens.toLocaleString()+'</td><td>'+data.completion_tokens.toLocaleString()+'</td><td>'+data.cache_hit_tokens.toLocaleString()+'</td><td>¥'+data.cost.toFixed(2)+'</td></tr>'}
html+='</tbody></table>';m.innerHTML=html}
load()
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/summary":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            records = cost_tracker.load_records()
            summary = cost_tracker.compute_summary(records)
            self.wfile.write(json.dumps(summary, ensure_ascii=False).encode("utf-8"))
        elif self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # silence request logs


def main():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"📊 Cost Dashboard: http://127.0.0.1:{PORT}")
    print(f"   Log file: {cost_tracker.LOG_FILE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()