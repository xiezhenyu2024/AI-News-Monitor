#!/usr/bin/env python3
"""腾讯云函数 Web 函数：新闻卡片 + 追问 + 搜索
部署为 SCF Web 函数，监听 9000 端口
"""
import json
import os
import re
import time
import urllib.request
import urllib.parse
from xml.etree import ElementTree
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

REPO = "xiezhenyu2024/AI-News-Monitor"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/master/docs"
API_BASE = f"https://api.github.com/repos/{REPO}/contents/docs"
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = "deepseek-chat"
CACHE = "/tmp/newsqa"
os.makedirs(CACHE, exist_ok=True)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def cache_get(name, ttl=300):
    p = os.path.join(CACHE, name)
    if os.path.exists(p):
        if time.time() - os.path.getmtime(p) < ttl:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    return None


def cache_set(name, data):
    with open(os.path.join(CACHE, name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def http_get(url, timeout=15, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


# ─── 数据 ───────────────────────────────────────────────────────────────

def list_contexts() -> list[dict]:
    cached = cache_get("ctx_list")
    if cached is not None:
        return cached
    keys = []
    for attempt in range(3):
        try:
            data = json.loads(http_get(API_BASE, headers={"User-Agent": "newsqa"}, timeout=15))
            for item in data:
                name = item.get("name", "")
                if name.startswith("context-") and name.endswith(".json"):
                    keys.append(name[8:-5])
            break
        except Exception as e:
            log(f"list_contexts attempt {attempt+1}: {e}")
    result = [{"key": k, "label": k} for k in sorted(keys, reverse=True)]
    cache_set("ctx_list", result)
    return result


def load_context(key: str) -> dict:
    """多源重试 + /tmp 缓存：先 raw.githubusercontent，失败切 GitHub API（base64）"""
    cached = cache_get(f"ctx_{key}", ttl=3600)
    if cached is not None:
        return cached
    errs = []
    # 源1: raw
    try:
        data = json.loads(http_get(f"{RAW_BASE}/context-{key}.json", timeout=15))
        cache_set(f"ctx_{key}", data)
        return data
    except Exception as e:
        errs.append(f"raw: {e}")
    # 源2: GitHub contents API（api.github.com 国内可达）
    try:
        import base64 as b64
        data = json.loads(http_get(
            f"{API_BASE}/context-{key}.json",
            headers={"User-Agent": "newsqa", "Accept": "application/vnd.github.v3+json"},
            timeout=15))
        content = data.get("content", "")
        if data.get("encoding") == "base64" and content:
            ctx = json.loads(b64.b64decode(content).decode("utf-8"))
            cache_set(f"ctx_{key}", ctx)
            return ctx
        raise RuntimeError("contents API 返回异常")
    except Exception as e:
        errs.append(f"api: {e}")
    raise RuntimeError(f"加载 {key} 失败: {'; '.join(errs)}")


# ─── DeepSeek ───────────────────────────────────────────────────────────

def chat(system: str, msgs: list[dict]) -> str:
    if not DEEPSEEK_API_KEY:
        return "（未配置 DEEPSEEK_API_KEY，无法回答）"
    body = json.dumps({
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "system", "content": system}] + msgs,
        "temperature": 0.7,
        "max_tokens": 5000,
    }).encode()
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read().decode())
    return d["choices"][0]["message"]["content"].strip()


