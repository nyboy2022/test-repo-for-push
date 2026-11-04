"""Reliable Discord user-token channel to webhook forwarder."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import certifi

if os.name == "nt":
    _ca_dir = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "DiscordForwarder"
    _ca_dir.mkdir(parents=True, exist_ok=True)
    _ca_path = _ca_dir / "cacert.pem"
    shutil.copyfile(certifi.where(), _ca_path)
    os.environ["CURL_CA_BUNDLE"] = str(_ca_path)
    os.environ["REQUESTS_CA_BUNDLE"] = str(_ca_path)
    os.environ["SSL_CERT_FILE"] = str(_ca_path)
    certifi.where = lambda: str(_ca_path)

import discord
import requests

from redaction import redact

if os.name == "nt":
    os.environ.setdefault("TZ", "UTC")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATUS_PATH = BASE_DIR / "runtime_status.json"
LOG_PATH = BASE_DIR / "forwarder.log"
ALLOWED_WEBHOOK_HOSTS = {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com", "ptb.discordapp.com", "canary.discordapp.com"}
ATTACHMENT_HOSTS = {"cdn.discordapp.com", "media.discordapp.net"}
LOG_MAX_BYTES = 20 * 1024 * 1024
LOG_BACKUPS = 5
EMBED_TOTAL_LIMIT = 6000
MAX_FILES_PER_MESSAGE = 10
# Below the 10 MiB limit of servers without boosts, so uploads normally succeed first time.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
DELIVERY_ROUNDS = 3
DELIVERY_ROUND_DELAY = 30
RECONNECT_BASE_DELAY = 30
RECONNECT_MAX_DELAY = 900
EXIT_ALREADY_RUNNING = 3
EXIT_ALL_ACCOUNTS_FAILED = 2


def is_valid_webhook_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
    except (AttributeError, ValueError):
        return False
    return parsed.scheme == "https" and parsed.hostname in ALLOWED_WEBHOOK_HOSTS and parsed.path.startswith("/api/webhooks/") and len(parsed.path.split("/")) >= 5


def webhook_id(url: str) -> str:
    """The numeric ID is not secret; only the part after it is."""
    parts = urlparse(url).path.split("/")
    return parts[3] if len(parts) > 3 else ""


def split_content(text: str, limit: int = 2000) -> list[str]:
    if not text:
        return []
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:]
        if text.startswith("\n"):
            text = text[1:]
    if text:
        parts.append(text)
    return parts


def _trim(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def safe_username(value: str) -> str:
    """Discord rejects webhook usernames containing these words with HTTP 400."""
    value = re.sub(r"discord", lambda match: f"{match.group(0)[:4]}-{match.group(0)[4:]}", value, flags=re.I)
    value = re.sub(r"clyde", lambda match: f"{match.group(0)[:3]}-{match.group(0)[3:]}", value, flags=re.I)
    return value[:80] or "Unknown"


def _get(item: Any, key: str) -> Any:
    return item.get(key) if isinstance(item, dict) else getattr(item, key, None)


def message_sources(message: Any) -> list[Any]:
    snapshots = _get(message, "message_snapshots")
    nested = [(item.get("message") or {}) if isinstance(item, dict) else item for item in (snapshots or [])]
    return [message, *nested]


def message_text(message: Any) -> str:
    return "\n".join(str(_get(item, "content")) for item in message_sources(message) if _get(item, "content"))


def message_attachments(message: Any) -> list[Any]:
    return [item for source in message_sources(message) for item in (_get(source, "attachments") or []) if _get(item, "url")]


def message_content(message: Any) -> str:
    content = message_text(message)
    attachments = "\n".join(str(_get(item, "url")) for item in message_attachments(message))
    return f"{content}\n{attachments}".strip() if attachments else content


def embed_size(embed: dict[str, Any]) -> int:
    texts = (embed.get("title"), embed.get("description"), (embed.get("author") or {}).get("name"), (embed.get("footer") or {}).get("text"))
    fields = embed.get("fields") or []
    return sum(len(str(item)) for item in texts if item) + sum(len(str(item.get("name", ""))) + len(str(item.get("value", ""))) for item in fields)


def limit_embeds(embeds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep what fits in one request; Discord rejects more than 10 embeds or 6000 characters in total."""
    output: list[dict[str, Any]] = []; total = 0
    for embed in embeds[:10]:
        size = embed_size(embed)
        if total + size > EMBED_TOTAL_LIMIT: break
        output.append(embed); total += size
    return output


