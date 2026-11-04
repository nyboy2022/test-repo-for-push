# Discord Channel Message Forwarder

Forward messages from channels in other Discord servers that your Discord account can access to webhooks you specify.

主要功能：把你自己的 Discord 账号能够看到的、其他服务器频道里的消息，转发到你指定的 Webhook。支持多账号、频道和分类规则、附件、Embed，以及按父频道规则匹配帖子/线程，并提供本机网页管理后台。

> [!CAUTION]
> 这个版本使用 `discord.py-self` 和普通用户 Token，属于 self-bot。Discord 明确禁止自动化普通用户账号，检测到后可能暂停或永久终止账号。请先阅读下面的风险说明；更安全、合规的做法是改用官方 Bot 账号和 Bot API。

## 功能

- 多个账号同时运行，每条规则可选择所属账号
- 来源可以是频道 ID 或分类 ID；分类规则覆盖其下所有子频道，子频道的单独规则优先
- 每个目标 Webhook 有独立队列，一个目标出问题不会拖慢其他目标
- 临时错误自动重试且不重发已送达的分段；404 这类错误不做无效重试
- 8 MB 以内的附件重新上传，避免 Discord 链接过期；更大的附件发送链接
- 账号断线自动重连，转发进程异常退出自动重启，防止循环转发和重复运行
- 日志自动轮转，日志、状态文件和页面都隐藏 Webhook 密钥和 Token
- 仅监听本机的管理后台：查看状态、编辑规则和账号、检测 Webhook、查看日志
- 可选的历史消息补发脚本

## 风险与责任

### 1. Discord 账号风险（高）

Discord 的 [Self-Bot 官方说明](https://support.discord.com/hc/en-us/articles/115002192352-Automated-User-Accounts-Self-Bots) 和 [社区准则](https://discord.com/guidelines) 都明确禁止 self-bot/user-bot。即使只读取和转发消息，也不能消除账号被限制或终止的风险。本项目的 MIT 许可证不提供任何可用性、合规性或账号安全保证。

README 不提供提取普通用户 Token 的教程。推荐把程序改成官方 Bot；但 Bot 必须由服务器管理者邀请并授予读取来源频道的权限。

### 2. Token 与 Webhook 泄露风险（高）

- 用户 Token 泄露可能导致账号被接管，包括读取私信、冒充账号，以及利用已有服务器权限。
- Webhook URL 泄露后，别人可以向目标频道发送垃圾或冒充消息。
- 不要把 `config.json`、截图、终端输出或 Token 上传到 GitHub、网盘、聊天群或问题反馈。
- 本仓库已忽略 `config.json`，但发布前仍应运行 `git status` 确认它没有被跟踪。
- 如果 Token 可能泄露，立即修改 Discord 密码，使旧用户 Token 失效；同时删除并重新创建泄露的 Webhook。Discord 的[账号安全说明](https://discord.com/safety/360044104071-tips-against-spam-and-hacking)也建议不要向任何人提供密码或 Token。

### 3. 隐私、版权和授权风险

程序会把成员昵称、头像、消息正文、Embed 和附件复制到另一个频道，补发脚本还会读取频道的历史消息。只转发你有权访问、保存和再分发的内容，并得到服务器管理者及相关成员的许可。不要用于抓取、监控、骚扰、垃圾消息或绕过频道权限。

附件重新上传后，即使来源消息被删除，副本也会留在目标频道。调试日志会把账号收到的所有消息完整写入本机日志；处理敏感内容时应关闭调试模式。

### 4. 运行与网络风险

- 管理后台默认只监听 `127.0.0.1:5151`，不要改成公网地址；它能修改你的 Token/Webhook 配置。
- 队列保存在内存中，没有送达保证。程序重启时，还在排队的消息和离线期间的消息不会自动补发。
- 目前只转发新消息，不同步编辑和删除；来源消息删除后，目标副本不会自动删除。

## 环境要求

- Python 3.11（开发和测试使用的版本）
- Windows 或 macOS

## 快速开始

Windows：双击 `一键启动.bat`。首次运行会创建 `.venv-win` 环境并安装依赖，然后打开管理后台。

macOS：运行 `打开配置_web.command`。

也可以手动启动：

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt   # Windows 为 .venv\Scripts\python.exe
.venv/bin/python config_web.py
```

浏览器会打开 `http://127.0.0.1:5151`。在“账号”和“转发规则”页填写内容并保存，再点击“启动转发”。之后打开后台时会自动开始转发；手动停止过的，下次打开不会自动开始。后台本身不会随系统启动。

配置保存后需要重启转发进程才能生效，页面会提示。编辑已有账号或规则时，把 Token/Webhook 留空即可保留原值。

## 手动配置

复制 `config.example.json` 为 `config.json`，然后填写账号和规则。

来源 ID：在 Discord 设置中打开“高级 → 开发者模式”，然后右键来源频道或分类并选择“复制 ID”。

目标 Webhook：需要目标服务器的“管理 Webhook”权限，在“频道设置 → 集成 → Webhook”中创建。Webhook URL 不要分享。

规则的 `allow_automated` 开启后会转发机器人和 Webhook 发出的消息。程序始终跳过配置中目标 Webhook 自己发出的消息，防止循环转发。

## 补发历史消息

```bash
python backfill_recent.py <频道ID> --days 4 --dry-run
python backfill_category.py <分类ID> --days 4 --dry-run
```

去掉 `--dry-run` 才会真正发送。补发使用与实时转发相同的过滤规则，并把已补发的消息记在 `backfill_sent.json` 中，重复运行不会重复发送（`--force` 可强制重发）。同一来源配置了多个账号时用 `--account <账号ID>` 指定。目前不包含子区（thread）和论坛频道。

## 日志

`forwarder.log` 超过 20 MB 自动轮转，保留 5 份。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试不会连接网络，也不会启动转发进程。

## 如何把自己的版本开源到 GitHub

先确认 `config.json` 没有出现在 `git status` 中，再执行：

```bash
git init -b main
git add .
git status
git commit -m "Initial open-source release"
git remote add origin https://github.com/你的用户名/你的仓库名.git
git push -u origin main
```

推荐在 GitHub 创建一个空仓库，不要勾选自动生成 README、`.gitignore` 或许可证，因为本项目已经包含这些文件。

如果秘密曾被提交过，单纯删除文件并不够，因为它仍可能留在 Git 历史中。先立即轮换 Token/Webhook，再清理历史。

## 许可证

[MIT](LICENSE)