def answer_question(ctx: dict, question: str, history: list, card_idx: int = None) -> str:
    cards = ctx.get("cards", [])

    # 只加载当前对话卡片的背景资料（不再加载全部新闻）
    if card_idx is not None and 0 <= card_idx < len(cards):
        card = cards[card_idx]
        src_text = card.get("source_text", "")
        src_title = card.get("source_title", "")
        src_url = card.get("source_url", "")
        src_name = card.get("source_name", "")
        if src_text:
            full_text = (f"来源: {src_name}\n"
                         f"标题: {src_title}\n"
                         f"原文摘要: {src_text[:1000]}\n"
                         f"URL: {src_url}")
        else:
            full_text = f"（该卡片暂无附加原文，仅卡片摘要可用）\n标题: {card.get('title','')}\n摘要: {card.get('summary','')}"
        cards_text = (f"#1 {card.get('title','')}\n"
                      f"  关键实体: {json.dumps(card.get('entities',[]), ensure_ascii=False)}\n"
                      f"  摘要: {card.get('summary','')}")
    else:
        full_text = "（未指定卡片）"
        cards_text = "（无）"

    system = f"""你是新闻问答助手。用户正在阅读一张新闻卡片，并针对这条新闻提问。你必须基于以下"该新闻的原文资料"回答，不要泛泛而谈。

【该新闻的背景资料】
{full_text}

【该新闻的摘要卡片】
{cards_text}

要求：
1. 回答围绕这张卡片对应的新闻展开（用户说"这条新闻""这件事""这家公司"都指当前这张卡片）
2. 基于资料回答，引用具体内容
3. 回答完整清晰、结构分明、逻辑严密，必要时可以写800-1500字
4. 出现专有名词或新概念时，用一句话解释
5. 资料不足以回答时，明确说"该新闻资料中没有相关信息"，再基于通用知识补充并注明"以下为通用知识，非该新闻内容"
6. 支持连续追问：结合上文问句理解指代

【深度分析模式——当用户的问题涉及判断、选择、决策、评价、利弊权衡时，必须使用双向钢人论证框架】

回答流程（严格按顺序）：
第一步：先别急着回答，也别默认用户已经把问题想清楚。用最完整、最有力的方式，重述用户真正想解决的问题（包括用户可能没说出来的深层意图）。
第二步：使用钢人论证法分别给出：
  a) 支持用户当前想法/立场的最强论证（把对方观点推到最合理、最有力的版本）
  b) 反对它的最强论证（同样推到最强，不用稻草人）
  每个论证都要给出理由、依据和适用条件。
第三步：找出双方真正的分歧点，以及最可能改变结论的关键变量（什么条件发生变化会翻转结论）。
第四步：只问用户一个最关键的问题（这个问题最能帮助双方收敛分歧）。
第五步：等用户回答后，再给出明确判断、理由和下一步行动。

注意：
- 第一步到第四步输出在当前回答中；第五步的"等用户回答后"表示：如果用户继续追问，则在下一轮给出明确判断。
- 如果用户的问题只是事实查询（如"这条新闻说了什么"），直接回答即可，不需要启用钢人论证。
- 如果用户明确说"简单回答""一句话"，则跳过该框架。"""
    msgs = []
    for m in (history or [])[-6:]:  # 只带最近6轮
        content = m.get("content", "")
        if len(content) > 800:  # 每条历史截断，防止请求体超限
            content = content[:800] + "…（已截断）"
        msgs.append({"role": m["role"], "content": content})
    msgs.append({"role": "user", "content": question[:1000]})
    return chat(system, msgs)


# ─── 搜索（复用现有信源）───────────────────────────────────────────────

def parse_rss_items(xml_text: str) -> list:
    """容错解析 RSS：先试 ElementTree，失败用正则提取"""
    items = []
    try:
        root = ElementTree.fromstring(xml_text)
        for item in root.iter("item"):
            items.append({
                "title": item.findtext("title") or "",
                "link": item.findtext("link") or "",
                "desc": re.sub(r"<[^>]+>", " ", item.findtext("description") or "").strip(),
            })
        return items
    except Exception:
        pass
    for m in re.finditer(r"<item>(.*?)</item>", xml_text, re.DOTALL):
        block = m.group(1)
        t = re.search(r"<title>(.*?)</title>", block, re.DOTALL)
        l = re.search(r"<link>(.*?)</link>", block, re.DOTALL)
        d = re.search(r"<description>(.*?)</description>", block, re.DOTALL)
        title = re.sub(r"<!\[CDATA\[|\]\]>", "", t.group(1)) if t else ""
        link = l.group(1) if l else ""
        desc = re.sub(r"<!\[CDATA\[|\]\]>", "", d.group(1)) if d else ""
        items.append({
            "title": title,
            "link": link,
            "desc": re.sub(r"<[^>]+>", " ", desc).strip(),
        })
    return items