@dataclass(frozen=True)
class Settings:
    account_id: str
    account_name: str
    token: str
    rules: dict[int, str]
    automated_channels: frozenset[int]
    debug: bool
    ignore_bots: bool
    max_retries: int
    queue_size: int
    destination_webhooks: frozenset[int] = field(default_factory=frozenset)


def load_all_settings() -> list[Settings]:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取 config.json: {exc}") from exc
    raw_accounts = data.get("accounts")
    if isinstance(raw_accounts, list) and raw_accounts:
        accounts = [(str(item.get("id", "")).strip(), str(item.get("name", "")).strip(), str(item.get("user_token", "")).strip()) for item in raw_accounts if isinstance(item, dict)]
    else:
        accounts = [("default", "主账号", str(data.get("user_token", "")).strip())]
    default_id = accounts[0][0] if accounts else "default"
    rules_by_account: dict[str, dict[int, str]] = {item[0]: {} for item in accounts}
    automated_by_account: dict[str, set[int]] = {item[0]: set() for item in accounts}
    for item in data.get("forward_rules", []):
        try:
            account_id = str(item.get("account_id") or default_id).strip()
            channel_id = int(str(item.get("channel_id", "")).strip())
            webhook_url = str(item.get("webhook_url", "")).strip()
        except (AttributeError, TypeError, ValueError):
            continue
        if account_id in rules_by_account and channel_id > 0 and is_valid_webhook_url(webhook_url):
            rules_by_account[account_id][channel_id] = webhook_url
            if bool(item.get("allow_automated", False)):
                automated_by_account[account_id].add(channel_id)
    options = data.get("options") or {}
    destinations = frozenset(int(webhook_id(url)) for rules in rules_by_account.values() for url in rules.values() if webhook_id(url).isdigit())
    return [Settings(
        account_id=account_id, account_name=name or account_id, token=token, rules=rules_by_account.get(account_id, {}), automated_channels=frozenset(automated_by_account.get(account_id, set())),
        debug=bool(data.get("debug", False)), ignore_bots=bool(options.get("ignore_bots", True)), max_retries=max(0, min(int(options.get("max_retries", 4)), 10)), queue_size=max(10, min(int(options.get("queue_size", 500)), 5000)), destination_webhooks=destinations,
    ) for account_id, name, token in accounts]


def load_settings() -> Settings:
    settings = load_all_settings()
    if not settings: raise RuntimeError("没有配置 Discord 账号")
    return settings[0]


def build_embeds(message: discord.Message) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    embeds = [embed for source in message_sources(message) for embed in getattr(source, "embeds", [])][:10]
    for embed in embeds:
        item: dict[str, Any] = {}
        if embed.title: item["title"] = _trim(embed.title, 256)
        if embed.description: item["description"] = _trim(embed.description, 4096)
        if embed.url: item["url"] = str(embed.url)
        if embed.color is not None: item["color"] = int(embed.color)
        if embed.author and embed.author.name:
            item["author"] = {"name": _trim(embed.author.name, 256)}
            if embed.author.url: item["author"]["url"] = str(embed.author.url)
            if embed.author.icon_url: item["author"]["icon_url"] = str(embed.author.icon_url)
        if embed.footer and embed.footer.text:
            item["footer"] = {"text": _trim(embed.footer.text, 2048)}
            if embed.footer.icon_url: item["footer"]["icon_url"] = str(embed.footer.icon_url)
        if embed.image and embed.image.url: item["image"] = {"url": str(embed.image.url)}
        if embed.thumbnail and embed.thumbnail.url: item["thumbnail"] = {"url": str(embed.thumbnail.url)}
        fields = [{"name": _trim(f.name, 256) or "\u200b", "value": _trim(f.value, 1024) or "\u200b", "inline": bool(f.inline)} for f in embed.fields[:25]]
        if fields: item["fields"] = fields
        if item: output.append(item)
    return limit_embeds(output)


class WebhookError(RuntimeError):
    """Delivery failed. The message never contains the webhook URL."""

    def __init__(self, message: str, status: int | None = None, permanent: bool = False) -> None:
        super().__init__(message); self.status = status; self.permanent = permanent


