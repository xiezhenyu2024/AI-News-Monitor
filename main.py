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


def build_tracking_report(tracked: list[dict], items: list[dict], append_mode: bool = False) -> tuple[str, list[dict]]:
    """检查追踪条目是否有新进展，返回（追踪报道文本，更新后的追踪列表）
    append_mode=True 时只输出有新进展的条目（供追加到推送时使用）"""
    log(f"[追踪诊断] 追踪条目数: {len(tracked)}, 待检新闻数: {len(items)}")
    for ti, t in enumerate(tracked):
        log(f"[追踪诊断]   条目{ti+1}: 「{t['title']}」 session={t.get('session','?')} 关键词={t.get('keywords',[])}")
    if not tracked:
        log("[追踪诊断] 追踪列表为空，跳过")
        return "", tracked

    updated = []
    report_parts = ["📡 **跟踪报道**"]
    has_any = False
    total_hits = 0

    for t in tracked:
        keywords = t.get("keywords", [])
        last_text = t.get("last_text", "暂无")
        new_finds = []
        for it in items:
            title_lower = (it.get("title", "") + " " + it.get("summary", "")).lower()
            if any(kw.lower() in title_lower for kw in keywords):
                new_finds.append(it)

        log(f"[追踪诊断] 主题「{t['title']}」关键词={keywords} 命中 {len(new_finds)}/{len(items)} 条")
        if new_finds:
            log(f"[追踪诊断]   → 首条命中: {new_finds[0]['title'][:80]}")
            for nf in new_finds:
                log(f"[追踪诊断]     - [{nf['source']}] {nf['title'][:80]}")
                total_hits += 1

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
        elif not append_mode:
            report_parts.append(
                f"\n【{t['title']}】\n"
                f"上次（{t.get('date', '?')}）：{last_text}\n"
                f"最新今日：暂无新进展"
            )
        updated.append(t)

    log(f"[追踪诊断] 总命中数: {total_hits}")
    if not has_any:
        if not append_mode:
            report_parts.append("\n所有追踪条目暂无新进展。")
        log("[追踪诊断] 无任何条目命中 → 追踪报道为空")
        if append_mode:
            return "", updated
    else:
        hit_count = sum(1 for t in tracked if any(
            kw.lower() in (it.get("title","")+" "+it.get("summary","")).lower()
            for kw in t.get("keywords",[]) for it in items
        ))
        log(f"[追踪诊断] {hit_count}/{len(tracked)} 个条目有命中")

    result = "\n".join(report_parts)
    log(f"[追踪诊断] 最终追踪文本长度: {len(result)} 字符")
    return result, updated


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


def fetch_hackernews_comments(top_n: int = 10) -> list[dict]:
    """抓取 HN 上 AI 相关帖子的评论区，提取开发者实战体验"""
    items = []
    ai_keywords = ["ai", "llm", "gpt", "claude", "chatgpt", "copilot", "cursor",
                   "opencode", "agent", "coding", "prompt", "workflow", "tool"]
    try:
        resp = _session.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json", timeout=15
        )
        if resp.status_code != 200:
            return items
        ids = resp.json()[:top_n]
        stories = []
        for sid in ids:
            try:
                r = _session.get(f"https://hacker-news.firebaseio.com/v0/item/{sid}.json", timeout=8)
                if r.status_code != 200:
                    continue
                s = r.json()
                if not s or s.get("type") != "story":
                    continue
                title = (s.get("title", "") or "").lower()
                if any(kw in title for kw in ai_keywords):
                    stories.append(s)
                    if len(stories) >= 3:
                        break
            except:
                continue
        for story in stories:
            comment_ids = story.get("kids", [])[:5]
            for cid in comment_ids:
                try:
                    r = _session.get(f"https://hacker-news.firebaseio.com/v0/item/{cid}.json", timeout=8)
                    if r.status_code != 200:
                        continue
                    comment = r.json()
                    if not comment or comment.get("type") != "comment":
                        continue
                    text = comment.get("text", "") or ""
                    text_clean = re.sub(r"<[^>]+>", " ", text).strip()[:500]
                    if len(text_clean) < 30:
                        continue
                    author = comment.get("by", "anonymous")
                    items.append({
                        "id": f"hn_comment_{cid}",
                        "source": "HN讨论",
                        "title": f"{author} 在「{story.get('title','')[:40]}」下的评论",
                        "url": f"https://news.ycombinator.com/item?id={story['id']}",
                        "summary": text_clean,
                        "author": author,
                    })
                except:
                    continue
        log(f"  HN讨论: {len(items)} 条开发者评论")
    except Exception as e:
        log(f"  HN讨论: {e}")
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
    # 方法4: 从 content:encoded 中提取 img
    for child in item:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "encoded":
            enc_text = child.text or ""
            m = re.search(r'<img[^>]+src=["\'](https?://[^"\']+)["\']', enc_text)
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
            ("实战速报", lambda: fetch_devto() + fetch_juejin() + fetch_oschina() + fetch_hackernews_comments(10)),
            ("开发者讨论", lambda: fetch_hackernews_all(30, ai_only=True)),
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
   d) 信源背景：★★★★以上新闻加一段，分析这条消息是谁报道的、报道方的立场倾向
   e) 社会观察：★★★★以上新闻加一段。必须有前提（事件背景/来龙去脉），有数据（死亡/受伤/涉及人数等具体数字，并注明数据来源，如"据XX部门统计"）。只写原文中能推断出的内容，不编造原文没有的观点和分析。