def search_sources(keyword: str, limit: int = 10) -> list[dict]:
    kw = keyword.lower()
    results = []
    seen = set()
    t0 = time.time()
    MAX_TIME = 12.0

    def add(source, title, url, summary):
        if time.time() - t0 > MAX_TIME:
            return
        sig = title[:50].lower()
        if sig not in seen and len(results) < limit:
            seen.add(sig)
            results.append({"source": source, "title": title,
                            "url": url, "summary": (summary or "")[:300]})

    # 1. 国内 RSS（快）：36氪 + 开源中国 + IT之家 + 少数派
    for name, feed in [("36氪", "https://36kr.com/feed"),
                       ("开源中国", "https://www.oschina.net/news/rss"),
                       ("IT之家", "https://www.ithome.com/rss/"),
                       ("少数派", "https://sspai.com/feed")]:
        try:
            for item in parse_rss_items(http_get(feed, timeout=8)):
                t = item["title"]
                if kw in t.lower():
                    add(name, t, item["link"], item["desc"][:250])
        except Exception as e:
            log(f"search {name}: {e}")

    # 2. 知乎日报
    try:
        data = json.loads(http_get("https://news-at.zhihu.com/api/4/news/latest", timeout=8))
        for it in data.get("stories", [])[:15]:
            t = it.get("title", "")
            if kw in t.lower():
                add("知乎日报", t, f"https://daily.zhihu.com/story/{it.get('id','')}", "")
    except Exception as e:
        log(f"search zhihu: {e}")

    # 3. GitHub 仓库
    try:
        q = urllib.parse.quote(keyword)
        data = json.loads(http_get(
            f"https://api.github.com/search/repositories?q={q}&sort=stars&per_page=5",
            headers={"User-Agent": "newsqa"}, timeout=10))
        for it in data.get("items", []):
            add("GitHub", it.get("full_name", ""), it.get("html_url", ""),
                it.get("description") or "")
    except Exception as e:
        log(f"search gh: {e}")

    # 4. Arxiv
    try:
        q = urllib.parse.quote(keyword)
        xml = http_get(f"http://export.arxiv.org/api/query?search_query=all:{q}&max_results=5", timeout=10)
        root = ElementTree.fromstring(xml)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for e in root.findall("a:entry", ns):
            t = (e.findtext("a:title", default="", namespaces=ns) or "").strip().replace("\n", " ")
            s = (e.findtext("a:summary", default="", namespaces=ns) or "").strip()
            lid = e.findtext("a:id", default="", namespaces=ns) or ""
            add("Arxiv", t[:150], lid, s[:300])
    except Exception as e:
        log(f"search arxiv: {e}")

    # 5. Hacker News（最后，慢）
    try:
        ids = json.loads(http_get("https://hacker-news.firebaseio.com/v0/topstories.json", timeout=10))
        from concurrent.futures import ThreadPoolExecutor, as_completed
        def get_story(sid):
            try:
                it = json.loads(http_get(f"https://hacker-news.firebaseio.com/v0/item/{sid}.json", timeout=8))
                return it if it and it.get("type") == "story" else None
            except Exception:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            for it in as_completed([pool.submit(get_story, sid) for sid in ids[:10]]):
                story = it.result()
                if not story:
                    continue
                title = story.get("title", "") or ""
                text = re.sub(r"<[^>]+>", " ", story.get("text", "") or "")
                if kw in title.lower() or kw in text.lower():
                    add("HN", title, f"https://news.ycombinator.com/item?id={story['id']}", text[:300])
    except Exception as e:
        log(f"search HN: {e}")

    return results[:limit]


