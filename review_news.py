#!/usr/bin/env python3
"""多智能体日报审核 - 用不同视角审查每日报告质量"""

import json, os, sys, requests
from datetime import datetime, timezone, timedelta

TZ_CST = timezone(timedelta(hours=8))
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
_session = requests.Session()

now_str = datetime.now(TZ_CST).strftime("%Y-%m-%d %H:%M")

AGENTS = {
    "accuracy": {
        "name": "🔍 事实核查员",
        "role": """你是日报的事实核查员。你的任务：
1. 挑出报告中每一个具体的数据（数字、日期、百分比）
2. 判断这些数据是否可能来自原文
3. 如果发现疑似编造的数据，明确指出
4. 如果所有数据看起来合理，也说"未发现问题"

只做事实核查，不做风格评价。""",
    },
    "readability": {
        "name": "📖 可读性审查员",
        "role": """你是日报的可读性审查员。你的任务：
1. 找出报告中你可能读不懂的句子
2. 判断：是因为缺少前提还是术语没解释
3. 指出哪些地方普通读者可能困惑
4. 给出具体的修改建议

针对每条问题，说明"缺前提"还是"术语没注解"。""",
    },
    "premise": {
        "name": "🧩 前提完整性审查员",
        "role": """你是日报的前提完整性审查员。你的任务：
1. 检查每条新闻是否交代了背景/来龙去脉
2. 如果一条新闻只说了结论没说前提，标记出来
3. 数据类新闻（灾难/伤亡/经济）必须有数字和出处
4. 给出具体的"缺什么"建议

标准：读者不需要查资料就能理解这条新闻在说什么。""",
    },
}


def call_llm(system_prompt: str, report: str, agent_name: str) -> str:
    if not DEEPSEEK_API_KEY:
        return f"[{agent_name}] 未配置 API Key，跳过"
    try:
        resp = _session.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"请审查以下日报：\n\n{report}\n\n按上述要求输出审查结果。"},
                ],
                "temperature": 0.2,
                "max_tokens": 1500,
            },
            timeout=60,
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
        return f"[{agent_name}] API 错误: {resp.status_code}"
    except Exception as e:
        return f"[{agent_name}] 异常: {e}"


def main():
    # 读取要审查的报告
    report_path = os.environ.get("REVIEW_FILE", "")
    if not report_path or not os.path.exists(report_path):
        print("请设置 REVIEW_FILE 环境变量指向待审查的报告文件")
        sys.exit(1)

    with open(report_path, "r", encoding="utf-8") as f:
        report = f.read()

    print(f"{'='*50}")
    print(f"📋 多智能体日报审核 - {now_str}")
    print(f"{'='*50}\n")
    print(f"报告长度: {len(report)} 字符\n")

    results = {}
    for key, agent in AGENTS.items():
        print(f">>> {agent['name']} 正在审查...", flush=True)
        result = call_llm(agent["role"], report, agent["name"])
        results[key] = result
        print(f"  ✓ 完成 ({len(result)} 字符)\n")

    # 汇总
    print(f"{'='*50}")
    print("📊 审核汇总")
    print(f"{'='*50}\n")

    for key, agent in AGENTS.items():
        print(f"{agent['name']}:")
        print(f"{results[key]}")
        print()

    # 输出汇总文件
    summary = f"多智能体日报审核 - {now_str}\n{'='*50}\n\n"
    for key, agent in AGENTS.items():
        summary += f"{agent['name']}:\n{results[key]}\n\n"

    with open("review_report.txt", "w", encoding="utf-8") as f:
        f.write(summary)
    print(f"审核报告已保存到 review_report.txt")


if __name__ == "__main__":
    main()
