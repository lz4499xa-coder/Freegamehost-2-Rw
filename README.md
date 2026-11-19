# FreeGameHost Auto Renew

基于 Python + Playwright + GitHub Actions 的 `panel.freegamehost.xyz` 自动续费保活脚本。

## 配置步骤

在仓库 **Settings -> Secrets and variables -> Actions -> Secrets** 中添加：

| Secret 名称 | 必填 | 说明 |
| :--- | :---: | :--- |
| `FGH_USERNAME` | 是 | 登录邮箱或用户名 |
| `FGH_PASSWORD` | 是 | 登录密码 |
| `FGH_SERVER_IDS` | 否 | 服务器 ID（如 `a1b2c3d4`，多个用英文逗号隔开；留空则自动识别首页全部服务器） |
| `PROXY_URL` | 否 | 代理地址（如 `http://user:pass@host:port` 或 `socks5://host:port`） |
| `TG_BOT_TOKEN` | 否 | Telegram 机器人 Token |
| `TG_CHAT_ID` | 否 | Telegram 接收者 Chat ID |

> 可选变量：在 **Variables** 中设置 `TG_ALWAYS_NOTIFY=1`，可让每次处于冷却期时也发送 Telegram 截图通知。