3. **前提完整要求**：每条新闻必须交代清楚——发生了什么、为什么发生、背景是什么。前提信息写在正文里。读者不需要查资料就能理解。
4. **术语注解要求**：专业术语首次出现时必须加括号注解。
5. **去重要求（严格执行）**：
   - 在写任何内容之前，先把所有信源的标题扫描一遍
   - 同一个事件/新闻在不同信源中出现时，只保留一条
   - 合并写法示例：把"【BBC】哈梅内伊葬礼"和"【France24】哈梅内伊葬礼"和"【纽约时报】哈梅内伊葬礼"合并成一条【BBC/France24/纽约时报】哈梅内伊葬礼
   - 判断标准：标题核心关键词相同、报道的是同一件事，就视为重复
   - 合并时在来源名称中列出所有涉及的信源，用斜杠分隔
6. **翻译要求**：所有英文标题、英文原文、英文名称必须翻译成中文。不允许出现未经翻译的英文句子。人名、地名、公司名首次出现时保留英文原名并加括号。
7. **自检机制**：写完全部内容后对照原文逐条检查，每个判断在原文中有依据才保留，数据必须一致，找不到出处的删掉。
8. **排版要求**：
   - 每条新闻内部：正文→空一行→重要程度/时间标签→空一行→各界评论（有则写）→空一行→信源背景→空一行→社会观察
   - 不同新闻之间：空两行作为分隔
   - 不要把所有内容挤在一起
9. 不写开场白结束语，不涉及AI/科技

最后，在正式内容结束后，必须输出一段【数据更新】JSON（各字段说明：subjects=主体最新动态，包含主体做了什么和日期；causality=事件因果链，包含因果描述和涉及的节点事件。不得省略，即使无数据也要返回空数组。）
{
  "subjects": {"主体名": {"action":"做了什么","date":"日期"}},
  "causality": [{"chain":"因果描述","nodes":["事件A","事件B"]}]
}""",
        "user": """请将以下全球资讯按【来源名称】分段整理：

{raw}

输出格式：来源分段 + 重要程度/时间标签/各界评论/信源背景/社会观察 + 术语注解 + 英文全翻成中文 + 同事件去重 + 排版留空 + 【数据更新】JSON。

注意：如果原始数据中有「跟踪新闻追踪」板块，你必须将其内容翻译成中文并写入正文的对应位置。所有英文标题、摘要必须翻译为中文。

