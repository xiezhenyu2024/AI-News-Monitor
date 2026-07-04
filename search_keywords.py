#!/usr/bin/env python3
"""搜索关键词测试 - 在所有信源中搜索指定关键词，返回匹配结果"""

import json
import os
import sys
import re
import hashlib
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ─── 配置 ──────────────────────────────────────────────────────────────
TZ_CST = timezone(timedelta(hours=8))
_session = requests.Session()
_session.headers.update({"User-Agent": "News-Daily/1.0"})
_adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)

KEYWORDS = os.environ.get("SEARCH_KEYWORDS", "")
SESSION_TYPE = os.environ.get("SEARCH_SESSION", "all")


def log(msg: str):
    ts = datetime.now(TZ_CST).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def clean_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()[:300]


# ─── 信源 ──────────────────────────────────────────────────────────────

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
                items.append({"source": name, "title": title, "url": link, "summary": desc})
            log(f"  {name}: ok")
        except Exception as e:
            log(f"  {name}: {e}")
    return items


def fetch_36kr() -> list[dict]:
    items = []
    try:
        resp = _session.get("https://36kr.com/feed", timeout=15)
        if resp.status_code == 200:
            root = ElementTree.fromstring(resp.content)
            for item in root.findall(".//item")[:5]:
                title = (item.findtext("title") or "").strip()
                link = item.findtext("link") or ""
                desc = clean_html(item.findtext("description") or "")
                if title:
                    items.append({"source": "36氪", "title": title, "url": link, "summary": desc})
        log(f"  36氪: ok")
    except:
        log(f"  36氪: fail")
    return items


def fetch_hackernews(top_n: int = 20) -> list[dict]:
    items = []
    try:
        resp = _session.get("https://hacker-news.firebaseio.com/v0/topstories.json", timeout=15)
        if resp.status_code != 200:
            return items
        ids = resp.json()[:top_n]
        def get_one(sid):
            try:
                r = _session.get(f"https://hacker-news.firebaseio.com/v0/item/{sid}.json", timeout=8)
                if r.status_code != 200:
                    return None
                s = r.json()
                if not s or s.get("type") != "story":
                    return None
                return {"source": "Hacker News", "title": s.get("title", ""),
                        "url": s.get("url", f"https://news.ycombinator.com/item?id={sid}"),
                        "summary": clean_html((s.get("text", "") or "")[:200])}
            except:
                return None
        with ThreadPoolExecutor(max_workers=10) as pool:
            for f in as_completed({pool.submit(get_one, sid): sid for sid in ids}):
                r = f.result()
                if r:
                    items.append(r)
        log(f"  Hacker News: ok")
    except:
        log(f"  Hacker News: fail")
    return items


def fetch_github_trending() -> list[dict]:
    items = []
    try:
        since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        resp = _session.get("https://api.github.com/search/repositories",
                            params={"q": f"created:>{since} stars:>50", "sort": "stars", "order": "desc", "per_page": 15},
                            timeout=15,
                            headers={"Accept": "application/vnd.github.v3+json"})
        if resp.status_code == 200:
            for repo in resp.json().get("items", []):
                items.append({"source": "GitHub Trending", "title": repo["full_name"] + " - " + (repo.get("description", "") or ""),
                              "url": repo["html_url"], "summary": f"⭐ {repo['stargazers_count']}"})
        log(f"  GitHub Trending: ok")
    except:
        log(f"  GitHub Trending: fail")
    return items


def fetch_juejin() -> list[dict]:
    items = []
    try:
        resp = _session.get("https://api.juejin.cn/content_api/v1/content/article_rank",
                            params={"category_id": "6809637773935378440", "type": "hot"}, timeout=15)
        if resp.status_code == 200:
            for art in resp.json().get("data", [])[:8]:
                info = art.get("content", {})
                title = info.get("title", "") or ""
                if title:
                    items.append({"source": "掘金", "title": title.strip(),
                                  "url": f"https://juejin.cn/post/{info.get('content_id', '')}",
                                  "summary": (info.get("brief", "") or "")[:200]})
        log(f"  掘金: ok")
    except:
        log(f"  掘金: fail")
    return items


def fetch_devto() -> list[dict]:
    items = []
    try:
        resp = _session.get("https://dev.to/api/articles", params={"tag": "ai", "per_page": 8}, timeout=15)
        if resp.status_code == 200:
            for art in resp.json():
                items.append({"source": "Dev.to", "title": art.get("title", ""),
                              "url": art.get("url", ""),
                              "summary": clean_html(art.get("description") or "")})
        log(f"  Dev.to: ok")
    except:
        log(f"  Dev.to: fail")
    return items


# ─── 主流程 ────────────────────────────────────────────────────────────

SOURCES = {
    "all": [
        ("BBC 等全球新闻", lambda: fetch_rss_news({
            "BBC": "https://feeds.bbci.co.uk/news/world/rss.xml",
            "France24": "https://www.france24.com/en/rss",
        })),
        ("中文科技", lambda: fetch_36kr() + fetch_juejin()),
        ("开发者讨论", lambda: fetch_hackernews(20) + fetch_devto()),
        ("开源项目", lambda: fetch_github_trending()),
    ],
    "morning": [
        ("全球新闻", lambda: fetch_rss_news({
            "BBC": "https://feeds.bbci.co.uk/news/world/rss.xml",
            "France24": "https://www.france24.com/en/rss",
        })),
        ("中文热点", lambda: fetch_36kr()),
    ],
    "afternoon": [
        ("开发者讨论", lambda: fetch_hackernews(20) + fetch_devto() + fetch_juejin()),
        ("开源项目", lambda: fetch_github_trending()),
    ],
    "evening": [
        ("综合资讯", lambda: fetch_hackernews(20)),
    ],
}


def main():
    keywords = [k.strip().lower() for k in KEYWORDS.split(",") if k.strip()]
    if not keywords:
        log("请设置 SEARCH_KEYWORDS 环境变量，用逗号分隔")
        sys.exit(1)

    log(f"搜索关键词: {', '.join(keywords)}")
    log(f"搜索范围: {SESSION_TYPE}")
    log("=" * 40)

    sources = SOURCES.get(SESSION_TYPE, SOURCES["all"])
    all_items = []

    for name, fn in sources:
        log(f">>> {name}...")
        try:
            items = fn()
            log(f"  [{name}] {len(items)} 条")
            all_items.extend(items)
        except Exception as e:
            log(f"  [{name}] 异常: {e}")

    log("=" * 40)
    log(f"共搜索 {len(all_items)} 条内容")

    matched = []
    for it in all_items:
        text = (it.get("title", "") + " " + it.get("summary", "")).lower()
        matched_kws = [kw for kw in keywords if kw in text]
        if matched_kws:
            it["matched_keywords"] = matched_kws
            matched.append(it)

    if not matched:
        log(f"\n❌ 未找到匹配结果。关键词 '{', '.join(keywords)}' 在 {len(all_items)} 条内容中无命中。")
        log("建议：换更宽泛的关键词试试")
        return

    log(f"\n✅ 匹配到 {len(matched)} 条结果:\n")

    for i, it in enumerate(matched, 1):
        print(f"  [{i}] {it['source']}")
        print(f"      标题: {it['title'][:80]}")
        if it.get("url"):
            print(f"      链接: {it['url']}")
        if it.get("summary"):
            print(f"      摘要: {it['summary'][:100]}")
        kws = it.get("matched_keywords", [])
        print(f"      命中关键词: {', '.join(kws)}")
        print()


if __name__ == "__main__":
    main()
