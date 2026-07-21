#!/usr/bin/env python3
"""趋势双周报：读取 trends.json 生成趋势分析报告并推送"""

import json, os, sys, hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

TZ_CST = timezone(timedelta(hours=8))

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")

_session = requests.Session()
_session.headers.update({"User-Agent": "AI-News-Trend/1.0"})


def log(msg: str):
    print(f"[{datetime.now(TZ_CST).strftime('%H:%M:%S')}] {msg}", flush=True)


def load_trends() -> list[dict]:
    path = "trends.json"
    if not os.path.exists(path):
        log("trends.json 不存在")
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    log(f"读取到 {len(data)} 条趋势记录")
    return data


def call_deepseek(system_prompt: str, user_prompt: str) -> Optional[str]:
    if not DEEPSEEK_API_KEY:
        log("未配置 DEEPSEEK_API_KEY")
        return None
    try:
        resp = _session.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.4,
                "max_tokens": 4000,
            },
            timeout=60,
        )
        if resp.status_code != 200:
            log(f"DeepSeek: {resp.status_code} {resp.text[:200]}")
            return None
        content = resp.json()["choices"][0]["message"]["content"]
        return content.strip()
    except Exception as e:
        log(f"DeepSeek: {e}")
        return None


def send_pushplus(message: str) -> bool:
    if not PUSHPLUS_TOKEN:
        return False
    try:
        resp = _session.post("https://www.pushplus.plus/send", json={
            "token": PUSHPLUS_TOKEN,
            "title": f"趋势双周报 {datetime.now(TZ_CST).strftime('%m-%d')}",
            "content": message,
            "template": "txt",
        }, timeout=15)
        if resp.status_code == 200 and resp.json().get("code") == 200:
            log("PushPlus 推送成功")
            return True
        log(f"PushPlus 返回异常: {resp.json()}")
    except Exception as e:
        log(f"PushPlus: {e}")
    return False


def main():
    now = datetime.now(TZ_CST)
    log(f"趋势双周报 - {now.strftime('%Y-%m-%d')}")

    trends = load_trends()
    if not trends:
        log("无趋势数据，跳过")
        return

    # 取最近30天的趋势（双周报多看一些）
    cutoff = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    recent = [t for t in trends if t.get("date", "") >= cutoff]
    log(f"近30天内有 {len(recent)} 条趋势（总量 {len(trends)}）")

    if not recent:
        log("近期无趋势数据")
        return

    # 按主题聚合
    by_theme: dict[str, list[dict]] = {}
    for t in recent:
        theme = t.get("theme", t.get("title", "未分类"))
        by_theme.setdefault(theme, []).append(t)

    raw = json.dumps(recent, ensure_ascii=False, indent=2)

    system_prompt = """你是AI行业趋势分析师。你的任务是根据过去半个月收集的趋势数据，写一份结构清晰的中文趋势双周报。

输出格式：
【趋势总览】一句话概括本期AI行业趋势格局

【趋势一：XXX】
- 强度变化：持续加强 / 逐渐减弱 / 新出现
- 关键证据：列出具体的事件和数据
- 基层声音：开发者/用户的真实反馈
- 分析：为什么这个趋势重要

（按强度排列，列出所有重要趋势）

【下期观察】
- 建议重点关注的方向
- 可能出现的转折点

要求：
- 每个趋势必须引用具体日期和事件作为证据
- 基层声音如果没有数据则不编造
- 语言简洁有力，面向技术读者
- 如果多条日期记录指向同一趋势，合并分析"""  # noqa: E501

    user_prompt = f"""以下是过去30天收集的趋势数据（JSON格式），请分析并写成趋势双周报：

{raw}

注意：今天是{now.strftime('%Y-%m-%d')}，覆盖周期为{recent[0]['date']}至{recent[-1]['date']}。"""

    log("调用 DeepSeek 生成趋势报告...")
    report = call_deepseek(system_prompt, user_prompt)

    if not report:
        log("生成失败")
        return

    # 包装发送
    header = (
        f"📈 趋势双周报\n"
        f"📅 {now.strftime('%Y-%m-%d')}\n"
        f"📊 分析 {len(recent)} 条趋势记录\n"
        f"{'─' * 40}\n"
    )
    full_msg = header + report + "\n\n" + "—" * 30 + "\nPowered by DeepSeek"

    log("推送趋势双周报...")
    send_pushplus(full_msg)

    # 保存到文件供审查
    with open("trend_report.txt", "w", encoding="utf-8") as f:
        f.write(full_msg)
    log("已保存到 trend_report.txt")


if __name__ == "__main__":
    main()
