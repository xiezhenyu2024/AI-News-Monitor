#!/usr/bin/env python3
"""本地新闻问答应用 - 纯标准库实现，零依赖

启动: python server.py
然后浏览器打开 http://localhost:8000
"""
import json
import os
import re
import sys
import html as html_mod
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

TZ_CST = timezone(timedelta(hours=8))
DOCS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
PORT = int(os.environ.get("PORT", "8000"))
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = "deepseek-chat"

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>新闻问答</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;max-width:820px;margin:0 auto;padding:16px;background:#f7f7f8;color:#222}}
.top{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}}
select{{flex:1;min-width:220px;padding:8px;font-size:14px;border:1px solid #ddd;border-radius:8px;background:#fff}}
.card{{background:#fff;border-radius:10px;padding:14px 16px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
.card h3{{margin:0 0 6px;font-size:15px}}
.card p{{font-size:13px;line-height:1.7;margin:4px 0}}
.card .ask{{margin-top:8px;font-size:12px;color:#0b57d0;background:#eef3fd;border:none;border-radius:6px;padding:5px 10px;cursor:pointer}}
.chat{{background:#fff;border-radius:10px;padding:14px 16px;box-shadow:0 1px 3px rgba(0,0,0,.08);margin-top:16px}}
.msg{{margin:8px 0;padding:10px 12px;border-radius:8px;font-size:14px;line-height:1.7;white-space:pre-wrap}}
.user{{background:#0b57d0;color:#fff;margin-left:40px}}
.ai{{background:#f4f6fa;margin-right:40px}}
.inputrow{{display:flex;gap:8px;margin-top:10px}}
input{{flex:1;padding:10px;font-size:14px;border:1px solid #ddd;border-radius:8px}}
button{{padding:10px 18px;font-size:14px;background:#0b57d0;color:#fff;border:none;border-radius:8px;cursor:pointer}}
.hint{{color:#888;font-size:12px;margin:4px 0 10px}}
</style>
</head>
<body>
<div class="top">
  <h2 style="margin:0">📰 新闻卡片 + 追问</h2>
  <select id="pick"></select>
</div>
<div id="cards"></div>
<div class="chat">
  <b>💬 追问</b>
  <p class="hint">对任意新闻提问，AI 基于当天全部新闻原文和背景资料回答。可直接点卡片上的「问这条」。</p>
  <div id="msgs"></div>
  <div class="inputrow">
    <input id="q" placeholder="输入问题，例如：第三条新闻里那家公司是做什么的？" onkeydown="if(event.key==='Enter')ask()">
    <button onclick="ask()">提问</button>
  </div>
</div>
<script>
let ctx = null;
async function loadList() {{
  const r = await fetch('/api/list');
  const list = await r.json();
  const sel = document.getElementById('pick');
  sel.innerHTML = list.map(e => `<option value="${{e.key}}">${{e.label}} · ${{e.generated_at}}</option>`).join('');
  if (list.length) {{ sel.value = list[0].key; await loadCtx(); }}
}}
async function loadCtx() {{
  const key = document.getElementById('pick').value;
  const r = await fetch('/api/context?key=' + key);
  ctx = await r.json();
  const div = document.getElementById('cards');
  div.innerHTML = ctx.cards.map((c,i) => {{
    const ents = (c.entities||[]).map(e => `<p><b>${{e.name}}</b>：${{e.explain}}</p>`).join('');
    const rel = c.relevant ? `<p style="color:#0b57d0">与你相关：${{c.relevant}}</p>` : '';
    return `<div class="card"><h3>#${{i+1}} ${{c.title}}</h3>${{ents}}<p>${{c.summary}}</p>${{rel}}<button class="ask" onclick="askAbout(${{i+1}})">问这条</button></div>`;
  }}).join('');
}}
function askAbout(n) {{ document.getElementById('q').value = '第' + n + '条：'; }}
function addMsg(role, text) {{
  const div = document.getElementById('msgs');
  const m = document.createElement('div');
  m.className = 'msg ' + role;
  m.textContent = text;
  div.appendChild(m);
  div.scrollTop = div.scrollHeight;
}}
async function ask() {{
  const q = document.getElementById('q').value.trim();
  if (!q || !ctx) return;
  addMsg('user', q);
  document.getElementById('q').value = '';
  const key = ctx.key;
  const history = [...document.querySelectorAll('#msgs .msg')].slice(-8).map(m => ({{
    role: m.classList.contains('user') ? 'user' : 'assistant',
    content: m.textContent
  }}));
  const r = await fetch('/api/ask', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{key, question: q, history}})
  }});
  const data = await r.json();
  addMsg('ai', data.answer || '（无回答）');
}}
loadList();
</script>
</body></html>
"""


def log(msg):
    print(f"[{datetime.now(TZ_CST).strftime('%H:%M:%S')}] {msg}", flush=True)


def list_contexts() -> list[dict]:
    results = []
    if not os.path.isdir(DOCS_DIR):
        return results
    for fn in sorted(os.listdir(DOCS_DIR), reverse=True):
        if fn.startswith("context-") and fn.endswith(".json"):
            try:
                with open(os.path.join(DOCS_DIR, fn), "r", encoding="utf-8") as f:
                    data = json.load(f)
                results.append({
                    "key": data.get("key", fn[8:-5]),
                    "label": data.get("label", ""),
                    "generated_at": data.get("generated_at", ""),
                })
            except Exception:
                continue
    return results


def call_deepseek(system_prompt: str, user_prompt: str) -> str:
    if not DEEPSEEK_API_KEY:
        return "（未配置 DEEPSEEK_API_KEY，无法回答）"
    body = json.dumps({
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 2000,
    }).encode()
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"].strip()


def build_answer(ctx: dict, question: str, history: list) -> str:
    cards = ctx.get("cards", [])
    items = ctx.get("items", [])
    full_text = "\n".join(
        f"[{it['source']}] {it['title']}\n  详情: {it.get('summary','')}\n  URL: {it.get('url','')}"
        for it in items[:40]
    )
    cards_text = "\n".join(
        f"#{i+1} {c.get('title','')}\n  关键实体: {json.dumps(c.get('entities',[]), ensure_ascii=False)}\n  摘要: {c.get('summary','')}"
        for i, c in enumerate(cards)
    )
    system = f"""你是新闻问答助手。用户会基于当天新闻提问，你必须基于以下"当天新闻全文"回答，而不是泛泛而谈。

当天新闻全文：
{full_text}

当天摘要卡片：
{cards_text}

要求：
1. 先定位问题涉及的新闻（用户可能说"第三条""那家公司"等指代）
2. 基于全文回答，引用具体内容
3. 回答简洁直接，不超过200字
4. 出现专有名词或新概念时，用一句话解释
5. 如果当天资料不足以回答，明确说"当天资料中没有相关信息"，然后基于你的通用知识补充，并注明"以下为通用知识，非当天新闻内容"
6. 支持连续追问：结合用户上文的问句理解指代"""
    msgs = [{"role": "user", "content": m["content"]} if m["role"] == "user"
            else {"role": "assistant", "content": m["content"]}
            for m in (history or [])]
    msgs.append({"role": "user", "content": question})
    body = json.dumps({
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "system", "content": system}] + msgs,
        "temperature": 0.3,
        "max_tokens": 2000,
    }).encode()
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"].strip()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, content_type, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE_TEMPLATE.encode("utf-8"))
        elif path == "/api/list":
            self._send(200, "application/json; charset=utf-8",
                       json.dumps(list_contexts(), ensure_ascii=False).encode("utf-8"))
        elif path == "/api/context":
            from urllib.parse import urlparse, parse_qs
            key = parse_qs(urlparse(self.path).query).get("key", [""])[0]
            fpath = os.path.join(DOCS_DIR, f"context-{key}.json")
            if os.path.exists(fpath):
                with open(fpath, "r", encoding="utf-8") as f:
                    self._send(200, "application/json; charset=utf-8", f.read().encode("utf-8"))
            else:
                self._send(404, "application/json", b'{"error":"not found"}')
        elif path.startswith("/docs/"):
            rel = path[len("/docs/"):]
            fpath = os.path.join(DOCS_DIR, os.path.basename(rel))
            if os.path.exists(fpath):
                with open(fpath, "rb") as f:
                    self._send(200, "text/html; charset=utf-8", f.read())
            else:
                self._send(404, "text/plain", b"not found")
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        if self.path != "/api/ask":
            self._send(404, "text/plain", b"not found")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            key = data.get("key", "")
            question = data.get("question", "")
            history = data.get("history", [])
            fpath = os.path.join(DOCS_DIR, f"context-{key}.json")
            if not os.path.exists(fpath):
                msg = json.dumps({"answer": "未找到该期新闻上下文"}, ensure_ascii=False).encode("utf-8")
                self._send(404, "application/json", msg)
                return
            with open(fpath, "r", encoding="utf-8") as f:
                ctx = json.load(f)
            answer = build_answer(ctx, question, history)
            self._send(200, "application/json; charset=utf-8",
                       json.dumps({"answer": answer}, ensure_ascii=False).encode("utf-8"))
        except Exception as e:
            self._send(500, "application/json",
                       json.dumps({"answer": f"错误: {e}"}, ensure_ascii=False).encode("utf-8"))

    def log_message(self, fmt, *args):
        log(f"  {self.address_string()} {fmt % args}")


def main():
    if not os.path.isdir(DOCS_DIR):
        log(f"警告: 未找到 docs/ 目录（{DOCS_DIR}）")
    log(f"新闻问答服务: http://localhost:{PORT}")
    log("按 Ctrl+C 停止")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