class RuntimeState:
    def __init__(self, settings: list[Settings] | None = None) -> None:
        self.started_at = time.time(); self.connected = False; self.account = None; self.guilds = 0
        self.received = 0; self.forwarded = 0; self.failed = 0; self.dropped = 0; self.queue_depth = 0
        self.last_message_at = None; self.last_forward_at = None; self.last_error = None; self.last_error_at = None
        self.accounts = {item.account_id: {"name": item.account_name, "connected": False, "account": None, "guilds": 0, "rule_count": len(item.rules), "last_error": None} for item in (settings or [])}
        self.webhooks: dict[str, dict[str, Any]] = {}
        self._write_lock = threading.Lock(); self._last_write_warning = 0.0

    def update_account(self, account_id: str, **changes: Any) -> None:
        entry = self.accounts.setdefault(account_id, {"name": account_id, "connected": False, "account": None, "guilds": 0, "rule_count": 0, "last_error": None})
        entry.update(changes)
        active = list(self.accounts.values())
        self.connected = bool(active) and all(bool(item.get("connected")) for item in active)
        self.account = ", ".join(str(item.get("account")) for item in active if item.get("account")) or None
        self.guilds = sum(int(item.get("guilds") or 0) for item in active)
        self.write()

    def record_delivery(self, hook_id: str, error: Exception | None = None) -> None:
        entry = self.webhooks.setdefault(hook_id, {"ok": True, "status": None, "forwarded": 0, "failures": 0, "last_ok_at": None, "last_failed_at": None, "last_error": None})
        now = time.time()
        if error is None:
            self.forwarded += 1; self.last_forward_at = now
            entry.update(ok=True, status=None, last_ok_at=now, last_error=None, forwarded=entry["forwarded"] + 1)
        else:
            text = redact(f"{type(error).__name__}: {error}")[:500]
            self.failed += 1; self.last_error = text; self.last_error_at = now
            entry.update(ok=False, status=getattr(error, "status", None), last_failed_at=now, last_error=text, failures=entry["failures"] + 1)

    def write(self) -> None:
        """Status output is best effort: it must never take down delivery."""
        payload = {key: value for key, value in self.__dict__.items() if not key.startswith("_")}
        payload.update(pid=os.getpid(), updated_at=time.time())
        with self._write_lock:
            temp = STATUS_PATH.with_name(f".{STATUS_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                temp.write_text(redact(json.dumps(payload, ensure_ascii=False, indent=2)), encoding="utf-8")
                # On Windows the replace fails while the admin console has the file open.
                for attempt in range(5):
                    try:
                        os.replace(temp, STATUS_PATH); return
                    except PermissionError:
                        if attempt == 4: raise
                        time.sleep(.05)
            except OSError as exc:
                if time.time() - self._last_write_warning > 300:
                    self._last_write_warning = time.time(); logging.warning("无法写入状态文件: %s", exc)
            finally:
                try: temp.unlink(missing_ok=True)
                except OSError: pass


@dataclass
class QueueItem:
    message: Any
    next_part: int = 0
    plan: list[tuple[dict[str, Any], list[tuple[str, bytes, str]], str]] | None = None


class Forwarder:
    def __init__(self, settings: Settings, state: RuntimeState | None = None) -> None:
        self.settings = settings; self.state = state or RuntimeState([settings])
        self.queues: dict[str, asyncio.Queue[QueueItem]] = {}; self.workers: dict[str, asyncio.Task] = {}; self.sessions: dict[str, requests.Session] = {}
        self.seen_ids: deque[int] = deque(maxlen=5000); self.seen_set: set[int] = set()
        self._reconnect_delay = RECONNECT_BASE_DELAY
        self._new_client()

    def _new_client(self) -> None:
        self.client = discord.Client(guild_subscriptions=True)
        self._bind_events()

    def _bind_events(self) -> None:
        @self.client.event
        async def on_ready() -> None:
            self._reconnect_delay = RECONNECT_BASE_DELAY
            self.state.update_account(self.settings.account_id, connected=True, account=str(self.client.user), guilds=len(self.client.guilds), last_error=None)
            logging.info("[%s] 已登录 %s，监听 %d 条规则", self.settings.account_name, self.client.user, len(self.settings.rules))

        @self.client.event
        async def on_disconnect() -> None:
            self.state.update_account(self.settings.account_id, connected=False); logging.warning("[%s] Discord 连接已断开，客户端将自动重连", self.settings.account_name)

        @self.client.event
        async def on_resumed() -> None:
            self.state.update_account(self.settings.account_id, connected=True); logging.info("[%s] Discord 会话已恢复", self.settings.account_name)

        @self.client.event
        async def on_message(message: discord.Message) -> None:
            await self._on_message(message)

    @staticmethod
    def _source_ids(message: discord.Message) -> list[int]:
        """Return channel, thread parent, and category IDs from nearest to broadest."""
        channel = message.channel; output: list[int] = []
        for value in (getattr(channel, "id", None), getattr(channel, "parent_id", None), getattr(channel, "category_id", None)):
            if value and int(value) not in output: output.append(int(value))
        parent = getattr(channel, "parent", None)
        category = getattr(channel, "category", None)
        for value in (getattr(parent, "id", None), getattr(parent, "category_id", None), getattr(category, "id", None)):
            if value and int(value) not in output: output.append(int(value))
        parent_category = getattr(parent, "category", None)
        if parent_category and getattr(parent_category, "id", None) and int(parent_category.id) not in output: output.append(int(parent_category.id))
        return output

    def _webhook_for(self, message: discord.Message) -> str | None:
        return next((self.settings.rules[item] for item in self._source_ids(message) if item in self.settings.rules), None)

    def _allows_automated(self, message: discord.Message) -> bool:
        return any(item in self.settings.automated_channels for item in self._source_ids(message))

    def _remember(self, message_id: int) -> bool:
        if message_id in self.seen_set: return False
        if len(self.seen_ids) == self.seen_ids.maxlen: self.seen_set.discard(self.seen_ids[0])
        self.seen_ids.append(message_id); self.seen_set.add(message_id); return True

    def _queue_for(self, webhook_url: str) -> asyncio.Queue[QueueItem]:
        """Each destination has its own queue, so one failing webhook cannot delay the others."""
        queue = self.queues.get(webhook_url)
        if queue is None:
            queue = self.queues[webhook_url] = asyncio.Queue(self.settings.queue_size)
        worker = self.workers.get(webhook_url)
        if worker is None or worker.done():
            self.workers[webhook_url] = asyncio.create_task(self._worker(webhook_url, queue))
        return queue

    async def _on_message(self, message: discord.Message) -> None:
        self.state.received += 1; self.state.last_message_at = time.time()
        webhook_url = self._webhook_for(message)
        if not webhook_url: return
        if message.author.id == self.client.user.id: return
        source_webhook = getattr(message, "webhook_id", None)
        # A message posted by one of our own destinations would otherwise loop forever.
        if source_webhook and int(source_webhook) in self.settings.destination_webhooks: return
        allow_automated = self._allows_automated(message)
        if source_webhook and not allow_automated: return
        if self.settings.ignore_bots and getattr(message.author, "bot", False) and not allow_automated: return
        if not self._remember(int(message.id)): return
        try:
            self._queue_for(webhook_url).put_nowait(QueueItem(message)); self.state.queue_depth += 1
        except asyncio.QueueFull:
            hook_id = webhook_id(webhook_url)
            self.state.dropped += 1; self.state.last_error = f"Webhook {hook_id} 的转发队列已满"; self.state.last_error_at = time.time()
            logging.error("Webhook %s 队列已满，丢弃消息 %s", hook_id, message.id)
        self.state.write()

    async def _worker(self, webhook_url: str, queue: asyncio.Queue[QueueItem]) -> None:
        while True:
            item = await queue.get()
            try:
                await self._process(item, webhook_url)
            finally:
                queue.task_done(); self.state.queue_depth = max(0, self.state.queue_depth - 1); self.state.write()

    async def _process(self, item: QueueItem, webhook_url: str) -> None:
        hook_id = webhook_id(webhook_url); message_id = getattr(item.message, "id", "?")
        rounds = DELIVERY_ROUNDS if self.settings.max_retries else 1
        for round_number in range(1, rounds + 1):
            try:
                await asyncio.to_thread(self._deliver, item, webhook_url)
                self.state.record_delivery(hook_id); return
            except WebhookError as exc:
                if exc.permanent or round_number == rounds:
                    self.state.record_delivery(hook_id, exc)
                    logging.error("[%s] 转发消息 %s 失败 webhook=%s: %s", self.settings.account_name, message_id, hook_id, exc); return
                logging.warning("[%s] 消息 %s 暂时无法投递，%d 秒后从第 %d 段继续: %s", self.settings.account_name, message_id, DELIVERY_ROUND_DELAY, item.next_part + 1, exc)
                await asyncio.sleep(DELIVERY_ROUND_DELAY)
            except Exception as exc:
                self.state.record_delivery(hook_id, exc)
                logging.exception("[%s] 转发消息 %s 失败 webhook=%s", self.settings.account_name, message_id, hook_id); return

    def _session_for(self, webhook_url: str) -> requests.Session:
        return self.sessions.setdefault(webhook_url, requests.Session())

    def _download_attachments(self, message: discord.Message, session: requests.Session) -> tuple[list[tuple[str, bytes, str]], list[str]]:
        """Re-upload attachments because Discord CDN links expire. Whatever cannot be uploaded stays a link."""
        files: list[tuple[str, bytes, str]] = []; links: list[str] = []; total = 0
        for attachment in message_attachments(message):
            url = str(_get(attachment, "url")); size = int(_get(attachment, "size") or 0)
            if len(files) >= MAX_FILES_PER_MESSAGE or urlparse(url).hostname not in ATTACHMENT_HOSTS or not size or total + size > MAX_UPLOAD_BYTES:
                links.append(url); continue
            try:
                response = session.get(url, timeout=(5, 30)); response.raise_for_status(); data = response.content
            except requests.RequestException as exc:
                logging.warning("附件下载失败，改为发送链接: %s", redact(exc)); links.append(url); continue
            if total + len(data) > MAX_UPLOAD_BYTES:
                links.append(url); continue
            total += len(data)
            files.append((str(_get(attachment, "filename") or f"file{len(files)}"), data, str(_get(attachment, "content_type") or "application/octet-stream")))
        return files, links

    def _build_plan(self, message: discord.Message, session: requests.Session) -> list[tuple[dict[str, Any], list[tuple[str, bytes, str]], str]]:
        guild_name = message.guild.name if message.guild else "DM"
        username = safe_username(f"[{guild_name}] {message.author.display_name}")
        files, links = self._download_attachments(message, session)
        content = "\n".join(part for part in (message_text(message), *links) if part)
        parts = split_content(content); embeds = build_embeds(message)
        if not parts: parts = ["" if embeds or files else "*(无文字)*"]
        # The original links are the fallback for a destination that rejects the upload size.
        fallback = "\n".join(str(_get(item, "url")) for item in message_attachments(message)) if files else ""
        plan = []
        for index, part in enumerate(parts):
            payload: dict[str, Any] = {"username": username, "avatar_url": str(message.author.display_avatar.url), "content": part, "allowed_mentions": {"parse": []}}
            if index == 0 and embeds: payload["embeds"] = embeds
            plan.append((payload, files if index == 0 else [], fallback if index == 0 else ""))
        return plan

    def _deliver(self, item: QueueItem, webhook_url: str) -> None:
        session = self._session_for(webhook_url); message = item.message
        if item.plan is None: item.plan = self._build_plan(message, session)
        # next_part lets a later round resume without re-sending parts that already arrived.
        while item.next_part < len(item.plan):
            payload, files, fallback = item.plan[item.next_part]
            try:
                self._post_with_retry(webhook_url, payload, files)
            except WebhookError as exc:
                if exc.status != 413 or not files: raise
                logging.warning("[%s] 附件超过目标服务器限制，改为发送链接 message=%s", self.settings.account_name, message.id)
                content = "\n".join(part for part in (payload.get("content"), fallback) if part)
                pieces = split_content(content) or [""]
                base = {key: value for key, value in payload.items() if key not in {"content", "embeds"}}
                replacement = [({**base, "content": piece, **({"embeds": payload["embeds"]} if index == 0 and "embeds" in payload else {})}, [], "") for index, piece in enumerate(pieces)]
                item.plan[item.next_part:item.next_part + 1] = replacement
                continue
            item.next_part += 1
        logging.info("[%s] 转发成功 channel=%s message=%s parts=%d", self.settings.account_name, message.channel.id, message.id, len(item.plan))
        item.plan = None

    def _post_with_retry(self, url: str, payload: dict[str, Any], files: list[tuple[str, bytes, str]] | None = None) -> None:
        session = self._session_for(url)
        request: dict[str, Any] = {"data": {"payload_json": json.dumps(payload)}, "files": [(f"files[{index}]", item) for index, item in enumerate(files)]} if files else {"json": payload}
        last_error = "超过重试次数"; last_status: int | None = None
        for attempt in range(self.settings.max_retries + 1):
            backoff = min(2 ** attempt, 15) if attempt < self.settings.max_retries else 0
            try:
                response = session.post(url, timeout=(5, 60 if files else 15), **request)
            except requests.RequestException as exc:
                last_error = redact(f"{type(exc).__name__}: {exc}"); last_status = None
                time.sleep(backoff); continue
            status = response.status_code; last_status = status
            if status == 429:
                last_error = "HTTP 429 限流"
                time.sleep(self._retry_after(response)); continue
            if status >= 500:
                last_error = f"HTTP {status}"
                time.sleep(backoff); continue
            if status >= 400:
                # Retrying cannot fix a deleted webhook or a rejected payload.
                raise WebhookError(f"Webhook 返回 HTTP {status}", status, permanent=status != 408)
            return
        raise WebhookError(f"Webhook 投递失败: {last_error}", last_status)

    @staticmethod
    def _retry_after(response: requests.Response) -> float:
        value = response.headers.get("Retry-After")
        try:
            if value is None: value = response.json().get("retry_after", 1)
            return max(min(float(value), 30), .25)
        except (TypeError, ValueError, AttributeError):
            return 1.0

    async def start(self) -> None:
        """Keep this account connected. Only a rejected token ends the loop, because retrying it cannot succeed."""
        name = self.settings.account_name; account_id = self.settings.account_id
        try:
            while True:
                try:
                    await self.client.start(self.settings.token, reconnect=True)
                    error = "Discord 客户端已退出"
                except discord.LoginFailure:
                    self.state.update_account(account_id, connected=False, last_error="Token 无效或已失效，请在后台更新后重启")
                    logging.error("[%s] Token 无效或已失效，此账号已停止", name); return
                except Exception as exc:
                    error = redact(f"{type(exc).__name__}: {exc}")[:500]
                    logging.exception("[%s] Discord 客户端异常退出", name)
                delay = self._reconnect_delay; self._reconnect_delay = min(delay * 2, RECONNECT_MAX_DELAY)
                self.state.update_account(account_id, connected=False, last_error=f"{error}（{delay} 秒后重连）")
                logging.warning("[%s] %d 秒后重新连接", name, delay)
                try: await self.client.close()
                except Exception: logging.debug("关闭旧客户端失败", exc_info=True)
                await asyncio.sleep(delay)
                self._new_client()
        finally:
            for session in self.sessions.values(): session.close()


async def heartbeat(state: RuntimeState) -> None:
    while True:
        await asyncio.sleep(10)
        state.write()


async def run_forwarders(settings: list[Settings]) -> None:
    state = RuntimeState(settings); forwarders = [Forwarder(item, state) for item in settings]
    state.write()
    beat = asyncio.create_task(heartbeat(state))
    try:
        await asyncio.gather(*(item.start() for item in forwarders))
    finally:
        beat.cancel()


class RedactingFormatter(logging.Formatter):
    """Applied to the final text so tracebacks are covered as well."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class RepeatFilter(logging.Filter):
    """discord.py-self emits this warning constantly; once an hour is enough to notice it."""

    NOISY = "User has more than 15 active sessions"

    def __init__(self, interval: float = 3600) -> None:
        super().__init__(); self.interval = interval; self.last_seen: float | None = None

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.getMessage().startswith(self.NOISY): return True
        now = time.monotonic()
        if self.last_seen is not None and now - self.last_seen < self.interval: return False
        self.last_seen = now; return True


def setup_logging(debug: bool) -> None:
    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(message)s"); noise = RepeatFilter()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout), RotatingFileHandler(LOG_PATH, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")]
    for handler in handlers:
        handler.setFormatter(formatter); handler.addFilter(noise)
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, handlers=handlers)
    # urllib3 DEBUG lines include the complete webhook URL, including its secret.
    logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)


_instance_mutex = None


def acquire_single_instance() -> bool:
    """Two forwarders would post every message twice, so a second one must refuse to run."""
    global _instance_mutex
    if os.name != "nt": return True
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _instance_mutex = kernel32.CreateMutexW(None, False, "Local\\DiscordForwarder-Process")
    return ctypes.get_last_error() != 183


def main() -> int:
    try:
        if not acquire_single_instance():
            print("启动失败: 已有另一个转发进程在运行，请先关闭它", file=sys.stderr); return EXIT_ALREADY_RUNNING
        settings = load_all_settings()
        if not settings or any(not item.token for item in settings): raise RuntimeError("有 Discord 账号未配置 User Token")
        if not any(item.rules for item in settings): raise RuntimeError("没有有效的转发规则")
        setup_logging(any(item.debug for item in settings))
        asyncio.run(run_forwarders(settings))
        print("所有账号都已停止，请检查 Token", file=sys.stderr); return EXIT_ALL_ACCOUNTS_FAILED
    except KeyboardInterrupt: return 0
    except Exception as exc:
        print(redact(f"启动失败: {type(exc).__name__}: {exc}"), file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
