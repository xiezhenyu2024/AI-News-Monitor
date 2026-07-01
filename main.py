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
    if hour == 7: return "morning"
    if hour == 14: return "afternoon"
    if hour == 22: return "evening"
    return "afternoon"


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


def fetch_hackernews_all(top_n: int = 20) -> list[dict]:
    items = []
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
                items.append({
                    "id": f"rss_{hashlib.md5((name + title).encode()).hexdigest()[:12]}",
                    "source": name,
                    "title": title,
                    "url": link,
                    "summary": desc,
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
            desc = clean_html(item.findtext("description") or "")
            if not title:
                continue
            items.append({
                "id": f"36kr_{hashlib.md5(title.encode()).hexdigest()[:12]}",
                "source": "36氪",
                "title": title,
                "url": link,
                "summary": desc,
            })
        log(f"  36氪: {len(items)} 条")
    except Exception as e:
        log(f"  36氪: {e}")
    return items


def fetch_github_trending(days: int = 7) -> list[dict]:
    items = []
    try:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        resp = _session.get(
            "https://api.github.com/search/repositories",
            params={"q": f"created:>{since}", "sort": "stars", "order": "desc", "per_page": 10},
            timeout=15,
            headers={"Accept": "application/vnd.github.v3+json"},
        )
        if resp.status_code != 200:
            log(f"  GitHub Trending: {resp.status_code}")
            return items
        for repo in resp.json().get("items", []):
            items.append({
                "id": f"gh_{repo['id']}",
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


# ─── 会话调度 ──────────────────────────────────────────────────────────

SESSION_CONFIG = {
    "morning": {
        "label": "🌅 全球早报",
        "prompt_type": "general",
        "sources_list": [
            ("BBC 世界新闻", lambda: fetch_rss_news({
                "BBC": "https://feeds.bbci.co.uk/news/world/rss.xml",
                "France24": "https://www.france24.com/en/rss",
                "TASS Russia": "https://tass.com/rss/v2.xml",
                "中国日报": "https://www.chinadaily.com.cn/rss/world_rss.xml",
                "纽约时报": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
            })),
            ("中文热点", lambda: ThreadPoolExecutor(max_workers=2).submit(
                lambda: None
            ).result() or (
                fetch_36kr() + fetch_v2ex()
            )),
            ("Reddit 全球讨论", lambda: fetch_reddit(["worldnews", "news"], 5)),
        ],
    },
    "afternoon": {
        "label": "☀️ 午间技术",
        "prompt_type": "tech",
        "sources_list": [
            ("AI前沿", lambda: (
                fetch_arxiv(["cs.AI", "cs.LG", "cs.CL"], 5)
                + fetch_huggingface()
            )),
            ("开发者讨论", lambda: fetch_hackernews_all(30)),
            ("开源项目", lambda: fetch_github_trending(7)),
        ],
    },
    "evening": {
        "label": "🌙 晚间速览",
        "prompt_type": "general",
        "sources_list": [
            ("游戏圈", lambda: fetch_reddit(["gaming", "Games"], 5)),
            ("数理生综合", lambda: fetch_arxiv(
                ["physics.gen-ph", "math.GM", "q-bio.GN"], 5
            )),
            ("科技新闻", lambda: fetch_hackernews_all(20)),
        ],
    },
}


DEEPSEEK_PROMPTS = {
    "general": {
        "system": """你是"前沿日报"的科普编辑，面向零基础普通读者。

写作要求：
1. 禁止使用任何专业术语，必须转化成大白话
2. 每条讲清楚：发生了什么事 → 为什么重要 → 跟普通人有什么关系
3. 像讲故事一样，先抛出一个场景再展开
4. 每段50-100字，简短有力
5. 每条标注来源""",
        "user": """以下是本时段收集的资讯：

{raw}

请改写成零基础能看懂的科普简报，格式：
🔥 **今日要闻**（最重要的2-3条）
• 每条约100字，讲清楚"出了什么事 + 为什么重要 + 跟普通人有什么关系"

📄 **其他速览**（简略提及）""",
    },
    "tech": {
        "system": """你是"前沿日报"的技术编辑，面向有编程/技术基础的读者。

写作要求：
1. 可以使用专业术语，但首次出现时简单解释一下
2. 重点讲清楚：这是什么技术 → 解决什么问题 → 跟现有方案比怎么样
3. 开源项目要提到语言、Star数、核心功能
4. 技术新闻要有技术深度，不要稀释成大白话
5. 每条标注来源""",
        "user": """以下是本时段收集的技术资讯：

{raw}

请整理成技术简报，格式：
🔥 **技术头条**（最重要的2-3条）
• 重点讲技术本身、解决的问题、对比现有方案

📦 **开源项目**（GitHub热门）
• 项目名 + 语言 + Star数 + 核心功能一句话

📄 **其他值得关注**""",
    },
}


def build_prompt(session_type: str, items: list[dict]) -> tuple[str, str]:
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
        sections.append("")
    raw = "\n".join(sections)

    prompts = DEEPSEEK_PROMPTS.get(session_type, DEEPSEEK_PROMPTS["general"])
    system_prompt = prompts["system"]
    user_prompt = prompts["user"].format(raw=raw)
    return system_prompt, user_prompt


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
                "max_tokens": 2000,
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

def send_pushplus(message: str) -> bool:
    if not PUSHPLUS_TOKEN:
        return False
    try:
        resp = _session.post("https://www.pushplus.plus/send", json={
            "token": PUSHPLUS_TOKEN,
            "title": f"前沿日报 {datetime.now(TZ_CST).strftime('%H:%M')}",
            "content": message,
            "template": "txt",
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


def send_telegram(message: str):
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

    if PUSHPLUS_TOKEN and send_pushplus(message):
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
        save_state(state)
        return

    new_items = [it for it in all_items if is_new(it["source"], it["id"], state)]
    log(f"共 {len(all_items)} 条, 新内容 {len(new_items)} 条")

    if not new_items:
        log("无新内容")
        save_state(state)
        return

    log(">>> 调用 DeepSeek...")
    sys_prompt, usr_prompt = build_prompt(config["prompt_type"], new_items)
    report = call_deepseek(sys_prompt, usr_prompt)

    sources_count = len(set(it["source"] for it in new_items))
    if report:
        msg = (
            f"{config['label']}\n"
            f"📅 {now_str}\n"
            f"📊 {sources_count} 个信源 | {len(new_items)} 条\n"
            f"{'─' * 40}\n"
            f"{report}\n\n"
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
    log("✓ 完成")


if __name__ == "__main__":
    main()
