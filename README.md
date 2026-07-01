# AI前沿日报

多源AI新闻自动抓取 + DeepSeek摘要翻译，每天 10:00 / 14:00 / 21:00 推送到手机。

## 信源

| 来源 | 协议 | 免注册 |
|------|------|--------|
| Reddit (r/LocalLLaMA, r/MachineLearning, r/artificial) | 公开JSON | ✅ |
| Hacker News (AI相关帖子) | Firebase API | ✅ |
| ArXiv (cs.AI, cs.LG, cs.CL) | 官方API | ✅ |
| Hugging Face Daily Papers | 公开API | ✅ |

## 部署步骤（GitHub Actions，免费）

### 1. 注册 DeepSeek API
- 打开 https://platform.deepseek.com/
- 创建 API Key
- 你已经有这个了

### 2. 创建 Telegram Bot（如需要推送）
- 在 Telegram 搜索 @BotFather
- 发送 `/newbot` 按提示创建
- 拿到 Bot Token
- 搜索你创建的 bot，发送 `/start`
- 访问 `https://api.telegram.org/bot<你的TOKEN>/getUpdates` 拿到 Chat ID

### 3. 发布到 GitHub
```bash
# 在 GitHub 创建一个新仓库（公开）
git init
git add .
git commit -m "init"
git remote add origin https://github.com/你的用户名/你的仓库名.git
git push -u origin main
```

### 4. 设置 Secrets
在 GitHub 仓库页面: `Settings → Secrets and variables → Actions`

添加以下 secrets：

| Secret | 说明 |
|--------|------|
| `DEEPSEEK_API_KEY` | 你的 DeepSeek API Key |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token（可选） |
| `TELEGRAM_CHAT_ID` | 你的 Telegram Chat ID（可选） |

### 5. 完成
推送代码后，Action 会自动在每天 10:00 / 14:00 / 21:00 北京时间运行。
你也可以在 Actions 页面手动触发测试。

## 本地测试
```bash
pip install -r requirements.txt
set DEEPSEEK_API_KEY=你的key
python main.py
```
