"""Backfill all readable messages from every text channel in a configured category."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

import requests

from backfill_recent import API_ROOT, CONFIG_PATH, ChannelUnavailable, SentLog, account_token, destination_ids, fetch_recent, find_rule, get_with_rate_limit, own_user_id, payloads_for, post_with_retry, run_safely, safe_username, should_forward, webhook_wait_url
from discord_forwarder import is_valid_webhook_url, webhook_id


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("category_id"); parser.add_argument("--days", type=float, default=4); parser.add_argument("--limit", type=int); parser.add_argument("--dry-run", action="store_true"); parser.add_argument("--account"); parser.add_argument("--force", action="store_true", help="send messages again even if already backfilled")
    args = parser.parse_args(); config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    rule = find_rule(config, args.category_id, args.account)
    webhook_url = str(rule.get("webhook_url") or "")
    if not is_valid_webhook_url(webhook_url): raise RuntimeError("Configured webhook is invalid")
    token = account_token(config, rule)
    if not token: raise RuntimeError("User Token is missing")
    source = requests.Session(); source.headers.update({"Authorization": token, "User-Agent": "Mozilla/5.0"})
    category_response = get_with_rate_limit(source, f"{API_ROOT}/channels/{args.category_id}")
    if category_response.status_code != 200: raise RuntimeError(f"Category API returned HTTP {category_response.status_code}")
    category = category_response.json(); guild_id = str(category.get("guild_id") or "")
    channels_response = get_with_rate_limit(source, f"{API_ROOT}/guilds/{guild_id}/channels")
    if channels_response.status_code != 200: raise RuntimeError(f"Guild channels API returned HTTP {channels_response.status_code}")
    channels = [item for item in channels_response.json() if str(item.get("parent_id") or "") == args.category_id and int(item.get("type", -1)) in {0, 5}]
    channels.sort(key=lambda item: (int(item.get("position", 0)), str(item.get("id"))))
    beginning = datetime.now(timezone.utc) - timedelta(days=args.days); collected = []; inaccessible = []
    sent = SentLog(); hook_id = webhook_id(webhook_url); me = own_user_id(source); destinations = destination_ids(config)
    for channel in channels:
        try:
            messages = [item for item in fetch_recent(source, str(channel["id"]), beginning) if should_forward(item, rule, config.get("options") or {}, me, destinations)]
        except ChannelUnavailable as exc:
            inaccessible.append(channel); print(f"channel_id={channel['id']} name={channel.get('name', '')!r} skipped={exc}"); continue
        if not args.force: messages = [item for item in messages if not sent.has(hook_id, str(item["id"]))]
        if args.limit is not None: messages = messages[-max(args.limit, 0):]
        collected.append((channel, messages)); print(f"channel_id={channel['id']} name={channel.get('name', '')!r} messages={len(messages)}")
    total = sum(len(messages) for _, messages in collected); print(f"channels={len(channels)} readable_channels={len(collected)} inaccessible_channels={len(inaccessible)} total_messages={total}")
    if args.dry_run: source.close(); return 0
    destination = requests.Session(); url = webhook_wait_url(webhook_url); completed = 0
    for channel, messages in collected:
        label = f"#{channel.get('name') or channel['id']}"
        for message in messages:
            for payload in payloads_for(message):
                payload["username"] = safe_username(f"[{label}] {payload['username']}")
                post_with_retry(destination, url, payload)
            sent.add(hook_id, str(message["id"]))
            completed += 1; print(f"forwarded={completed}/{total} channel_id={channel['id']} source_id={message['id']}")
    source.close(); destination.close(); print(f"complete={completed}"); return 0


if __name__ == "__main__": raise SystemExit(run_safely(main))
