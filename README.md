# AI 前沿日报 / AI News Daily

> 一个帮你每天追踪 AI、科技、全球新闻的自动推送系统。  
> 跑在 GitHub Actions 上，完全免费，推送到你微信。  
> 我业余捣鼓的小项目，可能不完善，欢迎 fork 自己改。

> An automated daily news digest that tracks AI, tech, and world news.  
> Runs on GitHub Actions (free), delivers to your WeChat via PushPlus.  
> A side project I tinkered with — probably far from perfect, feel free to fork and tweak.

---

## 功能一览 / Features

| 功能 | 说明 |
|------|------|
| 📅 **每日三报** | 早报（全球要闻）→ 午报（AI技术）→ 晚报（科普综合） |
| 📡 **新闻追踪** | 指定话题，每天自动检查最新进展 |
| 📊 **判断台账** | 自动从新闻中提取预测，后续回看验证 |
| 🏛️ **主体档案** | 跟踪公司/机构的言行变化和承诺兑现 |
| 🔗 **因果链** | 把时间线上分散的事件串联成因果故事 |
| 📈 **趋势纪事** | 每两周出一份长期视角的趋势分析 |
| 🌐 **中英双语** | 支持多语言信源，AI 自动翻译成中文 |

---

## 你需要什么 / Prerequisites

| 项目 | 是否必填 | 说明 |
|------|---------|------|
| **GitHub 账号** | ✅ 必填 | 用于 fork 仓库和运行 Actions |
| **LLM API Key** | ✅ 必填 | 用于内容总结（DeepSeek / OpenAI / Claude 等均可） |
| **PushPlus Token** | ✅ 必填 | 用于推送到微信（pushplus.plus 免费注册） |
| **EasyCron / cron-job.org** | ⚠️ 推荐 | GitHub 定时任务有延迟，外部定时器可保证准时推送 |

---

## 快速开始 / Quick Start

### 1. Fork 仓库

点击右上角的 Fork 按钮，把仓库复制到你的 GitHub 账号下。

### 2. 配置 Secrets

在你的仓库中：`Settings → Secrets and variables → Actions`

添加以下 Secrets：

| Secret | 说明 |
|--------|------|
| `DEEPSEEK_API_KEY` | 你的 LLM API Key（DeepSeek / OpenAI / Claude 等） |
| `PUSHPLUS_TOKEN` | 在 pushplus.plus 注册后获取的 Token |

### 3. 手动触发测试

在 Actions 页面找到 `News Daily` 工作流，点击 `Run workflow` 手动触发一次。

如果配置正确，你的微信会收到一条测试推送。

### 4. （推荐）配置准时触发

GitHub Actions 的定时任务有时会延迟几十分钟甚至几小时。如果你希望准时收到推送，可以用外部定时服务。

推荐两个免费服务：

- **EasyCron**：[easycron.com](https://www.easycron.com)
- **cron-job.org**：[cron-job.org](https://cron-job.org)

创建三个定时任务，到点调用 GitHub API 触发工作流：

```
请求地址：POST
URL：https://api.github.com/repos/你的用户名/你的仓库名/actions/workflows/fetch-news.yml/dispatches
Headers：
  Authorization: Bearer 你的GitHubToken（需有 workflow 权限）
  Content-Type: application/json
Body：{"ref": "master"}
```

三个任务的时间建议：

| 任务 | 时间（北京时间） |
|------|----------------|
| 早报 | 07:30 |
| 午报 | 14:00 |
| 晚报 | 21:40 |

---

## 信源说明 / Data Sources

系统目前从以下公开信源获取信息（全部免费，无需注册）：

| 信源 | 时段 | 内容类型 |
|------|------|---------|
| **BBC** / **France24** / **TASS** / **中国日报** / **纽约时报** / **卫报** | 早报 | 全球新闻 |
| **36氪** / **V2EX** | 早报 | 中文科技/热点 |
| **ArXiv** / **Hugging Face** | 午报 | AI 学术前沿 |
| **Hacker News** | 午报/晚报 | 技术讨论 |
| **GitHub Trending** | 午报 | 热门开源项目 |
| **掘金** / **开源中国** / **Dev.to** | 午报 | 开发者实测评测 |

---

## 提示词说明 / About the Prompts

所有提示词都在 `main.py` 的 `DEEPSEEK_PROMPTS` 字典里，分为三套：

- **`morning`** — 全球早报：简洁、信源分析、社会观察、各界评论
- **`afternoon`** — 午间技术：技术内容、争议点、判断提取、主体跟踪
- **`evening`** — 科普晚报：轻松有趣、谁关心、通俗易懂

你可以随便改。想调整风格、增减内容、换了模型，直接改 `main.py` 里对应的文字就行。改完 push 上去，下次运行自动生效。

> 提示词里有一段 `【数据更新】JSON` 是给系统内部使用的，建议保留，不要删除。

---

## 换别的模型 / Using Other Models

系统默认使用 DeepSeek API，但你也可以换成任何兼容 OpenAI 接口的模型。

在 GitHub Secrets 里把 `DEEPSEEK_API_KEY` 换成你的 API Key，然后在 `main.py` 里修改：

```python
DEEPSEEK_MODEL = "deepseek-chat"  # 改成你的模型名，比如 "gpt-4" / "claude-3" / "qwen" 等
```

兼容 OpenAI API 格式的模型都可以直接用。

---

## 项目结构 / Project Structure

```
├── main.py                      # 主程序（抓取 + 总结 + 推送）
├── requirements.txt             # Python 依赖
├── .github/workflows/
│   ├── fetch-news.yml           # 主工作流（数据抓取和推送）
│   └── trigger.yml              # 定时触发器（辅助，保证准时）
└── README.md                    # 本文件
```

运行过程中会自动生成以下数据文件（在 GitHub Actions 的 artifact 中持久化）：

| 文件 | 用途 |
|------|------|
| `state.json` | 已读新闻去重 |
| `tracked.json` | 你手动指定的追踪话题 |
| `judgments.json` | 自动提取的判断台账 |
| `subjects.json` | 公司/机构言行档案 |
| `trends.json` | 长期趋势原料 |
| `causality.json` | 事件因果链 |

---

## 常见问题 / FAQ

### 收不到推送？
1. 检查 PushPlus Token 是否正确配置
2. 检查 Action 运行日志是否有报错
3. 在 PushPlus 公众号发送"激活消息"激活推送

### 定时不准？
GitHub Actions 的定时任务有时会延迟。建议配合 EasyCron 或 cron-job.org 使用。

### 想改推送时间？
修改 `.github/workflows/fetch-news.yml` 里的 cron 表达式（UTC 时间），或者修改 EasyCron 里的定时设置。

### 想加自己的信源？
在 `main.py` 的 `SESSION_CONFIG` 里找到对应时段，加上你的抓取函数即可。

### 费用？
- GitHub Actions：公开仓库完全免费
- DeepSeek API：极低费用，每天三报约 0.001 元
- PushPlus：免费
- EasyCron：免费

---

## 许可 / License

MIT License — 随便用，随便改，不需要署名。

---

*最后更新：2026-07-03*
