"""
Discord 消息监控转发脚本
使用 discord.py-self (User Token) 监听指定频道消息，并转发到 Webhook
"""

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import discord
import requests


# ==================== 配置加载 ====================
CONFIG_PATH = Path(__file__).parent / "config.json"


def is_valid_webhook_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False

    host = (parsed.netloc or "").lower()
    allowed_hosts = {
        "discord.com",
        "discordapp.com",
        "ptb.discord.com",
        "canary.discord.com",
        "ptb.discordapp.com",
        "canary.discordapp.com",
    }
    if host not in allowed_hosts:
        return False

    return parsed.path.startswith("/api/webhooks/")


def load_config() -> tuple[str, dict[int, str], bool]:
    """
    从 config.json 加载配置
    返回 (token, channel_to_webhook: {channel_id: webhook_url}, debug)
    """
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            token = data.get("user_token", "")
            debug = data.get("debug", True)

            # 新格式：forward_rules
            rules = data.get("forward_rules", [])
            channel_to_webhook: dict[int, str] = {}

            if rules:
                for r in rules:
                    if not isinstance(r, dict):
                        continue
                    cid = r.get("channel_id")
                    wh = r.get("webhook_url", "").strip()
                    if not cid or not wh or not is_valid_webhook_url(wh):
                        continue
                    try:
                        channel_to_webhook[int(str(cid).strip())] = wh
                    except (ValueError, TypeError):
                        pass
            else:
                # 兼容旧格式：source_channel_ids + webhook_url
                raw_ids = data.get("source_channel_ids", [])
                webhook = data.get("webhook_url", "").strip()
                if webhook and is_valid_webhook_url(webhook):
                    for x in raw_ids:
                        try:
                            s = str(x).strip()
                            if s.isdigit():
                                channel_to_webhook[int(s)] = webhook
                        except (ValueError, TypeError):
                            pass

            return token, channel_to_webhook, debug
        except (json.JSONDecodeError, ValueError):
            pass
    return "", {}, True


USER_TOKEN, CHANNEL_TO_WEBHOOK, DEBUG_MODE = load_config()


# ==================== 核心逻辑 ====================
# guild_subscriptions=True 确保在大型服务器中也能收到消息事件
client = discord.Client(guild_subscriptions=True)


def build_webhook_embeds(message: discord.Message) -> list[dict]:
    """将 Discord 消息中的 embeds 转成 Webhook 可接受的精简结构。"""
    embeds_out: list[dict] = []
    for embed in message.embeds:
        item: dict = {}

        if embed.title:
            item["title"] = embed.title
        if embed.description:
            item["description"] = embed.description
        if embed.url:
            item["url"] = embed.url
        if embed.color is not None:
            item["color"] = int(embed.color)

        if embed.author:
            author = {}
            if embed.author.name:
                author["name"] = embed.author.name
            if embed.author.url:
                author["url"] = embed.author.url
            if embed.author.icon_url:
                author["icon_url"] = embed.author.icon_url
            if author:
                item["author"] = author

        if embed.footer and embed.footer.text:
            footer = {"text": embed.footer.text}
            if embed.footer.icon_url:
                footer["icon_url"] = embed.footer.icon_url
            item["footer"] = footer

        if embed.image and embed.image.url:
            item["image"] = {"url": embed.image.url}
        if embed.thumbnail and embed.thumbnail.url:
            item["thumbnail"] = {"url": embed.thumbnail.url}

        fields = []
        for field in embed.fields:
            if field.name or field.value:
                fields.append(
                    {
                        "name": field.name or "\u200b",
                        "value": field.value or "\u200b",
                        "inline": bool(field.inline),
                    }
                )
        if fields:
            item["fields"] = fields[:25]

        if item:
            embeds_out.append(item)

    return embeds_out[:10]