写完自检一遍。""",
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
3. **【实战速报】板块**：放在 AI前沿 和 开发者讨论 之间。选2-3条开发者真实使用经验，内容为：
   - 谁在使用什么工具/插件/skill/workflow
   - 他具体怎么用的，解决了什么问题
   - 他的真实感受和效果
   - 每条附来源
   来源优先从Dev.to、掘金、开源中国、HN讨论中取。
4. 智能体和各家大模型对比要单独说
5. 生僻名词首次出现加注解：（名词：一句话讲清）
6. 最后一条【后记】：一句话总结今天最关键的信号

7. **总结要求（严格执行）**：
   - 不要逐条罗列所有搜索结果
   - 必须将同类信息合并提炼，每个板块写3-5条经过整合的要点
   - 同一个话题在不同信源中出现时，合并成一条，注明多信源
   - 写之前先对所有素材做分类归并，再做精简总结

分析要求：
- 从新闻中提取隐含的判断（"如果…那么…"）并标注验证周期（短/中/长）
- 跟踪预设主体的最新动态和承诺兑现情况
- 识别事件之间的因果链
- **可读性要求**：遇到学术论文或技术概念，必须按前提→做法→结论的顺序写

最后，在【后记】之后，必须输出一段【数据更新】JSON，格式如下（不得省略，即使无数据也要输出空数组）：
{
  "judgments": [{"content":"判断内容","term":"短/中/长","source":"来源"}],
  "subjects": {"主体名": {"action":"做了什么","date":"日期","pending":"待履行承诺"}},
  "causality": [{"chain":"因果描述","nodes":["事件A","事件B"]}],
  "trends": [{"theme":"趋势主题","importance":4-5,"evidence":"证据","grassroots":"基层声音"}]
}

输出示例（注意：示例末尾包含完整的【数据更新】JSON，这是必须输出的部分）：
【前言】
今天AI圈最热闹的是Anthropic发布了新版Claude。

【AI前沿】
DeepMind提出了新的强化学习方法。
发展阶段：科研阶段
谁在干：美国Google
基层体验：暂无
争议点：有人认为算力消耗过大
重要程度：★★★★☆
时间标签：今日

【实战速报】
🛠️ 一位开发者分享了使用Claude Code+OpenCode双打的workflow
他让Claude Code负责架构设计，OpenCode负责代码实现，配合起来的效率比单个工具快一倍。
来源：Dev.to

🛠️ 掘金上有人总结了5个Cursor必装插件，其中CodeGraph能可视化代码依赖
来源：掘金

【开发者讨论】
HN上有开发者讨论Claude Sonnet 5的实际体验，不少人反馈编程能力提升但推理变慢。

【开源项目】
ponytail - 让AI像最懒的资深开发者一样思考。⭐ 74,339

【后记】
AI编程工具在进化，开发者也在摸索最好的用法。

【数据更新】
{
  "judgments": [{"content":"如果OpenAI持续降本降质，Codex用户可能流失到Claude","term":"中","source":"HN讨论"}],
  "subjects": {"Anthropic": {"action":"发布新版Claude，编程能力提升","date":"2026-07-05","pending":"等待更多用户实测反馈"}},
  "causality": [{"chain":"模型推理能力提升 → 编程Agent更可靠 → 开发者更依赖Agent","nodes":["模型升级","Agent可靠性提升","开发者采纳"]}],
  "trends": [{"theme":"AI编程工具进入效率优化阶段","importance":5,"evidence":"多个开发者自发分享workflow优化经验","grassroots":"开发者从'能不能用'转向'怎么用好'"}]
}""",
        "user": """请将以下技术资讯整理成午间技术日报：

{raw}

输出顺序必须严格遵守：前言 → 【AI前沿】 → 【实战速报】（2-3条开发者实战） → 【开发者讨论】 → 【开源项目】 → 后记 → 【数据更新】JSON。

【数据更新】JSON 不得省略。必须写在最后，紧接后记之后。即使没有数据，对应字段也要返回空数组。这是日报追踪机制的核心部分。

写完自检一遍，确认没有遗漏任何板块，特别是确认【数据更新】JSON 已完整输出。

注意：如果原始数据中有「跟踪新闻追踪」板块，你必须将其内容翻译成中文并写入【后记】之前。所有英文标题、摘要必须翻译为中文。

**核心要求：不要逐条罗列！必须将同类信息合并提练成3-5条总结，每条写清楚核心观点 + 来源即可。重复主题去重，原始数据中的冗余项不要写进最终日报。"""
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

🎵 **音乐推荐**（选2-3条）：
- 每天推荐2-3首，每天尽量覆盖不同语言/曲风
- 每条介绍艺术家、评价、风格

**用户音乐偏好：**
- 节奏感突出、旋律抓耳、氛围轻快明亮
- 标杆参考：《街头霸王6》杰米主题曲（The Playa）
- 语言范围：中文、英文、日文、纯音乐均可
- 核心规则：优先推荐用户未听过的、符合上述风格的音乐，不要局限在已知列表

**反偏好（不要推荐）：** 纯氛围/冥想/白噪音/无节奏铺垫的作品

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
Floating Points — Cascade（电子）。这位英国电子音乐人在古典和电子之间游走得非常自如，业内称他为"当代电子乐最细致的编排者"。这张专辑以明亮旋律和推动感见长，适合需要能量的时候听。
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
前言 → 【音乐推荐】×2-3条（按用户偏好）→ 【艺术发现】×1-2条 → 【哲学研究】×1-2条 → 【文学动态】×1-2条 → 【动物保护】×1-2条 → 后记

每个板块按上面要求的详细程度写。音乐推荐严格按用户偏好选曲，反偏好不要推荐。没有相关内容就写"今日暂无推荐"。跟踪新闻追踪有则加【跟踪报道】。最后输出【数据更新】JSON。

