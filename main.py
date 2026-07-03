#!/usr/bin/env python3
"""前沿日报 - 多领域新闻抓取 + DeepSeek摘要 + PushPlus推送"""

import json
import os
import sys
import hashlib
import re
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ─── 配置 ──────────────────────────────────────────────────────────────
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = "deepseek-chat"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_TO = os.environ.get("SMTP_TO", "")

STATE_FILE = "state.json"
TRACKED_FILE = "tracked.json"
JUDGMENTS_FILE = "judgments.json"
SUBJECTS_FILE = "subjects.json"
TRENDS_FILE = "trends.json"
CAUSALITY_FILE = "causality.json"
TZ_CST = timezone(timedelta(hours=8))

_session = requests.Session()
_session.headers.update({"User-Agent": "News-Daily/1.0"})
_adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)


# ─── 工具函数 ──────────────────────────────────────────────────────────

def log(msg: str):
    print(f"[{datetime.now(TZ_CST).strftime('%H:%M:%S')}] {msg}", flush=True)


def check_network() -> bool:
    for url in ("https://www.google.com", "https://api.github.com"):
        try:
            _session.get(url, timeout=5); return True
        except requests.RequestException:
            continue
    return False


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    serializable = {}
    for k, v in state.items():
        serializable[k] = list(v) if isinstance(v, set) else v
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False)


def is_new(source: str, item_id: str, state: dict) -> bool:
    seen = state.setdefault(source, set())
    if isinstance(seen, list):
        seen = set(seen); state[source] = seen
    if item_id in seen:
        return False
    seen.add(item_id)
    return True


def clean_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()[:500]


def get_session() -> str:
    hour = datetime.now(TZ_CST).hour
    if 6 <= hour <= 8: return "morning"
    if 13 <= hour <= 15: return "afternoon"
    if 21 <= hour <= 22: return "evening"
    return "afternoon"


DEFAULT_SUBJECTS = {
    "OpenAI": {"type": "company", "country": "美国", "backed_by": "微软", "history": [], "pending": []},
    "Google": {"type": "company", "country": "美国", "backed_by": "—", "history": [], "pending": []},
    "Meta": {"type": "company", "country": "美国", "backed_by": "—", "history": [], "pending": []},
    "Anthropic": {"type": "company", "country": "美国", "backed_by": "Google/Amazon", "history": [], "pending": []},
    "DeepMind": {"type": "company", "country": "美国", "backed_by": "Google", "history": [], "pending": []},
    "Mistral": {"type": "company", "country": "法国", "backed_by": "—", "history": [], "pending": []},
    "中国网信办": {"type": "government", "country": "中国", "backed_by": "中国政府", "history": [], "pending": []},
    "美联储": {"type": "institution", "country": "美国", "backed_by": "美国政府", "history": [], "pending": []},
}