def build_search_cards(keyword: str, results: list) -> list:
    raw = "\n".join(f"[{r['source']}] {r['title']}\n  {r['summary']}\n  {r['url']}"
                    for r in results)
    system = """你是新闻整理助手。把搜索结果整理成2-5张卡片，输出JSON数组：
[{"title":"一句话标题","entities":[{"name":"实体","explain":"一句话解释"}],"summary":"2-4句摘要","relevant":"为何值得关注","url":"来源链接"}]
要求：关键实体必须解释；只输出JSON数组。"""
    res = chat(system, [{"role": "user",
                         "content": f"搜索词：{keyword}\n\n搜索结果：\n{raw}"}])
    try:
        m = re.search(r"\[.*\]", res, re.DOTALL)
        cards = json.loads(m.group(0)) if m else []
        return cards if isinstance(cards, list) else []
    except Exception:
        return []


# ─── 网页 ───────────────────────────────────────────────────────────────

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>新闻问答</title>
<style>
*{box-sizing:border-box}
body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;max-width:820px;margin:0 auto;padding:14px;background:#f7f7f8;color:#222}
.top{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
h2{margin:0;font-size:18px}
select{flex:1;min-width:180px;padding:8px;font-size:14px;border:1px solid #ddd;border-radius:8px;background:#fff}
.tab{display:flex;gap:6px;margin-bottom:12px}
.tab button{flex:1;padding:9px;font-size:14px;border:1px solid #ddd;border-radius:8px;background:#fff;cursor:pointer}
.tab button.on{background:#0b57d0;color:#fff;border-color:#0b57d0}
.card{background:#fff;border-radius:10px;padding:14px 16px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.card h3{margin:0 0 6px;font-size:15px}
.card p{font-size:13px;line-height:1.7;margin:4px 0}
.card .ask{margin-top:8px;font-size:12px;color:#0b57d0;background:#eef3fd;border:none;border-radius:6px;padding:5px 10px;cursor:pointer}
.chat{background:#fff;border-radius:10px;padding:14px 16px;box-shadow:0 1px 3px rgba(0,0,0,.08);margin-top:14px}
.msg{margin:8px 0;padding:10px 12px;border-radius:8px;font-size:14px;line-height:1.7;white-space:pre-wrap}
.user{background:#0b57d0;color:#fff;margin-left:40px}
.ai{background:#f4f6fa;margin-right:40px}
.inputrow{display:flex;gap:8px;margin-top:10px}
input{flex:1;padding:10px;font-size:14px;border:1px solid #ddd;border-radius:8px}
button{padding:10px 18px;font-size:14px;background:#0b57d0;color:#fff;border:none;border-radius:8px;cursor:pointer}
.hint{color:#888;font-size:12px;margin:4px 0 10px}
.loading{color:#888;font-size:13px;text-align:center;padding:20px}
</style>
</head>
<body>
<div class="top">
  <h2>📰 新闻问答</h2>
  <select id="pick" onchange="switchCtx()"></select>
</div>
<div class="tab">
  <button id="tabCards" class="on" onclick="switchTab('cards')">今日卡片</button>
  <button id="tabSearch" onclick="switchTab('search')">搜索补充</button>
</div>
<div id="cards"></div>
<div id="search" style="display:none">
  <div class="inputrow">
    <input id="skw" placeholder="输入关键词，从新闻源搜索新内容">
    <button onclick="doSearch()">搜索</button>
  </div>
  <div id="sres" class="hint" style="margin-top:10px"></div>
</div>
<div class="chat">
  <b>💬 追问</b>
  <p class="hint">基于当天全部新闻原文回答，点卡片「问这条」快速提问。</p>
  <div id="msgs"></div>
  <div class="inputrow">
    <input id="q" placeholder="输入问题，如：第三条新闻里那家公司是做什么的？" onkeydown="if(event.key==='Enter')ask()">
    <button onclick="ask()">提问</button>
  </div>
</div>
<script>
const CONTEXTS = __CONTEXTS__;
let ctx = null;

function renderCards(){
  if(!ctx || !ctx.cards || !ctx.cards.length){ document.getElementById('cards').innerHTML='<div class="loading">该期无卡片数据</div>'; return; }
  document.getElementById('cards').innerHTML =
    ctx.cards.map((c,i)=>{
      const ents=(c.entities||[]).map(e=>`<p><b>${e.name}</b>：${e.explain}</p>`).join('');
      const rel=c.relevant?`<p style="color:#0b57d0">与你相关：${c.relevant}</p>`:'';
      return `<div class="card"><h3>#${i+1} ${c.title}</h3>${ents}<p>${c.summary}</p>${rel}<button class="ask" onclick="askAbout(${i+1})">问这条</button></div>`;
    }).join('');
}

function switchCtx(){
  const key = document.getElementById('pick').value;
  ctx = CONTEXTS[key] || null;
  renderCards();
}

function askAbout(n){ document.getElementById('q').value='第'+n+'条：'; }
function switchTab(t){
  document.getElementById('cards').style.display = t==='cards' ? '' : 'none';
  document.getElementById('search').style.display = t==='search' ? '' : 'none';
  document.getElementById('tabCards').className = t==='cards' ? 'on' : '';
  document.getElementById('tabSearch').className = t==='search' ? 'on' : '';
}
async function doSearch(){
  const kw = document.getElementById('skw').value.trim();
  if(!kw) return;
  const box = document.getElementById('sres');
  box.innerHTML = '搜索中…';
  try{
    const r = await fetch('/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({keyword:kw})});
    if(!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    box.innerHTML = d.cards.length
      ? d.cards.map((c,i)=>`<div class="card"><h3>#${i+1} ${c.title}</h3><p>${c.summary}</p><p style="color:#888">来源：<a href="${c.url||'#'}">${c.url||''}</a></p></div>`).join('')
      : '未找到相关内容';
  }catch(e){
    box.innerHTML = '搜索失败: ' + e.message + '（网络不稳定时请稍后重试）';
  }
}
function addMsg(role,text){
  const d=document.createElement('div');
  d.className='msg '+role;
  d.textContent=text;
  document.getElementById('msgs').appendChild(d);
}
async function ask(){
  const q=document.getElementById('q').value.trim();
  if(!q){ addMsg('ai','请输入问题'); return; }
  if(!curCard){ addMsg('ai','请先选择一张卡片'); return; }
  addMsg('user',q);
  document.getElementById('q').value='';
  const key=curCard.ctxKey;
  const card_idx=curCard.idx;
  const history=[...document.querySelectorAll('#msgs .msg')].slice(-12).map(m=>({role:m.classList.contains('user')?'user':'assistant',content:(m.textContent||'').slice(0,800)}));
  try{
    const r=await fetch(API + '/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key,question:q,history,card_idx})});
    if(!r.ok) throw new Error('HTTP ' + r.status);
    const d=await r.json();
    addMsg('ai',d.answer||'（无回答）');
  }catch(e){
    addMsg('ai','提问失败: ' + e.message + '，网络不稳定时请稍后重试');
  }
}

(function init(){
  const sel = document.getElementById('pick');
  const keys = Object.keys(CONTEXTS);
  sel.innerHTML = keys.map(k=>`<option value="${k}">${CONTEXTS[k].label||k}</option>`).join('');
  if(keys.length){
    switchCtx();
  } else {
    document.getElementById('cards').innerHTML='<div class="loading">暂无新闻数据，请等下一期日报生成</div>';
  }
})();
</script>
</body></html>"""


# ─── HTTP 服务 ──────────────────────────────────────────────────────────

def build_page() -> str:
    """构建页面：把最新几期卡片数据直接内嵌进 HTML（打开即显示，零接口请求）"""
    try:
        contexts = {}
        for item in list_contexts()[:5]:
            try:
                ctx = load_context(item["key"])
                contexts[item["key"]] = {
                    "key": item["key"],
                    "label": item.get("label", item["key"]),
                    "cards": ctx.get("cards", []),
                }
            except Exception:
                continue
    except Exception:
        contexts = {}
    payload = json.dumps(contexts, ensure_ascii=False)
    payload_escaped = payload.replace("</", "<\\/")
    return PAGE_TEMPLATE.replace("__CONTEXTS__", payload_escaped)


def route_request(method: str, path: str, query: dict, body_raw: str):
    """核心路由逻辑，返回 (status_code, headers, body_bytes)"""
    NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}
    CORS = {"Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Requested-With"}
    # OPTIONS 预检：直接放行（解决手机浏览器跨域/转码问题）
    if method == "OPTIONS":
        h = dict(CORS)
        h.update(NO_CACHE)
        return 200, h, b""
    # HEAD：与 GET 同路由但无 body（运营商代理/浏览器探测用）
    if method == "HEAD":
        code, h, body = route_request("GET", path, query, body_raw)
        return code, h, b""
    if method == "GET":
        if path in ("/", "/index.html"):
            h = dict(NO_CACHE)
            h.update(CORS)
            h["Content-Type"] = "text/html; charset=utf-8"
            return 200, h, build_page().encode("utf-8")
        if path in ("/list", "/api/list"):
            h = dict(NO_CACHE)
            h.update(CORS)
            h["Content-Type"] = "application/json; charset=utf-8"
            data = json.dumps(list_contexts(), ensure_ascii=False).encode("utf-8")
            return 200, h, data
        if path in ("/ctx", "/api/context"):
            h = dict(NO_CACHE)
            h.update(CORS)
            h["Content-Type"] = "application/json; charset=utf-8"
            key = (query or {}).get("key", "")
            data = json.dumps(load_context(key), ensure_ascii=False).encode("utf-8")
            return 200, h, data
        h = dict(CORS)
        return 404, h, b'{"error":"not found"}'
    elif method == "POST":
        try:
            data = json.loads(body_raw or "{}")
            if path in ("/ask", "/api/ask"):
                key = data.get("key", "")
                ctx = load_context(key)
                ans = answer_question(ctx, data.get("question", ""),
                                      data.get("history", []),
                                      data.get("card_idx"))
                out = json.dumps({"answer": ans}, ensure_ascii=False).encode("utf-8")
                h = dict(CORS)
                h["Content-Type"] = "application/json; charset=utf-8"
                return 200, h, out
            if path in ("/search", "/api/search"):
                kw = data.get("keyword", "")
                results = search_sources(kw)
                cards = build_search_cards(kw, results) if results else []
                out = json.dumps({"cards": cards}, ensure_ascii=False).encode("utf-8")
                h = dict(CORS)
                h["Content-Type"] = "application/json; charset=utf-8"
                return 200, h, out
            h = dict(CORS)
            return 404, h, b'{"error":"not found"}'
        except Exception as e:
            out = json.dumps({"answer": f"错误: {e}"}, ensure_ascii=False).encode("utf-8")
            h = dict(CORS)
            return 500, h, out
    h = dict(CORS)
    return 405, h, b'{"error":"method not allowed"}'


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            query = {k: v[0] for k, v in query.items()}
            code, headers, body = route_request("GET", parsed.path, query, "")
            self._send(code, headers.get("Content-Type", "text/plain"), body)
        except Exception as e:
            self._json(500, {"error": str(e)})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body_raw = self.rfile.read(length).decode("utf-8")
            parsed = urllib.parse.urlparse(self.path)
            code, headers, body = route_request("POST", parsed.path, {}, body_raw)
            self._send(code, headers.get("Content-Type", "text/plain"), body)
        except Exception as e:
            self._json(500, {"answer": f"错误: {e}"})

    def log_message(self, fmt, *args):
        log(f"  {fmt % args}")


def main_handler(event, context):
    log(f"event keys: {sorted((event or {}).keys())[:20]}")
    if event is None or not isinstance(event, dict) or "path" not in event:
        log("Web Function mode: 监听 9000 端口")
        ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()

    method = event.get("httpMethod", "GET")
    path = event.get("path", "/")
    log(f"REQ: method={method!r} path={path!r}")
    query = event.get("queryStringParameters") or event.get("queryString") or {}
    if isinstance(query, str):
        query = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
    body_raw = event.get("body", "") or ""
    if event.get("isBase64Encoded"):
        import base64 as b64
        body_raw = b64.b64decode(body_raw).decode("utf-8", errors="replace")

    code, headers, body = route_request(method, path, query, body_raw)
    return {
        "statusCode": code,
        "headers": headers,
        "body": body.decode("utf-8", errors="replace"),
        "isBase64Encoded": False,
    }


if __name__ == "__main__":
    main_handler(None, None)
