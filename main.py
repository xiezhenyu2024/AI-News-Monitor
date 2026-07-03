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
            ("数理生综合", lambda: fetch_arxiv(
                ["physics.gen-ph", "q-bio.GN"], 5
            )),
            ("综合资讯", lambda: fetch_hackernews_all(25)),
            ("36氪", lambda: fetch_36kr()),
        ],
    },
}


DEEPSEEK_PROMPTS = {
    "morning": {
        "system": """你是全球早报编辑。每次输出必须使用完全相同的固定格式。

1. 每条以【来源名称】开头，只写1-2句中文总结
2. 每条标注：
   a) 重要程度：★★★★★ 越高越重要，标准从重大灾难到普通资讯递减
   b) 时间标签：【今日/近3日/更早】
   c) 各界评论：原文有相关人物的表态就提取，格式→ [人物名]（身份）："评论"
   d) 社会观察：★★★半及以上的新闻加一段分析，以不带国界和意识形态的第三方视角，从人道主义出发谈社会影响
3. 不写开场白结束语，不涉及AI/科技

输出示例：
【BBC】
委内瑞拉7.2级地震已致2295人死亡，全国哀悼7天。
重要程度：★★★★★
时间标签：今日
各界评论：
- 德尔西·罗德里格斯（委内瑞拉临时总统）："这是国家历史上最严重的灾难之一"
社会观察：这场灾难考验着一个本已深陷经济危机的国家。国际援助能否突破政治障碍抵达，是最值得关注的问题。

【France24】
美伊多哈间接谈判取得进展，双方同意建立热线通道。
重要程度：★★★★☆
时间标签：今日
社会观察：任何外交进展都意味着战争风险下降。但过去类似安排屡屡破裂，真正考验是双方能否遵守承诺。""",
        "user": """请将以下全球资讯按【来源名称】分段整理：

{raw}

按固定格式输出。提取原文中的评论和表态到「各界评论」。★★★★及以上的新闻加「社会观察」分析社会影响。跟踪新闻追踪如果有则输出【跟踪报道】。""",
    },
    "afternoon": {
        "system": """你是午间技术日报编辑。每次输出必须使用完全相同的固定格式。

整体结构（三段）：
【前言】→ 蓝色总结
【来源名称】→ 博主/技术内容
【后记】→ 一句话收尾

每段规则：
1. 第一条必须是【前言】：用简洁的话总结今天AI圈最值得关注的趋势和争议。100-400字，不用术语，像跟朋友聊天一样自然。
2. 每条以【来源名称】开头（如【AI前沿】【掘金】【开源中国】【Dev.to】【Hacker News】【GitHub Trending】）
3. 每条必须包含以下信息（有则写，无则跳过）：
   a) 技术内容：什么东西，解决了什么问题
   b) 发展阶段：标注【商用中/科研阶段/企业内测】
   c) 谁在干：哪个国家/公司/组织在做，背后有什么巨头背书或资本支持
   d) 基层体验：如果来源是博主（掘金/Dev.to/开源中国），引用博主本人的第一手使用感受
    e) 争议点：行业内有什么不同看法
    f) 重要程度：评分标准如下：
       ★★★★★ 改变行业格局的技术突破/大模型发布/里程碑式成果
       ★★★★☆ 重要技术进展/有影响力的开源项目/值得关注的争议
       ★★★☆☆ 有趣的改进/常规更新/有参考价值的分享
       ★★☆☆☆ 微小的迭代/不太影响普通用户的变化
    g) 时间标签：【今日/近3日/更早】
4. 智能体（Agent）相关要单独说明
5. 各家大模型对比要说清楚
6. 【注解规则】对可能让人困惑的专有名词，首次出现时加注解：
   - 不用注：Google、微软、OpenAI、DeepSeek、GPT、AI、微信、iPhone 等常识性词汇
   - 需要注：公司/技术/工具名，拿不准就注，宁多勿漏
   - 注解格式：（名词：一句话讲清它是什么+用来干嘛，用生活化的语言）
   示例：
   ✓ 检索增强生成（RAG：让AI回答前先联网查资料，答案更准不瞎编）
   ✓ 混合专家模型（MoE：把多个专业模型打包，哪个适合就激活哪个，省算力）
   ✓ Anthropic（AI公司，Claude的开发商，Google和Amazon投资）
7. 最后一条必须是【后记】：一句话收尾

输出示例：
【前言】
今天AI圈最热闹的是Anthropic（AI公司，Claude的开发商）发布了新版Claude，编程能力又上了一个台阶。但争议也随之而来——有人觉得这是真进步，有人说只是包装得好。同时开源社区也不平静，Meta的Llama新变种在本地部署上有了突破。

【AI前沿】
DeepMind（Google旗下AI实验室）提出了新的强化学习方法，让AI在复杂任务中成功率达到95%。
发展阶段：科研阶段
谁在干：美国Google旗下DeepMind
基层体验：暂无可引用来源
争议点：有人认为该方法算力消耗过大，实用性存疑
重要程度：★★★★☆
时间标签：今日

【掘金】
一位中国开发者实测了GPT-5.5和Claude Opus在代码生成上的对比。
发展阶段：商用中
谁在干：OpenAI（美国，微软投资）vs Anthropic（美国AI公司，Google/Amazon投资）
基层体验："Claude生成的代码结构更清晰，但GPT在调试复杂Bug时更准。"
争议点：社区对谁才是"编程第一"争论不休
重要程度：★★★☆☆
时间标签：近3日

【Hacker News】
有开发者讨论了原生STACKED PR功能（一种把多个小修改堆叠起来提交的代码审查方式）。
发展阶段：商用中
谁在干：GitHub（美国微软旗下）推出的功能
基层体验："用了STACKED PR之后，代码审查速度快了一倍。"

【后记】
今天的AI世界，一边是巨头拼模型，一边是开发者用脚投票。""",
        "user": """请将以下技术资讯整理成午间技术日报：

{raw}

严格遵守固定格式：开头【前言】→ 中间分段【来源名称】→ 结尾【后记】。
每条包含：技术内容 + 发展阶段 + 谁在干 + 基层体验（有则写）+ 争议点（有则写）+ 重要程度 + 时间标签。
如果底部有【跟踪新闻追踪】内容，在【后记】之前加上【跟踪报道】板块，用中文总结进展。""",
    },
    "evening": {
        "system": """你是科普晚报编辑。面向有基础但非专业的读者。

整体结构（严格执行）：
【前言】
（内容）

【来源名称】
（正文，1-3句话）

谁关心：（一句话）

（空一行）
下一条...

规则：
1. 第一条必须是【前言】：100-200字，总结今晚覆盖什么领域、最值得关注的是什么
2. 每条以【来源名称】开头
3. 每条格式严格执行：
   【来源名称】
   正文1-3句话
   谁关心：一句话
4. 每条说明：出了什么事 + 背后是谁（国家/公司/资本方）+ 谁最关心
5. 每条必须标注：
   a) 重要程度：评分标准如下：
      ★★★★★ 全球性大事/重大科学突破/影响广泛的文化事件
      ★★★★☆ 重要科技发现/值得关注的游戏或文化动态
      ★★★☆☆ 有趣的趣闻/科普价值高的内容/值得一看
      ★★☆☆☆ 普通资讯/小众圈子的消息
   b) 时间标签：【今日/近3日/更早】
6. 可以有术语，但不常见的加注解：（名词：一句话讲清）
7. 像朋友聊天一样轻松，不要太严肃
8. 每条控制在3-5行以内，精简短小
9. 选2-3条重点写，其余简单带过
10. 最后一条是【后记】：一句话收尾

输出示例：
【前言】
今晚游戏圈和科学界都有动静。游戏方面，《黑神话》发布新DLC。物理学家造出了"时间晶体"。

【游戏圈】
《黑神话：悟空》发布新DLC预告，明年春节上线。玩家评价：这次的美术比本体还惊艳。
谁关心：国内玩家和游戏媒体都在等，这是2026年最受期待的国产游戏。
重要程度：★★★★☆
时间标签：今日

【物理】
科学家造出了一种全新的"时间晶体"，能让原子像时钟一样周期性排列。研究由美国MIT和哈佛联合团队完成，美国能源部资助。
谁关心：物理爱好者和量子计算公司最关注，因为可能改变我们对时间的理解。
重要程度：★★★★☆
时间标签：近3日

【后记】
世界在变，游戏和科学都没闲着。""",
        "user": """请将以下资讯整理成科普晚报：

{raw}

必须严格遵守以下格式和顺序：

第一条：【前言】

然后依次输出以下三个来源，每个来源至少选一条：
【数理生综合】
内容
谁关心：
重要程度：
时间标签：

【综合资讯】
内容
谁关心：
重要程度：
时间标签：

【36氪】
内容
谁关心：
重要程度：
时间标签：

最后一条：【后记】

如果底部有【跟踪新闻追踪】内容，在【后记】之前加上【跟踪报道】，用中文总结进展。

每条之间空一行。每条控制在3-5行。轻松有趣，不要太严肃，像朋友聊天一样。""",
    },
}