def load_json(file: str, default):
    if os.path.exists(file):
        try:
            with open(file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(file: str, data):
    with open(file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ─── 新闻追踪 ──────────────────────────────────────────────────────────

def load_tracked() -> list[dict]:
    if os.path.exists(TRACKED_FILE):
        try:
            with open(TRACKED_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_tracked(tracked: list[dict]):
    with open(TRACKED_FILE, "w", encoding="utf-8") as f:
        json.dump(tracked, f, ensure_ascii=False)


def build_tracking_report(tracked: list[dict], items: list[dict]) -> tuple[str, list[dict]]:
    """检查追踪条目是否有新进展，返回（追踪报道文本，更新后的追踪列表）"""
    if not tracked:
        return "", tracked

    updated = []
    report_parts = ["📡 **跟踪报道**"]
    has_any = False

    for t in tracked:
        keywords = t.get("keywords", [])
        last_text = t.get("last_text", "暂无")
        new_finds = []
        for it in items:
            title_lower = (it.get("title", "") + " " + it.get("summary", "")).lower()
            if any(kw.lower() in title_lower for kw in keywords):
                new_finds.append(it)

        if new_finds:
            has_any = True
            latest = new_finds[0]
            report_parts.append(
                f"\n【{t['title']}】\n"
                f"上次（{t.get('date', '?')}）：{last_text}\n"
                f"最新今日：{latest['title']}\n"
                f"来源：{latest['source']} {latest.get('url', '')}"
            )
            t["last_text"] = latest["title"][:150]
            t["date"] = datetime.now(TZ_CST).strftime("%m-%d")
        else:
            report_parts.append(
                f"\n【{t['title']}】\n"
                f"上次（{t.get('date', '?')}）：{last_text}\n"
                f"最新今日：暂无新进展"
            )
        updated.append(t)

    if not has_any:
        report_parts.append("\n所有追踪条目暂无新进展。")

    return "\n".join(report_parts), updated


# ─── 数据源 ────────────────────────────────────────────────────────────

def fetch_reddit(subreddits: list[str], limit: int = 5) -> list[dict]:
    items = []
    for sub in subreddits:
        try:
            resp = _session.get(
                f"https://www.reddit.com/r/{sub}/hot.json?limit={limit}", timeout=15
            )
            if resp.status_code != 200:
                continue
            for post in resp.json().get("data", {}).get("children", []):
                p = post["data"]
                items.append({
                    "id": f"reddit_{p['id']}",
                    "source": f"Reddit r/{sub}",
                    "title": p.get("title", ""),
                    "url": f"https://reddit.com{p.get('permalink', '')}",
                    "summary": clean_html(p.get("selftext", "") or p.get("url", "")),
                })
            log(f"  Reddit r/{sub}: ok")
        except Exception as e:
            log(f"  Reddit r/{sub}: {e}")
    return items


def fetch_hackernews_all(top_n: int = 20, ai_only: bool = False) -> list[dict]:
    items = []
    ai_keywords = ["ai", "artificial intelligence", "machine learning", "llm",
                   "gpt", "neural", "deep learning", "transformer", "openai",
                   "anthropic", "google", "meta", "llama", "gemma", "mistral",
                   "claude", "chatgpt", "diffusion", "rlhf", "rag", "agent",
                   "fine-tun", "model", "dataset", "copilot", "gemini",
                   "open source ai", "local ai", "mixture of experts",
                   "sora", "midjourney", "stable diffusion"]
    try:
        resp = _session.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json", timeout=15
        )
        if resp.status_code != 200:
            return items
        ids = resp.json()[:top_n]
        with ThreadPoolExecutor(max_workers=10) as pool:
            def get_one(sid):
                try:
                    r = _session.get(
                        f"https://hacker-news.firebaseio.com/v0/item/{sid}.json",
                        timeout=8
                    )
                    if r.status_code != 200:
                        return None
                    s = r.json()
                    if not s or s.get("type") != "story":
                        return None
                    title = (s.get("title", "") or "").lower()
                    text = ((s.get("text", "") or "")[:300]).lower()
                    if ai_only and not any(kw in (title + " " + text) for kw in ai_keywords):
                        return None
                    return {
                        "id": f"hn_{sid}",
                        "source": "Hacker News",
                        "title": s.get("title", ""),
                        "url": s.get("url", f"https://news.ycombinator.com/item?id={sid}"),
                        "summary": clean_html((s.get("text", "") or "")[:300]),
                    }
                except Exception:
                    return None
            for f in as_completed({pool.submit(get_one, sid): sid for sid in ids}):
                r = f.result()
                if r:
                    items.append(r)
                    if ai_only and len(items) >= 8:
                        break
        log(f"  Hacker News: {len(items)} 条")
    except Exception as e:
        log(f"  Hacker News: {e}")
    return items


def fetch_arxiv(categories: list[str], max_results: int = 5) -> list[dict]:
    items = []
    for cat in categories:
        try:
            resp = _session.get(
                f"http://export.arxiv.org/api/query?search_query=cat:{cat}"
                f"&sortBy=submittedDate&sortOrder=descending&max_results={max_results}",
                timeout=20,
            )
            if resp.status_code != 200:
                continue
            root = ElementTree.fromstring(resp.content)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            for entry in root.findall("atom:entry", ns):
                eid = entry.find("atom:id", ns).text.strip()
                title = (entry.find("atom:title", ns).text or "").strip()
                summary = clean_html((entry.find("atom:summary", ns).text or "").strip())[:300]
                authors = ", ".join(
                    a.find("atom:name", ns).text
                    for a in entry.findall("atom:author", ns)[:3]
                    if a.find("atom:name", ns) is not None
                )
                link = entry.find("atom:link[@rel='alternate']", ns)
                url = link.get("href", "") if link is not None else ""
                items.append({
                    "id": f"arxiv_{hashlib.md5(eid.encode()).hexdigest()[:12]}",
                    "source": f"ArXiv {cat}",
                    "title": title,
                    "url": url,
                    "summary": summary,
                    "author": authors,
                })
            log(f"  ArXiv {cat}: ok")
        except Exception as e:
            log(f"  ArXiv {cat}: {e}")
    return items


def fetch_huggingface() -> list[dict]:
    items = []
    try:
        resp = _session.get("https://huggingface.co/api/daily_papers", timeout=15)
        if resp.status_code != 200:
            return items
        papers = resp.json()
        if isinstance(papers, dict):
            papers = papers.get("papers", [])
        for paper in papers[:5]:
            pid = paper.get("id", paper.get("paperId", ""))
            title = paper.get("title", "")
            if not title:
                continue
            items.append({
                "id": f"hf_{hashlib.md5(pid.encode()).hexdigest()[:12]}",
                "source": "Hugging Face",
                "title": title,
                "url": paper.get("url", paper.get("link", "")
                                 or f"https://huggingface.co/papers/{pid}"),
                "summary": (paper.get("summary", paper.get("abstract", "")) or "")[:300],
            })
        log(f"  Hugging Face: {len(items)} 条")
    except Exception as e:
        log(f"  Hugging Face: {e}")
    return items


# ─── 新增信源 ──────────────────────────────────────────────────────────

def _extract_image(item) -> str:
    """从 RSS item 中提取图片 URL，支持多种格式"""
    img_tags = []
    # 方法1: media:thumbnail
    for child in item:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        ns = child.tag.split("}")[0].strip("{") if "}" in child.tag else ""
        if tag == "thumbnail" and "media" in ns.lower():
            url = child.get("url", "")
            if url and url.startswith("https"):
                return url
        if tag == "content" and "media" in ns.lower():
            url = child.get("url", "")
            if url and url.startswith("https") and child.get("medium") == "image":
                return url
    # 方法2: enclosure
    enc = item.find("enclosure")
    if enc is not None:
        url = enc.get("url", "")
        if url and url.startswith("https"):
            return url
    # 方法3: 从 description 的 HTML 中提取 img
    desc = item.findtext("description") or ""
    if desc:
        m = re.search(r'<img[^>]+src=["\'](https?://[^"\']+)["\']', desc)
        if m:
            url = m.group(1)
            if url.startswith("http://"):
                url = "https://" + url[7:]
            return url
    return ""


def fetch_rss_news(feeds: dict[str, str], limit: int = 5) -> list[dict]:
    items = []
    for name, url in feeds.items():
        try:
            resp = _session.get(url, timeout=15)
            if resp.status_code != 200:
                continue
            root = ElementTree.fromstring(resp.content)
            channel = root.find("channel")
            if channel is None:
                continue
            for item in channel.findall("item")[:limit]:
                title = (item.findtext("title") or "").strip()
                link = item.findtext("link") or ""
                desc = clean_html(item.findtext("description") or "")
                if not title:
                    continue
                image = _extract_image(item)
                items.append({
                    "id": f"rss_{hashlib.md5((name + title).encode()).hexdigest()[:12]}",
                    "source": name,
                    "title": title,
                    "url": link,
                    "summary": desc,
                    "image": image,
                })
            log(f"  {name}: ok")
        except Exception as e:
            log(f"  {name}: {e}")
    return items


def fetch_36kr() -> list[dict]:
    items = []
    try:
        resp = _session.get("https://36kr.com/feed", timeout=15)
        if resp.status_code != 200:
            return items
        root = ElementTree.fromstring(resp.content)
        for item in root.findall(".//item")[:5]:
            title = (item.findtext("title") or "").strip()
            link = item.findtext("link") or ""
            raw_desc = item.findtext("description") or ""
            desc = clean_html(raw_desc)
            image = ""
            m = re.search(r'<img[^>]+src=["\'](https?://[^"\']+)["\']', raw_desc)
            if m:
                image = m.group(1)
            if not title:
                continue
            items.append({
                "id": f"36kr_{hashlib.md5(title.encode()).hexdigest()[:12]}",
                "source": "36氪",
                "title": title,
                "url": link,
                "summary": desc,
                "image": image,
            })
        log(f"  36氪: {len(items)} 条")
    except Exception as e:
        log(f"  36氪: {e}")
    return items


def fetch_github_trending(days: int = 7) -> list[dict]:
    items = []
    seen_ids = set()
    try:
        # 查法1：最近7天新项目，按Star排序（已有）
        since_7d = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        # 查法2：最近30天Star>200的项目，按Star排序（发现增长快的）
        since_30d = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

        queries = [
            (f"created:>{since_7d}", 10, "新项目"),
            (f"created:>{since_30d} stars:>200", 10, "增长快"),
        ]

        for q, per_page, tag in queries:
            resp = _session.get(
                "https://api.github.com/search/repositories",
                params={"q": q, "sort": "stars", "order": "desc", "per_page": per_page},
                timeout=15,
                headers={"Accept": "application/vnd.github.v3+json"},
            )
            if resp.status_code != 200:
                log(f"  GitHub Trending({tag}): {resp.status_code}")
                continue
            for repo in resp.json().get("items", []):
                rid = repo["id"]
                if rid in seen_ids:
                    continue
                seen_ids.add(rid)
                items.append({
                    "id": f"gh_{rid}",
                    "source": "GitHub Trending",
                    "title": f"{repo['full_name']} - {repo.get('description', '') or '无描述'}",
                    "url": repo["html_url"],
                    "summary": f"⭐ {repo['stargazers_count']} | 🍴 {repo['forks_count']} | {repo.get('language', '未知')}",
                })
        log(f"  GitHub Trending: {len(items)} 条")
    except Exception as e:
        log(f"  GitHub Trending: {e}")
    return items


def fetch_v2ex() -> list[dict]:
    items = []
    try:
        resp = _session.get("https://www.v2ex.com/api/v2/topics/hot", timeout=15,
                            headers={"User-Agent": "curl/7.0"})
        if resp.status_code != 200:
            return items
        for topic in resp.json()[:5]:
            items.append({
                "id": f"v2ex_{topic['id']}",
                "source": "V2EX",
                "title": topic.get("title", ""),
                "url": f"https://www.v2ex.com/t/{topic['id']}",
                "summary": topic.get("content", "")[:200] if topic.get("content") else "",
            })
        log(f"  V2EX: {len(items)} 条")
    except Exception as e:
        log(f"  V2EX: {e}")
    return items


# ─── 博主评测信源 ──────────────────────────────────────────────────────

def fetch_juejin() -> list[dict]:
    """掘金 - 中国开发者AI实测评测"""
    items = []
    try:
        resp = _session.get(
            "https://api.juejin.cn/content_api/v1/content/article_rank",
            params={"category_id": "6809637773935378440", "type": "hot"},
            timeout=15,
        )
        if resp.status_code == 200:
            for art in resp.json().get("data", [])[:8]:
                info = art.get("content", {})
                title = info.get("title", "") or info.get("content", "")[:80]
                if not title:
                    continue
                items.append({
                    "id": f"juejin_{info.get('content_id', '')}",
                    "source": "掘金",
                    "title": title.strip(),
                    "url": f"https://juejin.cn/post/{info.get('content_id', '')}",
                    "summary": (info.get("brief", "") or "")[:200],
                    "author": info.get("user_name", ""),
                })
        log(f"  掘金: {len(items)} 条")
    except Exception as e:
        log(f"  掘金: {e}")
    return items


def fetch_oschina() -> list[dict]:
    """开源中国 - 中国开发者实战文章"""
    items = []
    try:
        resp = _session.get("https://www.oschina.net/news/rss", timeout=15)
        if resp.status_code == 200:
            root = ElementTree.fromstring(resp.content)
            for item in root.findall(".//item")[:5]:
                title = (item.findtext("title") or "").strip()
                link = item.findtext("link") or ""
                desc = clean_html(item.findtext("description") or "")[:200]
                if not title:
                    continue
                items.append({
                    "id": f"oschina_{hashlib.md5(title.encode()).hexdigest()[:12]}",
                    "source": "开源中国",
                    "title": title,
                    "url": link,
                    "summary": desc,
                })
        log(f"  开源中国: {len(items)} 条")
    except Exception as e:
        log(f"  开源中国: {e}")
    return items


def fetch_devto() -> list[dict]:
    """Dev.to - 海外开发者AI评测"""
    items = []
    try:
        resp = _session.get(
            "https://dev.to/api/articles",
            params={"tag": "ai", "per_page": 8, "state": "rising"},
            timeout=15,
        )
        if resp.status_code == 200:
            for art in resp.json():
                items.append({
                    "id": f"devto_{art['id']}",
                    "source": "Dev.to",
                    "title": art.get("title", ""),
                    "url": art.get("url", ""),
                    "summary": clean_html((art.get("description") or "")[:200]),
                    "author": art.get("user", {}).get("name", ""),
                })
        log(f"  Dev.to: {len(items)} 条")
    except Exception as e:
        log(f"  Dev.to: {e}")
    return items


# ─── 会话调度 ──────────────────────────────────────────────────────────

SESSION_CONFIG = {
    "morning": {
        "label": "🌅 全球早报",
        "prompt_type": "morning",
        "sources_list": [
            ("BBC 世界新闻", lambda: fetch_rss_news({
                "BBC": "https://feeds.bbci.co.uk/news/world/rss.xml",
                "France24": "https://www.france24.com/en/rss",
                "TASS Russia": "https://tass.com/rss/v2.xml",
                "中国日报": "https://www.chinadaily.com.cn/rss/world_rss.xml",
                "纽约时报": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
                "卫报": "https://www.theguardian.com/world/rss",
            })),
            ("中文热点", lambda: fetch_36kr() + fetch_v2ex()),
            ("Reddit 全球讨论", lambda: fetch_reddit(["worldnews", "news"], 5)),
        ],
    },
    "afternoon": {
        "label": "☀️ 午间技术",
        "prompt_type": "afternoon",
        "sources_list": [
            ("AI前沿", lambda: (
                fetch_arxiv(["cs.AI", "cs.LG", "cs.CL"], 5)
                + fetch_huggingface()
            )),
            ("开发者讨论", lambda: fetch_hackernews_all(30, ai_only=True)),
            ("中文博主评测", lambda: fetch_juejin() + fetch_oschina()),
            ("海外博主评测", lambda: fetch_devto()),
            ("开源项目", lambda: fetch_github_trending(7)),
        ],
    },
    "evening": {
        "label": "🌙 晚间速览",
        "prompt_type": "evening",
        "sources_list": [
            ("音乐推荐", lambda: fetch_rss_news({
                "Pitchfork": "https://pitchfork.com/feed/feed-album-reviews/rss",
            }, limit=5)),
            ("艺术发现", lambda: fetch_rss_news({
                "Open Culture": "https://www.openculture.com/category/art/feed",
            }, limit=3)),
            ("哲学研究", lambda: fetch_rss_news({
                "Aeon": "https://aeon.co/feed.rss",
                "Daily Nous": "https://dailynous.com/feed/",
            }, limit=3)),
            ("文学动态", lambda: fetch_rss_news({
                "卫报书籍": "https://www.theguardian.com/books/rss",
                "伦敦书评": "https://www.lrb.co.uk/rss",
            }, limit=3)),
            ("动物保护", lambda: fetch_rss_news({
                "Mongabay": "https://news.mongabay.com/feed/",
                "卫报野生动物": "https://www.theguardian.com/environment/wildlife/rss",
            }, limit=3)),
            ("36氪", lambda: fetch_36kr()),
        ],
    },
}


DEEPSEEK_PROMPTS = {
    "morning": {
        "system": """你是全球早报编辑。输出固定格式：每条以【来源名称】开头。

1. 每条只写1-2句中文总结
2. 每条标注：
   a) 重要程度：★★★★★ 越高越重要
   b) 时间标签：【今日/近3日/更早】
   c) 各界评论：原文有相关人物的表态就提取
   d) 信源背景：★★★★以上新闻加一段，分析这条消息是谁报道的、报道方的立场倾向、为什么这条今天会出现
   e) 社会观察：★★★半及以上的新闻加一段人道主义分析
3. 不写开场白结束语，不涉及AI/科技

最后，在正式内容结束后，输出一段【数据更新】JSON：
{
  "subjects": {"主体名": {"action":"做了什么","date":"日期"}},
  "causality": [{"chain":"因果描述","nodes":["事件A","事件B"]}]
}
如果没有新数据，对应字段返回空数组。""",
        "user": """请将以下全球资讯按【来源名称】分段整理：

{raw}

输出格式：来源分段 + 重要程度/时间标签/各界评论/信源背景/社会观察 + 【数据更新】JSON。""",
    },
    "afternoon": {
        "system": """你是午间技术日报编辑。输出固定格式：前言→分段→后记。

1. 【前言】：一句话总结今天AI圈最值得关注的趋势
2. 每条以【来源名称】开头，包含：
   a) 技术内容
   b) 发展阶段：【商用中/科研阶段/企业内测】
   c) 谁在干：国家/公司/资本方
   d) 基层体验：博主/用户的第一手感受（有则写）
   e) 争议点：行业内不同看法（有则写）
   f) 重要程度：★★★★★ 越高越重要
   g) 时间标签：【今日/近3日/更早】
3. 智能体和各家大模型对比要单独说
4. 生僻名词首次出现加注解：（名词：一句话讲清）
5. 最后一条【后记】

分析要求：
- 从新闻中提取隐含的判断（"如果…那么…"）并标注验证周期（短/中/长）
- 跟踪预设主体的最新动态和承诺兑现情况
- 识别事件之间的因果链

最后，在正式内容结束后，输出一段【数据更新】JSON：
{
  "judgments": [{"content":"判断内容","term":"短/中/长","source":"来源"}],
  "subjects": {"主体名": {"action":"做了什么","date":"日期","pending":"待履行承诺"}},
  "causality": [{"chain":"因果描述","nodes":["事件A","事件B"]}],
  "trends": [{"theme":"趋势主题","importance":4-5,"evidence":"证据","grassroots":"基层声音"}]
}
如果没有新数据，对应字段返回空数组。

输出示例：
【前言】
今天Anthropic发布了新版Claude，编程能力上了一个台阶。

【AI前沿】
DeepMind提出了新的强化学习方法，让AI在复杂任务中达到95%成功率。
发展阶段：科研阶段
谁在干：美国Google
基层体验：暂无
争议点：有人认为算力消耗过大
重要程度：★★★★☆
时间标签：今日

【后记】
一边是巨头拼模型，一边是开发者用脚投票。""",
        "user": """请将以下技术资讯整理成午间技术日报：

{raw}

输出格式：前言→分段【来源名称】→后记。每条包含技术内容/发展阶段/谁在干/基层体验/争议点/重要程度/时间标签。跟踪新闻追踪有则加【跟踪报道】。最后输出【数据更新】JSON。""",
    },
    "evening": {
        "system": """你是晚报编辑。输出固定格式。

固定板块顺序（必须严格执行）：
【前言】
【音乐推荐】
【艺术发现】
【哲学研究】
【文学动态】
【动物保护】
【后记】

每个板块详细要求：

🎵 **音乐推荐**（选2条）：
- 艺术家简介 + 他在音乐圈内评价如何、圈外听众怎么看
- 推荐曲目风格
- 每条附来源

🎨 **艺术发现**（选1-2条）：
- 作品创作背景 + 它是怎么来的
- 背后的故事
- 绘画/雕塑/建筑…均可，新旧不限

📚 **哲学研究**（选1-2条）：
- 哲学家的结论是什么
- 他从什么角度切入思考
- 他经历了什么才做这个研究

📚 **文学动态**（选1-2条）：
- 作者想表达什么
- 写作目的
- 文学家们在关注什么情感世界

🐘 **动物保护**（选1-2条）：
- 陈述事实：哪个地区、什么动物、什么情况
- 人类发展压力 vs 动物生存压力的冲突

通用规则：
- 每条详细但不术语，像朋友介绍一样
- 来源标注在每条末尾
- 最后一条【后记】一句话收尾

输出示例：
【前言】
今晚有不错的音乐和哲学内容。

【音乐推荐】
Floating Points — Cascade（电子）。这位英国电子音乐人在古典和电子之间游走得非常自如，业内称他为"当代电子乐最细致的编排者"。这张专辑以复杂节奏和温暖音色见长，适合安静聆听。
来源：Pitchfork 8.5/10

【艺术发现】
梵高《星月夜》— 1889年创作于圣雷米精神病院。这幅画是梵高在病中透过窗户看到的夜景。旋转的星空和宁静的村庄形成强烈对比，是后印象派的代表作。
来源：Open Culture

【哲学研究】
牛津大学哲学家在Aeon发表文章，探讨"慢思考"在AI时代的意义。他认为越是被算法推着走，越需要主动放慢节奏来保持独立思考能力。
来源：Aeon

【文学动态】
卫报书评分析了今年布克奖入围作品，多位作家关注的主题是"流离失所"——人在全球化时代的归属感失落。
来源：卫报书籍

【动物保护】
Mongabay报道：非洲象在博茨瓦纳数量回升至13万头，但中非地区的盗猎仍然严重。经济发展和栖息地保护的矛盾始终是核心难题。
来源：Mongabay

【后记】
今天换个脑子，听听音乐看看画。""",
        "user": """请将以下资讯整理成晚报：

{raw}

严格执行固定顺序：
前言 → 【音乐推荐】×2条 → 【艺术发现】×1-2条 → 【哲学研究】×1-2条 → 【文学动态】×1-2条 → 【动物保护】×1-2条 → 后记

每个板块按上面要求的详细程度写。没有相关内容就写"今日暂无推荐"。跟踪新闻追踪有则加【跟踪报道】。最后输出【数据更新】JSON。""",
    },
}


def build_prompt(session_type: str, items: list[dict],
                 tracked: list[dict] | None = None,
                 judgments: list | None = None,
                 subjects: dict | None = None,
                 causality: list | None = None) -> tuple[str, str]:
    by_source = {}
    for it in items:
        by_source.setdefault(it["source"], []).append(it)
    sections = []
    for src, src_items in sorted(by_source.items()):
        sections.append(f"=== {src} ===")
        for it in src_items:
            sections.append(f"- {it['title']}")
            if it.get("summary"):
                sections.append(f"  详情: {it['summary'][:200]}")
            if it.get("url"):
                sections.append(f"  URL: {it['url']}")
            if it.get("author"):
                sections.append(f"  作者: {it['author']}")
        sections.append("")
    raw = "\n".join(sections)

    # 追加跟踪上下文
    if tracked:
        parts = ["", "=== 跟踪新闻追踪 ==="]
        for t in tracked:
            parts.append(f"追踪主题：{t['title']}")
            parts.append(f"上次报道：{t.get('last_text', '暂无')}")
            for it in items:
                combined = (it.get("title", "") + " " + it.get("summary", "")).lower()
                if any(kw.lower() in combined for kw in t.get("keywords", [])):
                    parts.append(f"- 匹配到的今日内容：{it['title']}")
                    parts.append(f"  来源：{it['source']} {it.get('url', '')}")
                    break
            parts.append("")
        raw += "\n".join(parts)

    # 追加判断台账上下文
    if judgments and judgments[-30:]:
        parts = ["", "=== 待验证判断 ==="]
        for j in judgments[-15:]:
            if j.get("status") in ("pending", "超期未兑现"):
                parts.append(f"- 判断：{j['content']}（{j['date']}，周期:{j.get('term','中')}）")
        if len(parts) > 1:
            raw += "\n".join(parts)

    # 追加主体档案上下文
    if subjects:
        parts = ["", "=== 主体档案 ==="]
        for name, info in sorted(subjects.items()):
            if info.get("history"):
                last = info["history"][-1]
                parts.append(f"- {name}（{info.get('country','?')} {info.get('type','?')}）：最近动态 → {last.get('content','')[:100]}")
        if len(parts) > 1:
            raw += "\n".join(parts)

    prompts = DEEPSEEK_PROMPTS.get(session_type, DEEPSEEK_PROMPTS["evening"])
    system_prompt = prompts["system"]
    user_prompt = prompts["user"].format(raw=raw)
    return system_prompt, user_prompt


# ─── HTML 构建（按来源分段+插图片） ─────────────────────────────────

COUNTRY_EMOJIS = {
    "BBC": "🇬🇧", "France24": "🇫🇷", "TASS Russia": "🇷🇺",
    "中国日报": "🇨🇳", "纽约时报": "🇺🇸", "36氪": "🇨🇳",
    "Hacker News": "🌐", "ArXiv": "📄", "Hugging Face": "🤗",
    "GitHub Trending": "⭐", "Reddit": "💬", "V2EX": "💬",
}

SOURCE_LABELS = {
    "BBC": "BBC 英国", "France24": "France24 法国", "TASS Russia": "TASS 俄罗斯",
    "中国日报": "中国日报", "纽约时报": "纽约时报", "36氪": "36氪",
    "V2EX": "V2EX", "Reddit": "Reddit",
}


def parse_source_sections(text: str) -> list[tuple[str, str]]:
    """从 DeepSeek 输出中解析 【来源名称】 段落"""
    pattern = r'【([^】]+)】\s*(.*?)(?=\n【|$)'
    matches = re.findall(pattern, text.strip(), re.DOTALL)
    if matches:
        return [(s.strip(), c.strip()) for s, c in matches]
    return []


def build_html_with_images(deepseek_text: str, items: list[dict],
                            session: str, now_str: str) -> str:
    label_map = {"morning": "🌅 全球早报", "afternoon": "☀️ 午间技术", "evening": "🌙 晚间速览"}
    title = label_map.get(session, "前沿日报")

    # 按来源整理图片
    images_by_source = {}
    for it in items:
        img = it.get("image", "")
        if img:
            images_by_source.setdefault(it["source"], []).append(img)

    # 解析 DeepSeek 输出
    sections = parse_source_sections(deepseek_text)

    html = [f"<div style='font-family:-apple-system,sans-serif;padding:10px;color:#222;max-width:600px'>"]
    html.append(f"<h2 style='margin:0;font-size:20px'>{title}</h2>")
    html.append(f"<p style='color:#888;font-size:13px;margin:4px 0 12px'>{now_str}</p>")
    html.append("<hr style='border:1px solid #eee'>")

    if sections:
        for src_name, content in sections:
            escaped = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            lines = escaped.replace("\n", "<br>")
            emoji = COUNTRY_EMOJIS.get(src_name, "📌")
            html.append(
                f"<div style='margin-bottom:14px;padding-bottom:10px;"
                f"border-bottom:1px solid #f0f0f0'>"
            )
            html.append(f"<p style='margin:0 0 4px;font-size:14px;line-height:1.6'>{lines}</p>")
            # 插入该来源的图片
            imgs = images_by_source.get(src_name, [])
            for img_url in imgs[:2]:
                html.append(
                    f"<img src='{img_url}' style='max-width:100%;height:auto;"
                    f"border-radius:6px;margin:4px 0' loading='lazy'>"
                )
            html.append("</div>")
    else:
        # 回退：如果解析不到【来源】，直接用全文
        safe = deepseek_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        html.append(f"<p style='font-size:14px;line-height:1.6'>{safe.replace(chr(10), '<br>')}</p>")

    html.append(
        f"<p style='color:#bbb;font-size:11px;text-align:center;margin-top:15px'>"
        f"Powered by DeepSeek</p></div>"
    )
    return "\n".join(html)


# ─── DeepSeek ──────────────────────────────────────────────────────────

def call_deepseek(system_prompt: str, user_prompt: str) -> Optional[str]:
    if not DEEPSEEK_API_KEY:
        log("  [跳过] 未配置 DEEPSEEK_API_KEY")
        return None
    try:
        resp = _session.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": DEEPSEEK_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 8000,
            },
            timeout=60,
        )
        if resp.status_code != 200:
            log(f"  DeepSeek: {resp.status_code} {resp.text[:200]}")
            return None
        content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        return content.strip()
    except Exception as e:
        log(f"  DeepSeek: {e}")
        return None


