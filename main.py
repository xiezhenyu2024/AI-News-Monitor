#!/usr/bin/env python3
"""AI前沿日报 - 多源AI新闻抓取 + DeepSeek摘要翻译"""

import json
import os
import sys
import hashlib
import re
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

STATE_FILE = "state.json"
TZ_CST = timezone(timedelta(hours=8))

# 全局 session 复用 TCP 连接
_session = requests.Session()
_session.headers.update({"User-Agent": "AI-News-Monitor/1.0"})
_adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)


# ─── 工具函数 ──────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now(TZ_CST).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def check_network() -> bool:
    for url in ("https://www.google.com", "https://api.github.com"):
        try:
            _session.get(url, timeout=5)
            return True
        except requests.RequestException:
            continue
    return False


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_state(state: dict):
    serializable = {}
    for k, v in state.items():
        if isinstance(v, set):
            serializable[k] = list(v)
        else:
            serializable[k] = v
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False)


def is_new(source: str, item_id: str, state: dict) -> bool:
    seen = state.setdefault(source, set())
    if isinstance(seen, list):
        seen = set(seen)
        state[source] = seen
    if item_id in seen:
        return False
    seen.add(item_id)
    return True


def clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


# ─── 数据源 ────────────────────────────────────────────────────────────

def fetch_reddit() -> list[dict]:
    items = []
    subreddits = ["LocalLLaMA", "MachineLearning", "artificial"]
    for sub in subreddits:
        try:
            url = f"https://www.reddit.com/r/{sub}/hot.json?limit=5"
            resp = _session.get(url, timeout=15)
            if resp.status_code != 200:
                log(f"  Reddit r/{sub}: {resp.status_code}")
                continue
            data = resp.json()
            for post in data.get("data", {}).get("children", []):
                p = post["data"]
                items.append({
                    "id": f"reddit_{p['id']}",
                    "source": f"Reddit r/{sub}",
                    "title": p.get("title", ""),
                    "url": f"https://reddit.com{p.get('permalink', '')}",
                    "summary": clean_html(p.get("selftext", "") or p.get("url", "")),
                    "score": p.get("score", 0),
                    "author": p.get("author", ""),
                    "time": str(p.get("created_utc", "")),
                })
            log(f"  Reddit r/{sub}: ok")
        except Exception as e:
            log(f"  Reddit r/{sub}: {e}")
    return items


def _fetch_hn_story(sid: int) -> Optional[dict]:
    try:
        resp = _session.get(
            f"https://hacker-news.firebaseio.com/v0/item/{sid}.json",
            timeout=8,
        )
        if resp.status_code != 200:
            return None
        story = resp.json()
        if not story or story.get("type") != "story":
            return None
        title = (story.get("title", "") or "").lower()
        text = (story.get("text", "") or "")[:300].lower()
        ai_keywords = ["ai", "artificial intelligence", "machine learning", "llm",
                       "gpt", "neural", "deep learning", "transformer", "openai",
                       "anthropic", "google", "meta", "llama", "gemma", "mistral",
                       "claude", "chatgpt", "diffusion", "rlhf", "rag", "agent",
                       "fine-tun", "open source", "model", "dataset"]
        if not any(kw in (title + " " + text) for kw in ai_keywords):
            return None
        return {
            "id": f"hn_{sid}",
            "source": "Hacker News",
            "title": story.get("title", ""),
            "url": story.get("url", f"https://news.ycombinator.com/item?id={sid}"),
            "summary": clean_html(story.get("text", "") or ""),
            "score": story.get("score", 0),
            "author": story.get("by", ""),
            "time": str(story.get("time", "")),
        }
    except Exception:
        return None


def fetch_hackernews() -> list[dict]:
    try:
        resp = _session.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json", timeout=15
        )
        if resp.status_code != 200:
            log(f"  Hacker News: {resp.status_code}")
            return []
        story_ids = resp.json()[:30]
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_fetch_hn_story, sid): sid for sid in story_ids}
            results = []
            for f in as_completed(futures):
                r = f.result()
                if r:
                    results.append(r)
                    if len(results) >= 5:
                        break
        log(f"  Hacker News: {len(results)} 条AI相关")
        return results
    except Exception as e:
        log(f"  Hacker News: {e}")
        return []


