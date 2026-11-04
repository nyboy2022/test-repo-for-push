"""Backfill recent messages from one configured Discord channel to its webhook."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from discord_forwarder import is_valid_webhook_url, limit_embeds, message_content, message_sources, safe_username, split_content, webhook_id
from redaction import redact

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
SENT_PATH = BASE_DIR / "backfill_sent.json"
API_ROOT = "https://discord.com/api/v9"
MAX_RATE_LIMIT_RETRIES = 8


class ChannelUnavailable(RuntimeError):
    """The account cannot read this channel, as opposed to a temporary failure."""


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def clean_embed(source: dict[str, Any]) -> dict[str, Any]:
    embed: dict[str, Any] = {}
    for key, limit in (("title", 256), ("description", 4096)):
        if source.get(key): embed[key] = str(source[key])[:limit]
    for key in ("url", "color", "timestamp"):
        if source.get(key) is not None:
            embed[key] = source[key]
    for key, fields in {
        "author": ("name", "url", "icon_url"),
        "footer": ("text", "icon_url"),
        "image": ("url",),
        "thumbnail": ("url",),
    }.items():
        value = source.get(key)
        if isinstance(value, dict):
            cleaned = {name: value[name] for name in fields if value.get(name) is not None}
            if cleaned:
                embed[key] = cleaned
    output_fields = []
    for field in (source.get("fields") or [])[:25]:
        if isinstance(field, dict):
            output_fields.append({"name": str(field.get("name") or "​")[:256], "value": str(field.get("value") or "​")[:1024], "inline": bool(field.get("inline", False))})
    if output_fields:
        embed["fields"] = output_fields
    return embed


def avatar_url(author: dict[str, Any]) -> str | None:
    user_id, avatar = str(author.get("id") or ""), str(author.get("avatar") or "")
    if not user_id or not avatar:
        return None
    extension = "gif" if avatar.startswith("a_") else "png"
    return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.{extension}?size=128"


def webhook_wait_url(url: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True)); query["wait"] = "true"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _retry_delay(response: requests.Response) -> float:
    try: return max(.25, min(float(response.json().get("retry_after", 1)), 30))
    except (ValueError, AttributeError, TypeError): return 1.0


def post_with_retry(session: requests.Session, url: str, payload: dict[str, Any], retries: int = 5) -> None:
    for attempt in range(retries + 1):
        try:
            response = session.post(url, json=payload, timeout=(5, 20))
        except requests.RequestException as exc:
            if attempt == retries: raise RuntimeError(f"Webhook request failed: {type(exc).__name__}") from None
            time.sleep(min(2 ** attempt, 15)); continue
        if response.status_code == 429:
            time.sleep(_retry_delay(response)); continue
        if response.status_code >= 500 and attempt < retries:
            time.sleep(min(2 ** attempt, 15)); continue
        if 200 <= response.status_code < 300:
            return
        raise RuntimeError(f"Webhook returned HTTP {response.status_code}")
    raise RuntimeError("Webhook retry limit exceeded")


def get_with_rate_limit(session: requests.Session, url: str, **kwargs: Any) -> requests.Response:
    """Wait out HTTP 429 so rate limiting is never mistaken for a missing channel."""
    for _ in range(MAX_RATE_LIMIT_RETRIES):
        response = session.get(url, timeout=(5, 20), **kwargs)
        if response.status_code != 429:
            return response
        time.sleep(_retry_delay(response))
    raise RuntimeError("Discord API rate limit retries exhausted")


def fetch_recent(session: requests.Session, channel_id: str, cutoff: datetime) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []; before: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 100}
        if before: params["before"] = before
        response = get_with_rate_limit(session, f"{API_ROOT}/channels/{channel_id}/messages", params=params)
        if response.status_code in {403, 404}:
            raise ChannelUnavailable(f"HTTP {response.status_code}")
        if response.status_code != 200:
            raise RuntimeError(f"Discord history API returned HTTP {response.status_code}")
        page = response.json()
        if not isinstance(page, list) or not page: break
        messages.extend(item for item in page if parse_time(item["timestamp"]) >= cutoff)
        oldest = parse_time(page[-1]["timestamp"]); before = str(page[-1]["id"])
        if len(page) < 100 or oldest < cutoff: break
    messages.sort(key=lambda item: parse_time(item["timestamp"]))
    return messages


def should_forward(message: dict[str, Any], rule: dict[str, Any], options: dict[str, Any], own_user_id: str, destinations: set[str]) -> bool:
    """The same filters the live forwarder applies."""
    author = message.get("author") or {}
    if str(author.get("id")) == own_user_id: return False
    source_webhook = str(message.get("webhook_id") or "")
    if source_webhook in destinations: return False
    allow_automated = bool(rule.get("allow_automated", False))
    if source_webhook and not allow_automated: return False
    if bool(options.get("ignore_bots", True)) and author.get("bot") and not allow_automated: return False
    return True


def destination_ids(config: dict[str, Any]) -> set[str]:
    return {webhook_id(str(item.get("webhook_url") or "")) for item in config.get("forward_rules", [])} - {""}


def own_user_id(session: requests.Session) -> str:
    response = get_with_rate_limit(session, f"{API_ROOT}/users/@me")
    if response.status_code != 200: raise RuntimeError(f"Discord user API returned HTTP {response.status_code}")
    return str(response.json().get("id") or "")


def payloads_for(message: dict[str, Any]) -> list[dict[str, Any]]:
    author = message.get("author") or {}; display_name = author.get("global_name") or author.get("username") or "Discord"
    original_time = parse_time(message["timestamp"]).astimezone().strftime("%m-%d %H:%M")
    username = safe_username(f"[历史 {original_time}] {display_name}")
    content = message_content(message)
    parts = split_content(content); embeds = [clean_embed(item) for source in message_sources(message) for item in (source.get("embeds") or [])][:10]
    embeds = limit_embeds([item for item in embeds if item])
    if not parts: parts = ["" if embeds else "*(无文字)*"]
    result = []
    for index, part in enumerate(parts):
        payload: dict[str, Any] = {"username": username, "content": part, "allowed_mentions": {"parse": []}}
        avatar = avatar_url(author)
        if avatar: payload["avatar_url"] = avatar
        if index == 0 and embeds: payload["embeds"] = embeds
        result.append(payload)
    return result


class SentLog:
    """Remembers what was backfilled, per destination, so running a backfill twice sends nothing twice."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or SENT_PATH
        try: self.data = {key: set(value) for key, value in json.loads(self.path.read_text(encoding="utf-8")).items()}
        except (OSError, ValueError, AttributeError, TypeError): self.data = {}

    def has(self, hook_id: str, message_id: str) -> bool:
        return message_id in self.data.get(hook_id, set())

    def add(self, hook_id: str, message_id: str) -> None:
        self.data.setdefault(hook_id, set()).add(message_id)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({key: sorted(value) for key, value in self.data.items()}), encoding="utf-8")
        temporary.replace(self.path)


