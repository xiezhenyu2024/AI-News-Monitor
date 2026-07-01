# AI前沿日报 使用手册

> 多源AI新闻自动抓取 → DeepSeek 摘要翻译 → 推送到你手机

---

## 一、这是什么？

一个**全自动的 AI 资讯追踪系统**。它每天定时从多个信源抓取最新 AI 相关内容，用 DeepSeek 整理成中文简报，然后推送到你的手机上。

**你不需要做任何操作**，手机到点自动收消息。

---

## 二、数据来源

| 信源 | 内容类型 | 抓取范围 |
|------|---------|---------|
| **ArXiv** | 学术论文 | cs.AI / cs.LG / cs.CL 最新提交 |
| **Hacker News** | 技术讨论 | 热帖中筛选 AI 相关 |
| **Hugging Face** | 每日论文 | Daily Papers 精选 |
| **Reddit** | 社区讨论 | r/LocalLLaMA / r/MachineLearning / r/artificial |

所有信源均为**公开 API，无需注册**，完全合规无风险。

---

## 三、运行时间

每天 **北京时间 10:00 / 14:00 / 21:00** 各执行一次。

也可以手动触发：打开 [GitHub Actions 页面](https://github.com/xiezhenyu2024/AI-News-Monitor/actions) → 点 `AI News Daily` → `Run workflow`。

---

## 四、推送方式

通过 **PushPlus（推送加）** 发送到你微信的服务通知。

如果你没收到消息：

1. 微信搜索「**PushPlus 推送加**」公众号并关注
2. 公众号内会显示历史推送记录
3. 如果手机没弹出通知，在公众号设置里开启「接收文章推送」

---

## 五、技术架构（简易版）

```
┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐
│ ArXiv  │  │  HN    │  │   HF   │  │ Reddit │
└───┬────┘  └───┬────┘  └───┬────┘  └───┬────┘
    └──────────┴──────────┴──────────┘
                      │
              ┌───────▼───────┐
              │  DeepSeek API  │  ← 摘要 + 翻译成中文
              │  (flash 模式)  │
              └───────┬───────┘
                      │
              ┌───────▼───────┐
              │  PushPlus 推送  │  → 你的微信
              └───────────────┘

运行在: GitHub Actions（免费，无需服务器）
费用: 0 元（DeepSeek API 几乎免费）
```

---

## 六、如何查看运行日志

如果你某次没收到推送，可以检查执行情况：

1. 打开 https://github.com/xiezhenyu2024/AI-News-Monitor/actions
2. 点击最新一次运行记录（绿色的 ✅ 表示成功）
3. 点 `fetch` 任务查看详细日志

日志里可以看到：
- 各信源抓取了多少条
- DeepSeek 返回了什么内容
- 推送是否成功

---

## 七、如何修改配置

### 想增减信源？
编辑 `main.py` 文件，找到对应的 `fetch_xxx()` 函数修改。

### 想改变推送时间？
编辑 `.github/workflows/fetch-news.yml` 的 cron 表达式（UTC 时间）。

### 想换推送方式？
代码已内置 Telegram / PushPlus / QQ邮箱 三种通道，配置对应的 Secrets 即可。

---

## 八、后续可扩展

- [ ] 接入 Twitter/X（有免费 API）
- [ ] 接入即刻爬虫
- [ ] Telegram Bot 推送
- [ ] 每日精华 Digest（三报合并为一报）
- [ ] 历史记录 Web 页面

---

## 九、遇到问题

在 GitHub 仓库的 Issues 页面提交：[https://github.com/xiezhenyu2024/AI-News-Monitor/issues](https://github.com/xiezhenyu2024/AI-News-Monitor/issues)

---

*最后更新: 2026-07-01*