注意：如果原始数据中有「跟踪新闻追踪」板块，你必须将其内容翻译成中文并写入【动物保护】之后、【后记】之前。所有英文标题、摘要必须翻译为中文。""",
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

    # 按来源整理图片（去重）
    images_by_source = {}
    for it in items:
        img = it.get("image", "")
        if img:
            images_by_source.setdefault(it["source"], [])
            if img not in images_by_source[it["source"]]:
                images_by_source[it["source"]].append(img)

    # 晚间主题→信源映射（DeepSeek输出的是主题名，但图片关联的是信源名）
    EVENING_SOURCE_MAP = {
        "音乐推荐": ["Pitchfork"],
        "艺术发现": ["Open Culture"],
        "哲学研究": ["Aeon", "Daily Nous"],
        "文学动态": ["卫报书籍", "伦敦书评"],
        "动物保护": ["Mongabay", "卫报野生动物"],
    }

    def get_images(section_name: str) -> list:
        if session == "evening":
            sources = EVENING_SOURCE_MAP.get(section_name, [])
            imgs = []
            for s in sources:
                imgs.extend(images_by_source.get(s, []))
            return imgs[:2]
        return images_by_source.get(section_name, [])[:2]

    # 全局已用图片池，防止同一张图在多个来源中重复出现
    used_images: set = set()

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
            # 插入该来源的图片（去重+每节最多1张）
            imgs = get_images(src_name)
            for img_url in imgs:
                if img_url not in used_images:
                    used_images.add(img_url)
                    html.append(
                        f"<img src='{img_url}' style='max-width:100%;height:auto;"
                        f"border-radius:6px;margin:4px 0' loading='lazy'>"
                    )
                    break  # 每节只插1张图
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


# ─── 摘要卡片 + 网页存档 ───────────────────────────────────────────────

def build_summary_cards(items: list[dict], max_items: int = 20) -> list:
    """调用 DeepSeek 从新闻中选最重要的8-10条生成结构化摘要卡片"""
    if not items:
        return []
    raw = "\n".join(
        f"[{it['source']}] {it['title']}\n  摘要: {it.get('summary','')[:250]}\n  URL: {it.get('url','')}"
        for it in items[:max_items]
    )
    sys_prompt = """你是新闻摘要卡片生成器。从提供的新闻中选出最重要的8-10条，为每条生成一张结构化卡片，输出JSON数组，格式：
[{
  "title": "一句话标题，说清楚发生了什么",
  "entities": [{"name": "公司/人物/术语名", "explain": "一句话通俗解释它是什么"}],
  "summary": "2-4句核心摘要，50-150字，说清事件本身和结果",
  "relevant": "这条新闻为什么值得关注（可为空字符串）"
}]

要求：
- 只选8-10条最重要的，按重要性排序，其余不选
- 关键实体必须自动识别并解释，不得出现"某公司"而不说明它是什么
- 解释要通俗，面向不了解该领域的读者
- 摘要保持信息密度，不要废话套话
- 只输出JSON数组，不要输出其他任何内容"""
    user_prompt = f"请从以下新闻中选出最重要的8-10条并生成摘要卡片：\n\n{raw}"
    res = call_deepseek(sys_prompt, user_prompt)
    if not res:
        return []
    try:
        m = re.search(r"\[.*\]", res, re.DOTALL)
        cards = json.loads(m.group(0)) if m else []
        return cards[:10] if isinstance(cards, list) else []
    except Exception as e:
        log(f"  卡片解析失败: {e}")
        return []


def build_cards_html(now_str: str, label: str, cards: list,
                     items: list[dict], source_count: int) -> str:
    """把摘要卡片渲染成排版良好的 HTML 页面"""
    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    body = []
    body.append(f"<h1 style='margin:0 0 4px'>{esc(label)}</h1>")
    body.append(f"<p style='color:#888;font-size:13px;margin:0 0 16px'>{esc(now_str)} · {source_count}个信源 · {len(items)}条新闻</p>")
    for i, c in enumerate(cards, 1):
        ents = "".join(
            f"<li><b>{esc(e.get('name',''))}</b>：{esc(e.get('explain',''))}</li>"
            for e in c.get("entities", [])
        )
        rel = c.get("relevant", "")
        rel_html = f"<p class='rel'>与你相关：{esc(rel)}</p>" if rel else ""
        body.append(
            f"<div class='card'>"
            f"<h3>#{i} {esc(c.get('title',''))}</h3>"
            f"<div class='ents'><b>关键实体</b><ul>{ents}</ul></div>"
            f"<p>{esc(c.get('summary',''))}</p>"
            f"{rel_html}"
            f"</div>"
        )
    html = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(label)}</title>
<style>
body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;max-width:760px;margin:0 auto;padding:16px;background:#f7f7f8;color:#222}}
.card{{background:#fff;border-radius:10px;padding:16px 18px;margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
.card h3{{margin:0 0 8px;font-size:16px}}
.card p{{font-size:14px;line-height:1.7;margin:6px 0}}
.ents{{font-size:13px;color:#444;background:#f4f6fa;border-radius:6px;padding:8px 12px;margin:6px 0}}
.ents ul{{margin:4px 0 0;padding-left:18px}}
.ents b{{color:#333}}
.rel{{color:#0b57d0;font-size:13px}}
</style>
</head>
<body>{''.join(body)}
<p style='color:#bbb;font-size:11px;text-align:center;margin-top:18px'>Powered by DeepSeek · 本机问答: 运行 server.py</p>
</body></html>"""
    return html