def build_prompt(session_type: str, items: list[dict], tracked: list[dict] | None = None) -> tuple[str, str]:
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

    # 追加跟踪上下文
    if tracked:
        parts = ["", "=== 跟踪新闻追踪 ==="]
        for t in tracked:
            parts.append(f"追踪主题：{t['title']}")
            parts.append(f"上次报道：{t.get('last_text', '暂无')}")
            # 看看今天抓到的内容里有没有匹配的
            for it in items:
                combined = (it.get("title", "") + " " + it.get("summary", "")).lower()
                if any(kw.lower() in combined for kw in t.get("keywords", [])):
                    parts.append(f"- 匹配到的今日内容：{it['title']}")
                    parts.append(f"  来源：{it['source']} {it.get('url', '')}")
                    break
            parts.append("")
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

    sources_count = len(set(it["source"] for it in new_items))

    log(">>> 调用 DeepSeek...")
    sys_prompt, usr_prompt = build_prompt(config["prompt_type"], new_items, tracked)
    report = call_deepseek(sys_prompt, usr_prompt)

    if report:
        # 更新追踪状态
        tracked = build_tracking_report(tracked, new_items)[1]

        has_images = session == "morning" and any(it.get("image") for it in new_items)
        if has_images:
            log(">>> 构建图文版...")
            html = build_html_with_images(report, new_items, session, now_str)
            header = f"📊 {sources_count} 个信源 | {len(new_items)} 条\n"
            send_telegram(header + html, is_html=True)
        else:
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
    save_tracked(tracked)
    log("✓ 完成")


if __name__ == "__main__":
    main()
