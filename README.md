# Discord 消息转发

把指定 Discord 频道中的新消息转发到对应的 Discord Webhook。支持文字、附件链接、Embed、普通频道，以及按父频道规则匹配帖子/线程。

> [!CAUTION]
> 这个版本使用 `discord.py-self` 和普通用户 Token，属于 self-bot。Discord 明确禁止自动化普通用户账号，检测到后可能暂停或永久终止账号。请先阅读下面的风险说明；更安全、合规的做法是改用官方 Bot 账号和 Bot API。

## 功能

- 一个来源频道对应一个目标 Webhook
- 转发文字、附件链接和 Discord Embed
- 帖子/线程可自动匹配父频道规则
- 提供桌面配置界面和仅监听本机的 Web 配置界面
- Windows、macOS 启动脚本

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

程序会把成员昵称、头像、消息正文、Embed 和附件链接复制到另一个频道。只转发你有权访问、保存和再分发的内容，并得到服务器管理者及相关成员的许可。不要用于抓取、监控、骚扰、垃圾消息或绕过频道权限。

附件仍使用 Discord 原始链接，链接可能过期，也可能让目标频道成员看到原本不应公开的内容。调试模式会在本机输出频道信息和消息摘要；处理敏感内容时应关闭调试模式。

### 4. 运行与网络风险

- Web 配置页默认只监听 `127.0.0.1:5151`，不要改成公网地址；它能读取和修改你的 Token/Webhook 配置。
- 项目没有消息队列、重试持久化或送达保证。断网、Discord 限流、Webhook 删除或程序退出都可能造成消息遗漏。
- 目前只转发新消息，不同步编辑和删除；来源消息删除后，目标副本不会自动删除。

## 环境要求

- Python 3.10 或更高版本
- Windows 或 macOS

## 快速开始

```bash
python -m venv .venv
```

Windows：

```powershell
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python config_web.py
```

macOS：

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
python config_web.py
```

浏览器会打开 `http://127.0.0.1:5151`。填写 Token 和转发规则后，点击“保存配置”和“启动转发”。该 Web 界面只监听本机地址。

也可以直接使用目录中的启动脚本：

- Windows：`打开配置_web.bat`
- macOS：`打开配置_web.command`

## 手动配置

复制 `config.example.json` 为 `config.json`，然后填写：

```json
{
  "user_token": "你的用户 Token",
  "forward_rules": [
    {
      "channel_id": "来源频道 ID",
      "webhook_url": "目标频道 Webhook URL"
    }
  ],
  "debug": true
}
```

每个来源频道可配置一个目标 Webhook。帖子或线程没有单独规则时，会尝试匹配它的父频道。

来源频道 ID：在 Discord 设置中打开“高级 → 开发者模式”，然后右键来源频道并选择“复制频道 ID”。

目标 Webhook：需要目标服务器的“管理 Webhook”权限，在“频道设置 → 集成 → Webhook”中创建。Webhook URL 不要分享给其他人。

## 运行

```bash
python discord_forwarder.py
```

当前版本只转发新消息，不同步消息编辑和删除。附件以原始链接转发。

## 测试

```bash
python -m unittest discover -s tests -v
```

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

推荐在 GitHub 创建一个空仓库，不要勾选自动生成 README、`.gitignore` 或许可证，因为本项目已经包含这些文件。发布后再次打开 GitHub 文件列表，确认没有 `config.json`、`.env`、日志或虚拟环境。

如果秘密曾被提交过，单纯删除文件并不够，因为它仍可能留在 Git 历史中。先立即轮换 Token/Webhook，再清理历史。

## 许可证

[MIT](LICENSE)