def fetch_arxiv() -> list[dict]:
    items = []
    categories = ["cs.AI", "cs.LG", "cs.CL"]
    for cat in categories:
        try:
            url = (f"http://export.arxiv.org/api/query"
                   f"?search_query=cat:{cat}"
                   f"&sortBy=submittedDate&sortOrder=descending&max_results=5")
            resp = _session.get(url, timeout=20)
            if resp.status_code != 200:
                continue
            root = ElementTree.fromstring(resp.content)
            ns = {"atom": "http://www.w3.org/2005/Atom",
                  "arxiv": "http://arxiv.org/schemas/atom"}
            for entry in root.findall("atom:entry", ns):
                arxiv_id = entry.find("atom:id", ns).text.strip()
                title = (entry.find("atom:title", ns).text or "").strip()
                summary = clean_html(
                    (entry.find("atom:summary", ns).text or "").strip()
                )[:300]
                authors = ", ".join(
                    a.find("atom:name", ns).text
                    for a in entry.findall("atom:author", ns)[:3]
                    if a.find("atom:name", ns) is not None
                )
                link = entry.find("atom:link[@rel='alternate']", ns)
                paper_url = link.get("href", "") if link is not None else ""
                items.append({
                    "id": f"arxiv_{hashlib.md5(arxiv_id.encode()).hexdigest()[:12]}",
                    "source": f"ArXiv {cat}",
                    "title": title,
                    "url": paper_url,
                    "summary": summary,
                    "score": 0,
                    "author": authors,
                    "time": "",
                })
            log(f"  ArXiv {cat}: ok")
        except Exception as e:
            log(f"  ArXiv {cat}: {e}")
    return items


def fetch_huggingface() -> list[dict]:
    items = []
    try:
        resp = _session.get(
            "https://huggingface.co/api/daily_papers", timeout=15
        )
        if resp.status_code != 200:
            log(f"  Hugging Face: {resp.status_code}")
            return items
        papers = resp.json()
        if isinstance(papers, dict):
            papers = papers.get("papers", [])
        for paper in papers[:5]:
            paper_id = paper.get("id", paper.get("paperId", ""))
            title = paper.get("title", "")
            if not title:
                continue
            items.append({
                "id": f"hf_{hashlib.md5(paper_id.encode()).hexdigest()[:12]}",
                "source": "Hugging Face",
                "title": title,
                "url": paper.get("url", paper.get("link", "")
                                 or f"https://huggingface.co/papers/{paper_id}"),
                "summary": paper.get("summary", paper.get("abstract", ""))[:300],
                "score": paper.get("upvotes", paper.get("score", 0)),
                "author": paper.get("author", paper.get("authors", "")),
                "time": "",
            })
        log(f"  Hugging Face: {len(items)} 条")
    except Exception as e:
        log(f"  Hugging Face: {e}")
    return items


# ─── LLM 处理 ──────────────────────────────────────────────────────────

def call_deepseek(system_prompt: str, user_prompt: str) -> Optional[str]:
    if not DEEPSEEK_API_KEY:
        log("  [跳过] 未配置 DEEPSEEK_API_KEY")
        return None
    try:
        resp = _session.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
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
            log(f"  DeepSeek API: {resp.status_code} {resp.text[:200]}")
            return None
        result = resp.json()
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        return content.strip()
    except Exception as e:
        log(f"  DeepSeek API: {e}")
        return None