# ─── 推送 ──────────────────────────────────────────────────────────────

def send_pushplus(message: str, is_html: bool = False) -> bool:
    if not PUSHPLUS_TOKEN:
        return False
    try:
        resp = _session.post("https://www.pushplus.plus/send", json={
            "token": PUSHPLUS_TOKEN,
            "title": f"前沿日报 {datetime.now(TZ_CST).strftime('%H:%M')}",
            "content": message,
            "template": "html" if is_html else "txt",
        }, timeout=15)
        if resp.status_code == 200 and resp.json().get("code") == 200:
            log("  PushPlus 推送成功")
            return True
        log(f"  PushPlus 返回异常: {resp.json()}")
    except Exception as e:
        log(f"  PushPlus: {e}")
    return False


def send_email(message: str) -> bool:
    if not all([SMTP_USER, SMTP_PASS, SMTP_TO]):
        return False
    try:
        msg = MIMEText(message, "plain", "utf-8")
        msg["Subject"] = f"前沿日报 {datetime.now(TZ_CST).strftime('%m-%d %H:%M')}"
        msg["From"] = SMTP_USER
        msg["To"] = SMTP_TO
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        log("  QQ邮箱 推送成功")
        return True
    except Exception as e:
        log(f"  QQ邮箱: {e}")
    return False