def find_rule(config: dict[str, Any], source_id: str, account_id: str | None) -> dict[str, Any]:
    rules = [item for item in config.get("forward_rules", []) if str(item.get("channel_id")) == source_id]
    if account_id: rules = [item for item in rules if str(item.get("account_id")) == account_id]
    if not rules: raise RuntimeError("No forwarding rule for this ID")
    if len(rules) > 1: raise RuntimeError("Several accounts have a rule for this ID; choose one with --account")
    return rules[0]


def account_token(config: dict[str, Any], rule: dict[str, Any]) -> str:
    accounts = config.get("accounts") or []
    account_id = str(rule.get("account_id") or (accounts[0].get("id") if accounts else "default"))
    account = next((item for item in accounts if str(item.get("id")) == account_id), None)
    return str((account or {}).get("user_token") or config.get("user_token") or "").strip()


def run_safely(function: Callable[[], int]) -> int:
    """Print failures without a traceback, which could carry a webhook URL."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        return function()
    except (RuntimeError, OSError, ValueError, requests.RequestException) as exc:
        print(redact(f"failed: {type(exc).__name__}: {exc}"), file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("channel_id"); parser.add_argument("--days", type=float, default=4); parser.add_argument("--limit", type=int); parser.add_argument("--snapshots-only", action="store_true"); parser.add_argument("--dry-run", action="store_true"); parser.add_argument("--account"); parser.add_argument("--force", action="store_true", help="send messages again even if already backfilled")
    args = parser.parse_args(); config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    rule = find_rule(config, args.channel_id, args.account)
    webhook_url = str(rule.get("webhook_url") or "")
    if not is_valid_webhook_url(webhook_url): raise RuntimeError("Configured webhook is invalid")
    token = account_token(config, rule)
    if not token: raise RuntimeError("User Token is missing")
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    source = requests.Session(); source.headers.update({"Authorization": token, "User-Agent": "Mozilla/5.0"})
    sent = SentLog(); hook_id = webhook_id(webhook_url); me = own_user_id(source); destinations = destination_ids(config)
    messages = [item for item in fetch_recent(source, args.channel_id, cutoff) if should_forward(item, rule, config.get("options") or {}, me, destinations)]
    if args.snapshots_only: messages = [item for item in messages if item.get("message_snapshots")]
    if not args.force: messages = [item for item in messages if not sent.has(hook_id, str(item["id"]))]
    if args.limit is not None: messages = messages[-max(args.limit, 0):]
    print(f"messages={len(messages)} cutoff_utc={cutoff:%Y-%m-%dT%H:%M:%SZ}")
    if args.dry_run: source.close(); return 0
    destination = requests.Session(); url = webhook_wait_url(webhook_url)
    for index, message in enumerate(messages, 1):
        for payload in payloads_for(message): post_with_retry(destination, url, payload)
        sent.add(hook_id, str(message["id"]))
        print(f"forwarded={index}/{len(messages)} source_id={message['id']}")
    source.close(); destination.close(); print(f"complete={len(messages)}"); return 0


if __name__ == "__main__": raise SystemExit(run_safely(main))
