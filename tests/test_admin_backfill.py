"""Regression tests for admin console safety and the backfill scripts. Nothing here touches the network."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import backfill_recent as backfill
import config_web as web

SECRET = "SuperSecretWebhookValue-123"
URL = f"https://discord.com/api/webhooks/555/{SECRET}"
LOCAL = {"REMOTE_ADDR": "127.0.0.1"}


class AdminBase(unittest.TestCase):
    def setUp(self):
        web.app.config["TESTING"] = True
        self.client = web.app.test_client()
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        for name, path in {"CONFIG_PATH": root / "config.json", "STATUS_PATH": root / "status.json", "AUDIT_PATH": root / "audit.log", "ADMIN_STATE_PATH": root / "state.json"}.items():
            patcher = patch.object(web, name, path); patcher.start(); self.addCleanup(patcher.stop)

    def write_config(self, **changes):
        payload = {"accounts": [{"id": "one", "name": "一号", "user_token": "a" * 40}, {"id": "two", "name": "二号", "user_token": "b" * 40}], "forward_rules": [{"account_id": "one", "channel_id": "123", "webhook_url": URL, "allow_automated": True}], "options": {}}
        payload.update(changes)
        web.CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class GuardTests(AdminBase):
    def test_foreign_host_is_rejected(self):
        response = self.client.get("/", environ_base=LOCAL, headers={"Host": "evil.example:5151"})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(web.ADMIN_TOKEN, response.get_data(as_text=True))

    def test_local_hosts_are_allowed(self):
        for host in ("127.0.0.1:5151", "localhost:5151", "127.0.0.1"):
            self.assertEqual(self.client.get("/api/status", environ_base=LOCAL, headers={"Host": host}).status_code, 200, host)

    def test_security_headers_present(self):
        headers = self.client.get("/", environ_base=LOCAL).headers
        self.assertEqual(headers["X-Frame-Options"], "DENY"); self.assertIn("frame-ancestors", headers["Content-Security-Policy"])


class StatusTests(AdminBase):
    def test_status_redacts_old_status_file(self):
        web.STATUS_PATH.write_text(json.dumps({"updated_at": 0, "last_error": f"404 for {URL}", "accounts": {"one": {"last_error": URL}}}), encoding="utf-8")
        body = self.client.get("/api/status", environ_base=LOCAL).get_data(as_text=True)
        self.assertNotIn(SECRET, body)

    def test_public_config_exposes_id_not_secret(self):
        self.write_config()
        rule = web.public_config()["forward_rules"][0]
        self.assertEqual(rule["webhook_id"], "555"); self.assertNotIn(SECRET, json.dumps(rule))

    def test_restart_required_only_after_config_changes(self):
        self.write_config(); process = MagicMock(); process.poll.return_value = None
        mtime = web.CONFIG_PATH.stat().st_mtime
        with patch.object(web, "_proc", process):
            with patch.object(web, "_proc_started_at", mtime + 10): self.assertFalse(web.config_changed_since_start())
            with patch.object(web, "_proc_started_at", mtime - 10): self.assertTrue(web.config_changed_since_start())


class ConfigTests(AdminBase):
    def request(self, **rule):
        return {"accounts": [{"id": "one", "name": "一号", "user_token": ""}, {"id": "two", "name": "二号", "user_token": ""}], "forward_rules": [{"account_id": "one", "channel_id": "123", "webhook_url": "", **rule}], "options": {"auto_start": False}}

    def test_note_is_kept(self):
        self.write_config()
        self.assertEqual(web.normalize_config(self.request(note="  行情  "))["forward_rules"][0]["note"], "行情")

    def test_moving_rule_to_other_account_keeps_webhook(self):
        self.write_config()
        result = web.normalize_config(self.request(account_id="two"))
        self.assertEqual(result["forward_rules"][0]["webhook_url"], URL)
        self.assertEqual(result["forward_rules"][0]["account_id"], "two")

    def test_auto_start_option_round_trips(self):
        self.write_config()
        self.assertFalse(web.normalize_config(self.request())["options"]["auto_start"])


class AutostartTests(AdminBase):
    def test_starts_by_default(self):
        self.write_config()
        with patch.object(web, "start_process") as start: web.autostart()
        start.assert_called_once()

    def test_manual_stop_is_remembered(self):
        self.write_config(); web.stop_process()
        with patch.object(web, "start_process") as start: web.autostart()
        start.assert_not_called()

    def test_console_exit_does_not_count_as_manual_stop(self):
        self.write_config(); web.remember_desired_state(True); web.stop_process(remember=False)
        self.assertTrue(web.desired_running())

    def test_option_disables_autostart(self):
        self.write_config(options={"auto_start": False})
        with patch.object(web, "start_process") as start: web.autostart()
        start.assert_not_called()


class WebhookCheckTests(AdminBase):
    def test_check_requires_admin_token(self):
        self.assertEqual(self.client.post("/api/webhooks/check", environ_base=LOCAL).status_code, 403)

    def test_results_never_include_webhook_token(self):
        self.write_config()
        reply = MagicMock(status_code=200); reply.json.return_value = {"name": "归档", "channel_id": "123", "guild_id": "9", "token": SECRET}
        with patch("requests.get", return_value=reply) as getter:
            body = self.client.post("/api/webhooks/check", environ_base=LOCAL, headers={"X-Admin-Token": web.ADMIN_TOKEN}).get_json()
        getter.assert_called_once()
        self.assertNotIn(SECRET, json.dumps(body))
        self.assertEqual(body["results"]["555"]["name"], "归档")
        self.assertTrue(body["results"]["555"]["loop_risk"])

    def test_deleted_webhook_reported(self):
        self.write_config()
        with patch("requests.get", return_value=MagicMock(status_code=404)):
            result = web.check_webhooks()["555"]
        self.assertFalse(result["ok"]); self.assertEqual(result["status"], 404)


def page_message(message_id, **changes):
    message = {"id": str(message_id), "timestamp": datetime.now(timezone.utc).isoformat(), "author": {"id": "7", "username": "user"}, "content": "x"}
    message.update(changes)
    return message


class BackfillTests(unittest.TestCase):
    def setUp(self):
        sleeper = patch.object(backfill.time, "sleep"); self.sleep = sleeper.start(); self.addCleanup(sleeper.stop)

    def reply(self, status, body=None):
        item = MagicMock(status_code=status); item.json.return_value = body if body is not None else {}
        return item

    def test_rate_limit_is_waited_out(self):
        session = MagicMock(); session.get.side_effect = [self.reply(429, {"retry_after": 2}), self.reply(200, [page_message(1)])]
        messages = backfill.fetch_recent(session, "1", datetime.now(timezone.utc) - timedelta(days=1))
        self.assertEqual(len(messages), 1); self.sleep.assert_called_once_with(2.0)

    def test_forbidden_is_reported_as_unavailable(self):
        session = MagicMock(); session.get.return_value = self.reply(403)
        with self.assertRaises(backfill.ChannelUnavailable):
            backfill.fetch_recent(session, "1", datetime.now(timezone.utc))

    def test_server_error_is_not_mistaken_for_unavailable(self):
        session = MagicMock(); session.get.return_value = self.reply(500)
        with self.assertRaises(RuntimeError) as caught:
            backfill.fetch_recent(session, "1", datetime.now(timezone.utc))
        self.assertNotIsInstance(caught.exception, backfill.ChannelUnavailable)

    def test_filters_match_live_forwarder(self):
        rule = {"allow_automated": False}; options = {"ignore_bots": True}
        check = lambda message, rule=rule: backfill.should_forward(message, rule, options, "99", {"555"})
        self.assertTrue(check(page_message(1)))
        self.assertFalse(check(page_message(1, author={"id": "99"})))
        self.assertFalse(check(page_message(1, webhook_id="321")))
        self.assertFalse(check(page_message(1, author={"id": "7", "bot": True})))
        self.assertTrue(check(page_message(1, webhook_id="321"), {"allow_automated": True}))
        self.assertFalse(check(page_message(1, webhook_id="555"), {"allow_automated": True}))

    def test_sent_log_survives_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sent.json"; log = backfill.SentLog(path); log.add("555", "1")
            again = backfill.SentLog(path)
            self.assertTrue(again.has("555", "1")); self.assertFalse(again.has("666", "1"))

    def test_ambiguous_rule_requires_account(self):
        config = {"forward_rules": [{"account_id": "a", "channel_id": "1"}, {"account_id": "b", "channel_id": "1"}]}
        with self.assertRaises(RuntimeError): backfill.find_rule(config, "1", None)
        self.assertEqual(backfill.find_rule(config, "1", "b")["account_id"], "b")

    def test_connection_error_hides_url(self):
        session = MagicMock(); session.post.side_effect = backfill.requests.ConnectionError(f"failed {URL}")
        with self.assertRaises(RuntimeError) as caught:
            backfill.post_with_retry(session, URL, {}, retries=0)
        self.assertNotIn(SECRET, str(caught.exception))

    def test_usernames_are_safe(self):
        self.assertNotIn("discord", backfill.payloads_for(page_message(1, author={"id": "7", "username": "discord fan"}))[0]["username"].lower())


if __name__ == "__main__":
    unittest.main()
