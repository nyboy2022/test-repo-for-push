"""Regression tests for delivery reliability and credential handling. Nothing here touches the network."""
import asyncio
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import discord_forwarder as core
from redaction import redact, redact_value

SECRET = "SuperSecretWebhookValue-123"
URL = f"https://discord.com/api/webhooks/555/{SECRET}"


class FakeObject:
    def __init__(self, **values): self.__dict__.update(values)


def response(status, headers=None, body=None):
    item = MagicMock(status_code=status, headers=headers or {})
    item.json.return_value = body or {}
    return item


def settings(**changes):
    values = dict(account_id="one", account_name="一号", token="t" * 40, rules={100: URL}, automated_channels=frozenset(), debug=False, ignore_bots=True, max_retries=4, queue_size=10)
    values.update(changes)
    return core.Settings(**values)


def incoming(message_id=1, webhook=None, bot=False, channel_id=100):
    return FakeObject(id=message_id, webhook_id=webhook, author=FakeObject(id=7, bot=bot, display_name="user", display_avatar=FakeObject(url="https://cdn/a.png")), channel=FakeObject(id=channel_id), guild=FakeObject(name="Server"), content="hello", attachments=[], embeds=[])


class Base(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        patcher = patch.object(core, "STATUS_PATH", Path(self.folder.name) / "status.json"); patcher.start(); self.addCleanup(patcher.stop)
        sleeper = patch.object(core.time, "sleep"); self.sleep = sleeper.start(); self.addCleanup(sleeper.stop)

    def forwarder(self, **changes):
        with patch.object(core.discord, "Client", return_value=MagicMock()):
            forwarder = core.Forwarder(settings(**changes))
        forwarder.client.user = FakeObject(id=999)
        return forwarder

    def with_session(self, forwarder, responses):
        session = MagicMock(); session.post.side_effect = responses
        forwarder.sessions[URL] = session
        return session


class RedactionTests(unittest.TestCase):
    def test_webhook_secret_removed_and_id_kept(self):
        text = redact(f"404 Not Found for url: {URL}")
        self.assertNotIn(SECRET, text); self.assertIn("555", text)

    def test_token_removed(self):
        self.assertNotIn("abcdefghijklmnopqrstuvwx", redact("abcdefghijklmnopqrstuvwx.abcdef.abcdefghijklmnopqrstuvwxyz"))

    def test_nested_values(self):
        self.assertNotIn(SECRET, json.dumps(redact_value({"a": [{"b": URL}], "n": 3})))

    def test_formatter_covers_tracebacks(self):
        try:
            raise RuntimeError(f"failed {URL}")
        except RuntimeError:
            record = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", None, sys.exc_info())
        self.assertNotIn(SECRET, core.RedactingFormatter("%(message)s").format(record))

    def test_status_file_never_contains_secret(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(core, "STATUS_PATH", Path(folder) / "status.json"):
            state = core.RuntimeState(); state.last_error = f"failed {URL}"; state.write()
            self.assertNotIn(SECRET, core.STATUS_PATH.read_text(encoding="utf-8"))


class DeliveryTests(Base):
    def test_deleted_webhook_fails_once_without_retry(self):
        forwarder = self.forwarder(); session = self.with_session(forwarder, [response(404)])
        with self.assertRaises(core.WebhookError) as caught:
            forwarder._post_with_retry(URL, {"content": "x"})
        self.assertEqual(session.post.call_count, 1); self.sleep.assert_not_called()
        self.assertTrue(caught.exception.permanent); self.assertEqual(caught.exception.status, 404)
        self.assertNotIn(SECRET, str(caught.exception))

    def test_server_error_is_retried(self):
        forwarder = self.forwarder(); session = self.with_session(forwarder, [response(500), response(204)])
        forwarder._post_with_retry(URL, {"content": "x"})
        self.assertEqual(session.post.call_count, 2)

    def test_rate_limit_waits_for_retry_after(self):
        forwarder = self.forwarder(); self.with_session(forwarder, [response(429, {"Retry-After": "2"}), response(204)])
        forwarder._post_with_retry(URL, {"content": "x"})
        self.sleep.assert_called_once_with(2.0)

    def test_connection_error_message_hides_url(self):
        forwarder = self.forwarder(max_retries=0)
        self.with_session(forwarder, [core.requests.ConnectionError(f"failed for {URL}")])
        with self.assertRaises(core.WebhookError) as caught:
            forwarder._post_with_retry(URL, {"content": "x"})
        self.assertNotIn(SECRET, str(caught.exception)); self.assertFalse(caught.exception.permanent)

    def test_resume_does_not_resend_delivered_parts(self):
        forwarder = self.forwarder(max_retries=0); message = incoming(); message.content = "a" * 1990 + "\n" + "b" * 100
        session = self.with_session(forwarder, [response(204), response(500), response(204)])
        item = core.QueueItem(message)
        with self.assertRaises(core.WebhookError): forwarder._deliver(item, URL)
        self.assertEqual(item.next_part, 1)
        forwarder._deliver(item, URL)
        contents = [call.kwargs["json"]["content"] for call in session.post.call_args_list]
        self.assertEqual(contents, ["a" * 1990, "b" * 100, "b" * 100])

    def test_attachment_uploaded_and_link_kept_out_of_text(self):
        forwarder = self.forwarder(); message = incoming(); message.attachments = [FakeObject(url="https://cdn.discordapp.com/a/b.png", size=3, filename="b.png", content_type="image/png")]
        session = self.with_session(forwarder, [response(204)]); session.get.return_value = MagicMock(content=b"png")
        forwarder._deliver(core.QueueItem(message), URL)
        sent = session.post.call_args.kwargs
        self.assertEqual(sent["files"], [("files[0]", ("b.png", b"png", "image/png"))])
        self.assertEqual(json.loads(sent["data"]["payload_json"])["content"], "hello")

    def test_oversized_attachment_stays_a_link(self):
        forwarder = self.forwarder(); message = incoming(); url = "https://cdn.discordapp.com/a/big.zip"
        message.attachments = [FakeObject(url=url, size=core.MAX_UPLOAD_BYTES + 1, filename="big.zip", content_type="application/zip")]
        session = self.with_session(forwarder, [response(204)])
        forwarder._deliver(core.QueueItem(message), URL)
        session.get.assert_not_called()
        self.assertIn(url, session.post.call_args.kwargs["json"]["content"])

    def test_upload_rejected_as_too_large_falls_back_to_link(self):
        forwarder = self.forwarder(); message = incoming(); url = "https://cdn.discordapp.com/a/b.png"
        message.attachments = [FakeObject(url=url, size=3, filename="b.png", content_type="image/png")]
        session = self.with_session(forwarder, [response(413), response(204)]); session.get.return_value = MagicMock(content=b"png")
        forwarder._deliver(core.QueueItem(message), URL)
        self.assertIn(url, session.post.call_args.kwargs["json"]["content"])

    def test_username_avoids_words_discord_rejects(self):
        self.assertNotIn("discord", core.safe_username("[Discord Fans] clyde").lower())
        self.assertNotIn("clyde", core.safe_username("[Discord Fans] clyde").lower())

    def test_embeds_limited_to_request_total(self):
        embeds = [{"description": "x" * 4000} for _ in range(3)]
        self.assertEqual(len(core.limit_embeds(embeds)), 1)


class QueueTests(Base):
    def run_message(self, forwarder, message):
        async def scenario():
            await forwarder._on_message(message)
            depth = sum(queue.qsize() for queue in forwarder.queues.values())
            for task in forwarder.workers.values(): task.cancel()
            return depth
        return asyncio.run(scenario())

    def test_own_destination_webhook_is_not_forwarded(self):
        forwarder = self.forwarder(destination_webhooks=frozenset({555}), automated_channels=frozenset({100}))
        self.assertEqual(self.run_message(forwarder, incoming(webhook=555)), 0)

    def test_other_webhook_follows_allow_automated(self):
        allowed = self.forwarder(automated_channels=frozenset({100}))
        self.assertEqual(self.run_message(allowed, incoming(webhook=321)), 1)
        self.assertEqual(self.run_message(self.forwarder(), incoming(webhook=321)), 0)

    def test_failing_webhook_does_not_block_another(self):
        other = "https://discord.com/api/webhooks/666/other"
        forwarder = self.forwarder(rules={100: URL, 200: other}, max_retries=0)
        delivered = []

        def deliver(item, url):
            delivered.append(url)
            if url == URL:
                raise core.WebhookError("HTTP 404", 404, permanent=True)

        async def scenario():
            with patch.object(forwarder, "_deliver", side_effect=deliver):
                await forwarder._on_message(incoming(1, channel_id=100))
                await forwarder._on_message(incoming(2, channel_id=200))
                await asyncio.gather(*(queue.join() for queue in forwarder.queues.values()))
            for task in forwarder.workers.values(): task.cancel()

        asyncio.run(scenario())
        self.assertCountEqual(delivered, [URL, other])
        self.assertEqual(forwarder.state.forwarded, 1); self.assertEqual(forwarder.state.failed, 1)
        self.assertEqual(forwarder.state.webhooks["555"]["status"], 404)
        self.assertTrue(forwarder.state.webhooks["666"]["ok"])

    def test_temporary_failure_retries_in_later_round(self):
        forwarder = self.forwarder(); attempts = []

        def deliver(item, url):
            attempts.append(1)
            if len(attempts) == 1:
                raise core.WebhookError("HTTP 500", 500)

        async def scenario():
            with patch.object(forwarder, "_deliver", side_effect=deliver), patch.object(core, "DELIVERY_ROUND_DELAY", 0):
                await forwarder._process(core.QueueItem(incoming()), URL)

        asyncio.run(scenario())
        self.assertEqual(len(attempts), 2); self.assertEqual(forwarder.state.forwarded, 1); self.assertEqual(forwarder.state.failed, 0)


class StatusFileTests(Base):
    def test_locked_status_file_does_not_raise(self):
        state = core.RuntimeState()
        with patch.object(core.os, "replace", side_effect=PermissionError(5, "Access is denied")) as replace:
            state.write()
        self.assertEqual(replace.call_count, 5)
        self.assertEqual(list(Path(self.folder.name).glob(".*tmp")), [])

    def test_recovers_when_lock_clears(self):
        state = core.RuntimeState(); real = core.os.replace; calls = []

        def flaky(source, target):
            calls.append(1)
            if len(calls) < 3: raise PermissionError(5, "Access is denied")
            real(source, target)

        with patch.object(core.os, "replace", side_effect=flaky): state.write()
        self.assertTrue(core.STATUS_PATH.is_file())

    def test_full_disk_does_not_raise(self):
        with patch.object(Path, "write_text", side_effect=OSError(28, "No space left on device")):
            core.RuntimeState().write()


class ConnectionTests(Base):
    def test_invalid_token_stops_only_that_account(self):
        forwarder = self.forwarder(); forwarder.client.start = MagicMock(side_effect=core.discord.LoginFailure("bad"))
        asyncio.run(forwarder.start())
        entry = forwarder.state.accounts["one"]
        self.assertFalse(entry["connected"]); self.assertIn("Token", entry["last_error"])
        self.assertEqual(forwarder.client.start.call_count, 1)

    def test_other_errors_reconnect_with_new_client(self):
        forwarder = self.forwarder(); clients = []

        class Stop(BaseException):
            pass

        first = forwarder.client
        async def fail(*_args, **_kwargs): raise OSError("network down")
        async def close(): return None
        async def stop(*_args, **_kwargs): raise Stop()
        first.start = fail; first.close = close
        second = MagicMock(); second.start = stop; clients.append(second)

        async def scenario():
            with patch.object(core.discord, "Client", side_effect=clients), patch.object(core, "RECONNECT_BASE_DELAY", 0), patch.object(core.asyncio, "sleep", return_value=None) as sleeper:
                sleeper.side_effect = lambda _delay: asyncio.ensure_future(asyncio.gather())
                forwarder._reconnect_delay = 0
                await forwarder.start()

        with self.assertRaises(Stop):
            asyncio.run(scenario())
        self.assertIs(forwarder.client, second)
        self.assertIn("network down", forwarder.state.accounts["one"]["last_error"])


class LogFilterTests(unittest.TestCase):
    def record(self, text):
        return logging.LogRecord("discord", logging.WARNING, __file__, 1, text, None, None)

    def test_session_warning_is_throttled(self):
        noise = core.RepeatFilter()
        self.assertTrue(noise.filter(self.record("User has more than 15 active sessions. x")))
        self.assertFalse(noise.filter(self.record("User has more than 15 active sessions. x")))
        self.assertTrue(noise.filter(self.record("anything else")))

    def test_settings_collect_destination_webhooks(self):
        payload = {"accounts": [{"id": "a", "name": "A", "user_token": "t" * 40}, {"id": "b", "name": "B", "user_token": "u" * 40}], "forward_rules": [{"account_id": "a", "channel_id": "1", "webhook_url": "https://discord.com/api/webhooks/11/x"}, {"account_id": "b", "channel_id": "2", "webhook_url": "https://discord.com/api/webhooks/22/y"}]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"; path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(core, "CONFIG_PATH", path): loaded = core.load_all_settings()
        self.assertEqual(loaded[0].destination_webhooks, frozenset({11, 22}))


if __name__ == "__main__":
    unittest.main()