def send_telegram(message: str, is_html: bool = False):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            resp = _session.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                       "parse_mode": "Markdown", "disable_web_page_preview": True},
                timeout=15,
            )
            if resp.status_code == 200:
                log("  Telegram 推送成功")
                return
            log(f"  Telegram: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            log(f"  Telegram: {e}")

    if PUSHPLUS_TOKEN and send_pushplus(message, is_html):
        return
    if all([SMTP_USER, SMTP_PASS, SMTP_TO]) and send_email(message):
        return

    log("  [跳过] 未配置推送，以下为预览")
    safe = message.encode("utf-8", errors="replace").decode(
        sys.stdout.encoding or "utf-8", errors="replace"
    )
    try:
        print("\n" + "=" * 50 + "\n" + safe + "\n" + "=" * 50 + "\n")
    except Exception:
        log(f"  (预览长度: {len(message)} 字符)")


def parse_data_update(report: str) -> dict:
    """从 DeepSeek 输出中解析【数据更新】JSON"""
    import re
    m = re.search(r'【数据更新】\s*(\{.*\})', report, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


def merge_subjects(existing: dict, updates: dict) -> dict:
    if not updates:
        return existing
    for name, info in updates.items():
        if name not in existing:
            existing[name] = {"type": "auto", "country": "?", "backed_by": "?", "history": [], "pending": []}
        if "history" not in existing[name]:
            existing[name]["history"] = []
        if info.get("action"):
            existing[name]["history"].append({
                "date": info.get("date", "?") or "?",
                "content": info["action"]
            })
        if info.get("pending"):
            existing[name]["pending"] = [info["pending"]]
    return existing


def merge_causality(existing: list, updates: list) -> list:
    if not updates:
        return existing
    existing.extend(updates)
    return existing[-50:]


def merge_judgments(existing: list, updates: list) -> list:
    if not updates:
        return existing
    for j in updates:
        j["status"] = "pending"
        j["date"] = datetime.now(TZ_CST).strftime("%Y-%m-%d")
        existing.append(j)
    return existing[-100:]


def merge_trends(existing: list, updates: list) -> list:
    if not updates:
        return existing
    for t in updates:
        imp = t.get("importance", 0)
        if isinstance(imp, str):
            imp = len(imp)
        if isinstance(imp, int) and imp >= 4:
            t["date"] = datetime.now(TZ_CST).strftime("%Y-%m-%d")
            existing.append(t)
    return existing[-200:]


# ─── 主流程 ────────────────────────────────────────────────────────────

def main():
    session = get_session()
    config = SESSION_CONFIG[session]
    now_str = datetime.now(TZ_CST).strftime("%Y-%m-%d %H:%M")

    log("=" * 40)
    log(f"{config['label']} - {now_str}")

    if not check_network():
        log("无网络连接，跳过")
        sys.exit(0)

    state = load_state()
    tracked = load_tracked()
    judgments = load_json(JUDGMENTS_FILE, [])
    subjects = load_json(SUBJECTS_FILE, {})
    if not subjects:
        subjects = dict(DEFAULT_SUBJECTS)
    trends = load_json(TRENDS_FILE, [])
    causality = load_json(CAUSALITY_FILE, [])

    all_items = []
    for src_name, src_fn in config["sources_list"]:
        log(f">>> {src_name}...")
        try:
            items = src_fn()
            log(f"  [{src_name}] {len(items)} 条")
            all_items.extend(items)
        except Exception as e:
            log(f"  [{src_name}] 异常: {e}")

    if not all_items:
        log("未获取到内容")
        save_state(state); save_tracked(tracked); save_json(JUDGMENTS_FILE, judgments)
        save_json(SUBJECTS_FILE, subjects); save_json(TRENDS_FILE, trends); save_json(CAUSALITY_FILE, causality)
        return

    new_items = [it for it in all_items if is_new(it["source"], it["id"], state)]
    log(f"共 {len(all_items)} 条, 新内容 {len(new_items)} 条")

    if not new_items:
        log("无新内容")
        save_state(state); save_tracked(tracked); save_json(JUDGMENTS_FILE, judgments)
        save_json(SUBJECTS_FILE, subjects); save_json(TRENDS_FILE, trends); save_json(CAUSALITY_FILE, causality)
        return

    sources_count = len(set(it["source"] for it in new_items))

    log(">>> 调用 DeepSeek...")
    sys_prompt, usr_prompt = build_prompt(config["prompt_type"], new_items, tracked,
                                          judgments, subjects, causality)
    report = call_deepseek(sys_prompt, usr_prompt)

    if report:
        # 更新追踪状态
        tracked = build_tracking_report(tracked, new_items)[1]

        # 解析【数据更新】
        data = parse_data_update(report)
        if data:
            subjects = merge_subjects(subjects, data.get("subjects"))
            causality = merge_causality(causality, data.get("causality"))
            judgments = merge_judgments(judgments, data.get("judgments"))
            trends = merge_trends(trends, data.get("trends"))

        # 去掉数据更新部分，只推送正文
        clean_report = report.split("【数据更新】")[0].strip()

        has_images = session == "morning" and any(it.get("image") for it in new_items)
        if has_images:
            log(">>> 构建图文版...")
            html = build_html_with_images(clean_report, new_items, session, now_str)
            header = f"📊 {sources_count} 个信源 | {len(new_items)} 条\n"
            send_telegram(header + html, is_html=True)
        else:
            msg = (
                f"{config['label']}\n"
                f"📅 {now_str}\n"
                f"📊 {sources_count} 个信源 | {len(new_items)} 条\n"
                f"{'─' * 40}\n"
                f"{clean_report}\n\n"
                f"{'—' * 30}\nPowered by DeepSeek"
            )
            send_telegram(msg)
    else:
        log("DeepSeek 未返回，推送原始内容")
        raw = f"{config['label']} (原始)\n{now_str}\n\n"
        for it in new_items[:10]:
            raw += f"• [{it['source']}] {it['title']}\n  {it['url']}\n"
        send_telegram(raw)

    save_state(state)
    save_tracked(tracked)
    save_json(JUDGMENTS_FILE, judgments)
    save_json(SUBJECTS_FILE, subjects)
    save_json(TRENDS_FILE, trends)
    save_json(CAUSALITY_FILE, causality)
    log("✓ 完成")


if __name__ == "__main__":
    main()