def build_cards_push_text(label: str, now_str: str, cards: list,
                          sources_count: int) -> str:
    """把卡片渲染成微信推送文本（纯文本，PushPlus txt 模板）"""
    lines = [f"{label}", f"📅 {now_str}", f"📊 {sources_count} 个信源 | 精选 {len(cards)} 条", "─" * 40]
    for i, c in enumerate(cards, 1):
        lines.append(f"\n#{i} {c.get('title', '')}")
        ents = c.get("entities", [])
        if ents:
            lines.append("关键实体：")
            for e in ents:
                lines.append(f"· {e.get('name','')}：{e.get('explain','')}")
        lines.append(f"摘要：{c.get('summary', '')}")
        if c.get("relevant"):
            lines.append(f"与你相关：{c.get('relevant')}")
    lines.append("\n" + "─" * 30 + "\n完整长文+追问：打开网页存档")
    return "\n".join(lines)


def build_long_report_html(clean_report: str, label: str, now_str: str,
                           sources_count: int, item_count: int) -> str:
    """把完整长文日报渲染成排版良好的 HTML 页面（GitHub Pages 存档）"""
    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    body = esc(clean_report).replace("\n", "<br>")
    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(label)}</title>
<style>
body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;max-width:760px;margin:0 auto;padding:16px;background:#f7f7f8;color:#222}}
.wrap{{background:#fff;border-radius:10px;padding:20px 22px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
h1{{margin:0 0 4px;font-size:20px}}
.meta{{color:#888;font-size:13px;margin:0 0 12px}}
hr{{border:none;border-top:1px solid #eee;margin:14px 0}}
p,li{{font-size:14px;line-height:1.8}}
</style>
</head>
<body><div class="wrap">
<h1>{esc(label)}</h1>
<p class="meta">{esc(now_str)} · {sources_count}个信源 · {item_count}条新闻</p>
<hr>{body}
<p style='color:#bbb;font-size:11px;text-align:center;margin-top:18px'>Powered by DeepSeek</p>
</div></body></html>"""


def save_docs(now_str: str, session: str, label: str, cards: list,
              items: list[dict], source_count: int,
              clean_report: str = "") -> str:
    """保存卡片HTML + 长文HTML + 上下文JSON到 docs/，返回归档键名"""
    os.makedirs("docs", exist_ok=True)
    date = datetime.now(TZ_CST).strftime("%Y-%m-%d")
    key = f"{date}-{session}"
    html = build_cards_html(now_str, label, cards, items, source_count)
    with open(f"docs/report-{key}.html", "w", encoding="utf-8") as f:
        f.write(html)
    if clean_report:
        long_html = build_long_report_html(clean_report, label, now_str,
                                           source_count, len(items))
        with open(f"docs/long-{key}.html", "w", encoding="utf-8") as f:
            f.write(long_html)
    ctx = {
        "key": key,
        "date": date,
        "session": session,
        "label": label,
        "source_count": source_count,
        "cards": cards,
        "items": items,
        "generated_at": now_str,
    }
    with open(f"docs/context-{key}.json", "w", encoding="utf-8") as f:
        json.dump(ctx, f, ensure_ascii=False, indent=2)

    # 重建 index.html（主页两列网格 + 全屏单卡对话 + IndexedDB 历史）
    entries = sorted(
        f.replace("report-", "").replace(".html", "")
        for f in os.listdir("docs") if f.startswith("report-") and f.endswith(".html")
    )
    keys_json = json.dumps(list(reversed(entries)), ensure_ascii=False)
    index = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>新闻问答</title>
<style>
*{box-sizing:border-box}
body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;max-width:820px;margin:0 auto;background:#f7f7f8;color:#222}
#home{padding:14px}
#chat{display:none;height:100dvh;flex-direction:column}
.top{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
h2{margin:0;font-size:18px}
select{flex:1;min-width:180px;padding:8px;font-size:14px;border:1px solid #ddd;border-radius:8px;background:#fff}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.gcard{background:#fff;border-radius:10px;padding:12px;box-shadow:0 1px 3px rgba(0,0,0,.08);cursor:pointer;overflow:hidden}
.gcard h3{margin:0 0 6px;font-size:13px;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.gcard p{margin:0;font-size:11px;line-height:1.5;color:#555;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.gcard .tag{display:inline-block;margin-top:6px;font-size:10px;color:#0b57d0;background:#eef3fd;border-radius:4px;padding:2px 6px;max-width:100%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.loading{color:#888;font-size:13px;text-align:center;padding:30px}
/* 对话页 */
.chatbar{display:flex;align-items:center;gap:8px;padding:10px 14px;background:#fff;border-bottom:1px solid #eee;position:sticky;top:0;z-index:10}
.chatbar button{background:none;border:none;font-size:20px;cursor:pointer;color:#0b57d0;padding:4px 8px}
.chatbar .ctitle{font-size:13px;color:#888;flex:1;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.cardinfo{padding:12px 14px;background:#fff;border-bottom:1px solid #eee;max-height:38dvh;overflow-y:auto}
.cardinfo h3{margin:0 0 6px;font-size:15px}
.cardinfo p{font-size:12px;line-height:1.6;margin:4px 0}
.cardinfo .ent{font-size:12px;color:#444;background:#f4f6fa;border-radius:6px;padding:6px 10px;margin:4px 0}
.cardinfo .rel{color:#0b57d0;font-size:12px}
#msgs{flex:1;overflow-y:auto;padding:12px 14px}
.msg{margin:8px 0;padding:10px 12px;border-radius:8px;font-size:14px;line-height:1.7;white-space:pre-wrap;max-width:92%}
.user{background:#0b57d0;color:#fff;margin-left:auto;border-bottom-right-radius:2px}
.ai{background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.06);border-bottom-left-radius:2px}
.inputrow{display:flex;gap:8px;padding:10px 14px;background:#fff;border-top:1px solid #eee}
input{flex:1;padding:10px;font-size:14px;border:1px solid #ddd;border-radius:8px}
button{padding:10px 18px;font-size:14px;background:#0b57d0;color:#fff;border:none;border-radius:8px;cursor:pointer}
.hint{color:#888;font-size:12px;margin:4px 0 10px}
.sbar{margin-bottom:12px}
</style>
</head>
<body>
<div id="home">
  <div class="top">
    <h2>📰 新闻问答</h2>
    <select id="pick" onchange="loadGrid()"></select>
  </div>
  <div class="sbar">
    <input id="skw" placeholder="🔍 搜索新闻（输入关键词补充查询）" onkeydown="if(event.key==='Enter')doSearch()">
  </div>
  <div id="sres" class="hint"></div>
  <div id="grid" class="grid"></div>
</div>

<div id="chat">
  <div class="chatbar">
    <button onclick="goHome()">←</button>
    <span class="ctitle" id="ctitle"></span>
  </div>
  <div class="cardinfo" id="cardinfo"></div>
  <div id="msgs"></div>
  <div class="inputrow">
    <input id="q" placeholder="问这条新闻…" onkeydown="if(event.key==='Enter')ask()">
    <button onclick="ask()">发送</button>
  </div>
</div>

<script>
const KEYS = __KEYS__;
const API = "https://1471673999-3ypqvp16ev.ap-guangzhou.tencentscf.com";
let ctx = null;          // 当前期 context
let curCard = null;      // 当前对话的卡片 {ctxKey, idx}
let curMsgs = [];        // 当前卡对话历史

// ── IndexedDB：每卡独立对话历史，保留2年 ──
let db = null;
function openDB(){
  return new Promise((resolve)=>{
    const req = indexedDB.open('newsqa_chat', 1);
    req.onupgradeneeded = (e)=>{
      const d = e.target.result;
      if(!d.objectStoreNames.contains('threads')){
        d.createObjectStore('threads', {keyPath:'id'});
      }
    };
    req.onsuccess = ()=>{ db = req.result; resolve(); };
    req.onerror = ()=>{ db = null; resolve(); };
  });
}
function threadId(ctxKey, idx){ return ctxKey + '|' + idx; }
function loadThread(id){
  return new Promise((resolve)=>{
    if(!db){ resolve([]); return; }
    const r = db.transaction('threads','readonly').objectStore('threads').get(id);
    r.onsuccess = ()=> resolve((r.result && r.result.msgs) || []);
    r.onerror = ()=> resolve([]);
  });
}
function saveThread(id, msgs){
  if(!db) return;
  const tx = db.transaction('threads','readwrite');
  const st = tx.objectStore('threads');
  st.put({id, msgs, updated: Date.now()});
  // 自动清理2年前的记录
  const cutoff = Date.now() - 730*24*3600*1000;
  const all = st.openCursor();
  all.onsuccess = (e)=>{
    const c = e.target.result;
    if(c){
      if(c.value.updated < cutoff) c.delete();
      c.continue();
    }
  };
}

// ── 主页：两列网格 ──
async function loadGrid(){
  const key = document.getElementById('pick').value;
  if(!key) return;
  const box = document.getElementById('grid');
  box.innerHTML = '<div class="loading">加载中…</div>';
  try{
    const r = await fetch('context-' + key + '.json', {cache:'no-store'});
    if(!r.ok) throw new Error('HTTP ' + r.status);
    ctx = await r.json();
    if(!ctx.cards || !ctx.cards.length){ box.innerHTML = '<div class="loading">该期无卡片数据</div>'; return; }
    box.innerHTML = ctx.cards.map((c,i)=>{
      const tag = (c.entities && c.entities[0]) ? c.entities[0].name : (c.relevant || '');
      const tagTxt = tag ? '<span class="tag">' + esc(tag) + '</span>' : '';
      return `<div class="gcard" onclick="openChat(${i})"><h3>${esc(c.title)}</h3><p>${esc(c.summary)}</p>${tagTxt}</div>`;
    }).join('');
  }catch(e){
    box.innerHTML = '<div class="loading">加载失败: ' + esc(e.message) + '</div>';
  }
}
function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// ── 对话页 ──
async function openChat(idx){
  if(!ctx || !ctx.cards[idx]) return;
  curCard = {ctxKey: ctx.key, idx};
  const c = ctx.cards[idx];
  document.getElementById('home').style.display = 'none';
  document.getElementById('chat').style.display = 'flex';
  document.getElementById('ctitle').textContent = (idx+1) + '/' + ctx.cards.length + ' · ' + ctx.key;
  const ents = (c.entities||[]).map(e=>`<div class="ent"><b>${esc(e.name)}</b>：${esc(e.explain)}</div>`).join('');
  const rel = c.relevant ? `<p class="rel">与你相关：${esc(c.relevant)}</p>` : '';
  document.getElementById('cardinfo').innerHTML =
    `<h3>#${idx+1} ${esc(c.title)}</h3>${ents}<p>${esc(c.summary)}</p>${rel}`;
  // 恢复该卡历史
  curMsgs = await loadThread(threadId(curCard.ctxKey, curCard.idx));
  const box = document.getElementById('msgs');
  box.innerHTML = '';
  curMsgs.forEach(m=>addMsg(m.role, m.content, false));
  box.scrollTop = box.scrollHeight;
}
function goHome(){
  document.getElementById('chat').style.display = 'none';
  document.getElementById('home').style.display = '';
  curCard = null;
}
function addMsg(role, text, save){
  const d = document.createElement('div');
  d.className = 'msg ' + role;
  d.textContent = text;
  document.getElementById('msgs').appendChild(d);
  document.getElementById('msgs').scrollTop = document.getElementById('msgs').scrollHeight;
  if(save && curCard){
    curMsgs.push({role, content:text});
    saveThread(threadId(curCard.ctxKey, curCard.idx), curMsgs);
  }
}
async function ask(){
  const q = document.getElementById('q').value.trim();
  if(!q || !curCard){ return; }
  addMsg('user', q, true);
  document.getElementById('q').value = '';
  const key = curCard.ctxKey;
  const history = curMsgs.slice(-40).map(m=>({role:m.role, content:m.content}));
  try{
    const r = await fetch(API + '/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key,question:q,history})});
    if(!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    addMsg('ai', d.answer || '（无回答）', true);
  }catch(e){
    addMsg('ai', '提问失败: ' + e.message + '，网络不稳定时请稍后重试', true);
  }
}

// ── 搜索 ──
async function doSearch(){
  const kw = document.getElementById('skw').value.trim();
  if(!kw) return;
  const box = document.getElementById('sres');
  box.innerHTML = '搜索中…';
  try{
    const r = await fetch(API + '/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({keyword:kw})});
    if(!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    box.innerHTML = d.cards.length
      ? d.cards.map((c,i)=>`<div class="gcard" style="margin-bottom:8px" onclick="window.open('${c.url||'#'}','_blank')"><h3>${esc(c.title)}</h3><p>${esc(c.summary)}</p></div>`).join('')
      : '未找到相关内容';
  }catch(e){
    box.innerHTML = '搜索失败: ' + esc(e.message);
  }
}

(async function init(){
  await openDB();
  const sel = document.getElementById('pick');
  sel.innerHTML = KEYS.map(k=>`<option value="${k}">${k}</option>`).join('');
  if(KEYS.length) loadGrid();
  else document.getElementById('grid').innerHTML = '<div class="loading">暂无新闻数据，请等下一期日报生成</div>';
})();
</script>
</body></html>"""
    index = index.replace("__KEYS__", keys_json)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(index)
    log(f"  已存档: docs/report-{key}.html + long-{key}.html + context-{key}.json")
    return key


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


def send_telegram(message: str, is_html: bool = False, fallback_pushplus: bool = True):
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

    if fallback_pushplus and PUSHPLUS_TOKEN and send_pushplus(message, is_html):
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
        # 更新追踪状态并获取追踪报道文本
        log(f"[追踪] 开始构建追踪报道: tracked={len(tracked)}条, new_items={len(new_items)}条")
        track_text, tracked = build_tracking_report(tracked, new_items, append_mode=True)
        # 拆出只有命中的条目文本（append_mode=True 时只输出命中的条目）
        track_hits = track_text.strip() if "📡" in track_text else ""
        log(f"[追踪] track_text 空={not track_text} | track_hits 空={not track_hits}")

        # 解析【数据更新】
        data = parse_data_update(report)
        if data:
            subjects = merge_subjects(subjects, data.get("subjects"))
            causality = merge_causality(causality, data.get("causality"))
            judgments = merge_judgments(judgments, data.get("judgments"))
            trends = merge_trends(trends, data.get("trends"))

        # 去掉数据更新部分，只推送正文；再追加追踪报道（作为最后手段）
        clean_report = report.split("【数据更新】")[0].strip()
        if track_hits and "📡 **跟踪报道**" not in clean_report:
            # 只取有命中的条目标题（中文），去掉英文的具体标题
            short_hits = []
            for line in track_hits.split("\n"):
                if line.startswith("【"):
                    short_hits.append(f"📌 {line.strip('【】')}：有新的相关报道")
            if short_hits:
                clean_report += "\n\n📡 **跟踪报道**\n" + "\n".join(short_hits)
                log(f"[追踪] 已追加追踪报道摘要（{len(short_hits)}条, 仅中文标题）")
        elif track_hits:
            log(f"[追踪] 追踪报道已由模型包含在正文中，跳过追加（{len(track_hits)}字符）")
        else:
            log("[追踪] 无命中条目，不追加追踪报道")

        has_images = session == "morning" and any(it.get("image") for it in new_items)
        if has_images:
            log(">>> 构建图文版...")
            html = build_html_with_images(clean_report, new_items, session, now_str)
            header = f"📊 {sources_count} 个信源 | {len(new_items)} 条\n"
            send_telegram(header + html, is_html=True, fallback_pushplus=False)
        else:
            msg = (
                f"{config['label']}\n"
                f"📅 {now_str}\n"
                f"📊 {sources_count} 个信源 | {len(new_items)} 条\n"
                f"{'─' * 40}\n"
                f"{clean_report}\n\n"
                f"{'—' * 30}\nPowered by DeepSeek"
            )
            send_telegram(msg, fallback_pushplus=False)

        # 保存报告供审核
        if clean_report:
            with open("last_report.txt", "w", encoding="utf-8") as f:
                f.write(f"{config['label']}\n{now_str}\n{sources_count}个信源|{len(new_items)}条\n{'─'*40}\n{clean_report}")

        # 生成摘要卡片（第二次独立调用 DeepSeek，选8-10条最重要）
        log(">>> 生成摘要卡片...")
        cards = build_summary_cards(new_items)
        log(f"  卡片数: {len(cards)}")
        if cards:
            # 微信推送卡片（替代长文推送到微信）
            card_msg = build_cards_push_text(config["label"], now_str, cards, sources_count)
            send_pushplus(card_msg)
            log("  卡片已推送到微信")

            # 存档：卡片HTML + 长文HTML + context JSON
            save_docs(now_str, session, config["label"], cards, new_items,
                      sources_count, clean_report)
        else:
            log("  卡片生成为空，跳过存档和卡片推送")
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