def process_items(all_items: list[dict]) -> Optional[str]:
    if not all_items:
        return None

    by_source = {}
    for item in all_items:
        by_source.setdefault(item["source"], []).append(item)

    sections = []
    for src, src_items in sorted(by_source.items()):
        sections.append(f"=== {src} ===")
        for it in src_items:
            line = f"- {it['title']}"
            if it["summary"]:
                line += f"\n  摘要: {it['summary'][:200]}"
            if it["url"]:
                line += f"\n  URL: {it['url']}"
            sections.append(line)
        sections.append("")

    raw_text = "\n".join(sections)

    system_prompt = """你是一个AI前沿资讯编辑。你的任务是将多平台收集到的AI资讯整理成一份中文简报。

要求：
1. 识别最重要的3-5个话题，每个话题写一段中文摘要（50-100字）
2. 如果同一话题出现在多个平台，指出各平台讨论的侧重点有何不同
3. 结构清晰，使用emoji标记，便于手机阅读
4. 保持客观，不做主观评价
5. 每个条目附带数据来源"""

    user_prompt = f"""以下是 {datetime.now(TZ_CST).strftime('%Y-%m-%d %H:%M')} 从各平台抓取的AI相关资讯：

{raw_text}

请整理成中文简报，包含：
🔥 今日热点
📄 最新论文/技术
💡 各平台观点对比（如果有同话题跨平台的情况）"""

    log(f"  发送给 DeepSeek ({len(raw_text)} 字符)...")
    result = call_deepseek(system_prompt, user_prompt)
    if result:
        log(f"  DeepSeek 返回 {len(result)} 字符")
    return result


# ─── 推送 ──────────────────────────────────────────────────────────────

def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("  [跳过] 未配置 Telegram，以下为结果预览")
        safe_msg = message.encode("utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8", errors="replace"
        )
        try:
            print("\n" + "=" * 50)
            print(safe_msg)
            print("=" * 50 + "\n")
        except Exception:
            log(f"  (预览长度: {len(message)} 字符)")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = _session.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }, timeout=15)
        if resp.status_code == 200:
            log("  Telegram 推送成功")
        else:
            log(f"  Telegram 推送失败: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log(f"  Telegram 推送异常: {e}")


# ─── 主流程 ────────────────────────────────────────────────────────────

def main():
    log("=" * 40)
    log(f"AI前沿日报 - 开始抓取")
    log(f"时间: {datetime.now(TZ_CST).strftime('%Y-%m-%d %H:%M')}")

    if not check_network():
        log("无网络连接，跳过本次抓取")
        sys.exit(0)

    state = load_state()
    log("  已加载状态记录")

    # 并行抓取所有信源
    all_items = []
    sources = {
        "Reddit": fetch_reddit,
        "Hacker News": fetch_hackernews,
        "ArXiv": fetch_arxiv,
        "Hugging Face": fetch_huggingface,
    }
    with ThreadPoolExecutor(max_workers=4) as pool:
        fut_map = {pool.submit(fn): name for name, fn in sources.items()}
        for f in as_completed(fut_map):
            name = fut_map[f]
            try:
                items = f.result()
                log(f"  [{name}] {len(items)} 条")
                all_items.extend(items)
            except Exception as e:
                log(f"  [{name}] 异常: {e}")

    if not all_items:
        log("未获取到任何内容")
        save_state(state)
        return

    log(f"共获取 {len(all_items)} 条原始内容")

    new_items = [it for it in all_items if is_new(it["source"], it["id"], state)]
    log(f"其中 {len(new_items)} 条是新内容")

    if not new_items:
        log("没有新内容需要处理")
        save_state(state)
        return

    log(">>> 正在调用 DeepSeek 生成简报...")
    report = process_items(new_items)

    if report:
        header = (
            f"🤖 AI前沿日报\n"
            f"📅 {datetime.now(TZ_CST).strftime('%Y-%m-%d %H:%M')}\n"
            f"📊 {len(set(it['source'] for it in new_items))} 个信源 | {len(new_items)} 条\n"
            f"{'─' * 40}\n"
        )
        full_msg = header + report + "\n\n—" * 15 + "\nPowered by DeepSeek | 每日三报"
        send_telegram(full_msg)
    else:
        log("DeepSeek 未返回结果，直接推送原始内容")
        raw = f"AI前沿日报 (原始)\n{datetime.now(TZ_CST).strftime('%Y-%m-%d %H:%M')}\n\n"
        for it in new_items[:10]:
            raw += f"• [{it['source']}] {it['title']}\n  {it['url']}\n"
        send_telegram(raw)

    save_state(state)
    log("状态已保存")
    log("✓ 完成")


if __name__ == "__main__":
    main()