def forward_to_webhook(message: discord.Message, webhook_url: str) -> None:
    """
    将 Discord 消息转发到指定 Webhook
    """
    try:
        # 1. 获取服务器名称（私信时为 "DM"）
        server_name = message.guild.name if message.guild else "DM"

        # 2. 获取发送者用户名（优先显示服务器内昵称）
        if hasattr(message.author, "display_name"):
            username = message.author.display_name
        else:
            username = message.author.name

        # 3. 构建 Webhook 显示名称: [Server Name] Original_Username
        webhook_username = f"[{server_name}] {username}"

        # 4. 获取发送者头像 URL
        avatar_url = str(message.author.display_avatar.url)

        # 5. 构建消息内容
        content = message.content or ""

        # 6. 若有附件，将 URL 追加到内容末尾
        if message.attachments:
            attachment_urls = [att.url for att in message.attachments]
            urls_text = "\n".join(attachment_urls)
            content = f"{content}\n{urls_text}".strip() if content else urls_text

        embeds = build_webhook_embeds(message)

        # 7. 只有在 content 和 embeds 都为空时才使用占位符
        if (not content or not content.strip()) and not embeds:
            content = "*(无文字)*"

        # 8. 构建 Webhook JSON 负载
        payload = {
            "username": webhook_username,
            "avatar_url": avatar_url,
            "content": content,
        }
        if embeds:
            payload["embeds"] = embeds

        # 9. 发送请求
        response = requests.post(
            webhook_url,
            json=payload,
            timeout=10,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        preview = (content[:50] + "...") if len(content) > 50 else content
        print(f"[转发成功] #{message.channel} | {username}: {preview}")
    except requests.HTTPError as e:
        print(f"[转发失败] HTTP {e.response.status_code}: {e.response.text[:200]}")
    except requests.RequestException as e:
        print(f"[转发失败] 网络错误: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"[转发失败] 未知错误: {type(e).__name__}: {e}")


def get_parent_channel_id(message: discord.Message) -> int | None:
    """返回线程/帖子所属的父频道 ID。普通频道没有父频道。"""
    channel = message.channel
    parent_id = getattr(channel, "parent_id", None)
    if parent_id:
        return int(parent_id)

    parent = getattr(channel, "parent", None)
    if parent and getattr(parent, "id", None):
        return int(parent.id)

    return None


def resolve_webhook_url(message: discord.Message) -> tuple[str | None, int | None, str]:
    """按当前频道优先，其次按父频道匹配转发规则。"""
    channel_id = int(message.channel.id)
    webhook_url = CHANNEL_TO_WEBHOOK.get(channel_id)
    if webhook_url:
        return webhook_url, channel_id, "当前频道"

    parent_id = get_parent_channel_id(message)
    if parent_id:
        webhook_url = CHANNEL_TO_WEBHOOK.get(parent_id)
        if webhook_url:
            return webhook_url, parent_id, "父频道"

    return None, None, "未匹配"


@client.event
async def on_ready():
    """登录成功后打印状态"""
    print(f"已登录: {client.user}")
    for cid, wh in CHANNEL_TO_WEBHOOK.items():
        print(f"  频道 {cid} -> Webhook")
    if DEBUG_MODE:
        print("[调试] 开启调试模式 - 将打印所有收到的消息及频道 ID")


@client.event
async def on_message(message: discord.Message):
    """
    收到消息时的处理逻辑
    """
    # 调试：打印收到的每条消息（便于确认是否收到、频道 ID 是否正确）
    webhook_url, matched_id, matched_by = resolve_webhook_url(message)
    if DEBUG_MODE:
        ch_name = getattr(message.channel, "name", str(message.channel))
        parent_id = get_parent_channel_id(message)
        is_self = message.author.id == client.user.id
        matched = webhook_url is not None
        print(
            f"[调试] 收到消息 | 频道ID={message.channel.id} | 父频道ID={parent_id or '-'} "
            f"| 频道={ch_name} | 匹配={matched} | 命中={matched_by}"
            f"{f'({matched_id})' if matched_id else ''} | 自己发的={is_self}"
        )

    # 忽略自己发的消息，防止循环
    if message.author.id == client.user.id:
        if DEBUG_MODE and webhook_url:
            print("[调试] 已忽略：自己发的消息不会转发")
        return

    # 只处理配置中的频道
    if not webhook_url:
        return

    # 使用线程池执行同步的 requests 请求，避免阻塞事件循环
    await asyncio.to_thread(forward_to_webhook, message, webhook_url)


# ==================== 入口 ====================
if __name__ == "__main__":
    if not USER_TOKEN or not CHANNEL_TO_WEBHOOK:
        print("请先运行 config_ui.py 配置 Token 和转发规则（频道 -> Webhook）")
        input("按回车键退出...")
        sys.exit(1)
    client.run(USER_TOKEN)
