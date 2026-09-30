# 每日头条推送

每天定时抓取时政、国际、财经、金融市场、科技、体育和各大热榜，生成一份网页日报，并推送到微信、企业微信、钉钉、Telegram 或邮箱。

## 本地试跑

```bash
pip install -r requirements.txt
python news_digest.py --no-push      # 只生成 output/index.html，用浏览器打开预览
```

想在本机推送，先设置对应环境变量（见下表），再去掉 `--no-push` 运行。电脑上定时运行可用 crontab（`30 7 * * * cd /路径/daily-news && python3 news_digest.py`）或 Windows 任务计划程序。

## 免费云端定时（GitHub Actions，推荐）

1. 新建一个 GitHub 仓库，把本文件夹全部内容上传（包括 `.github` 文件夹）。
2. 仓库 Settings → Pages → Source 选 **GitHub Actions**。
3. Settings → Secrets and variables → Actions：
   - **Secrets** 里填推送渠道的密钥（至少一个）。
   - **Variables** 里填 `REPORT_URL`，值为 `https://你的用户名.github.io/仓库名/`，推送消息会带上完整日报链接。
4. Actions 页面选「每日头条推送」→ Run workflow，手动跑一次确认没问题。之后每天北京时间 7:30 左右自动推送。

> 注意：仓库设为公开时，日报网页也是公开的；推送密钥放在 Secrets 里不会泄露。

## 推送渠道

| 渠道 | 需要的变量 | 获取方式 |
| --- | --- | --- |
| 微信（PushPlus） | `PUSHPLUS_TOKEN` | pushplus.plus 微信扫码登录后复制 token，支持完整网页排版 |
| 微信（Server酱） | `SERVERCHAN_KEY` | sct.ftqq.com 登录获取 SendKey |
| 企业微信群 | `WECOM_WEBHOOK` | 群设置 → 添加群机器人 → 复制 webhook |
| 钉钉群 | `DINGTALK_WEBHOOK` | 添加自定义机器人，安全设置选“自定义关键词”，填“日报” |
| Telegram | `TELEGRAM_BOT_TOKEN` `TELEGRAM_CHAT_ID` | @BotFather 建机器人 |
| 邮件 | `SMTP_HOST` `SMTP_PORT` `SMTP_USER` `SMTP_PASS` `MAIL_TO` | 如 QQ 邮箱：smtp.qq.com、465、邮箱地址、授权码 |

## 调整新闻源

编辑 `sources.json`：每个分类下增删 `{"name": "...", "url": "RSS 地址"}` 即可，`per_category` 控制每类条数。

- 某个源失效时会自动跳过，并列在日报末尾「未能获取的源」里，照着替换就行。
- 热榜、华尔街见闻、财联社、虎扑等没有官方 RSS，走的是 RSSHub。公共实例 rsshub.app 经常限流，建议自建一个（Docker 一行命令即可），然后把地址填进 `RSSHUB_BASE`。
- 只保留最近 36 小时内的新闻，跨分类重复的标题会自动去重。

## 说明

工具只抓取各媒体公开 RSS 的标题、链接和简短摘要，正文请点链接看原文。
