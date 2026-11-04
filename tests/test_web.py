import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import config_web as web


class WebTests(unittest.TestCase):
    def setUp(self):
        web.app.config["TESTING"] = True
        self.client = web.app.test_client()

    def test_status_is_local_read_only(self):
        response = self.client.get("/api/status", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

    def test_index_and_diagnostics_render(self):
        index = self.client.get("/", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        diagnostics = self.client.get("/api/diagnostics", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(index.status_code, 200)
        self.assertIn("Discord 转发管理后台", index.get_data(as_text=True))
        self.assertEqual(diagnostics.status_code, 200)
        self.assertIn("python", diagnostics.get_json()["diagnostics"])

    def test_remote_address_is_denied(self):
        response = self.client.get("/api/status", environ_base={"REMOTE_ADDR": "10.0.0.5"})
        self.assertEqual(response.status_code, 403)

    def test_mutation_requires_admin_token(self):
        response = self.client.post("/api/process/stop", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(response.status_code, 403)

    def test_public_config_hides_token(self):
        config = web.public_config()
        self.assertNotIn("user_token", config)
        self.assertIn("token_configured", config)
        self.assertTrue(all("user_token" not in account for account in config["accounts"]))

    def test_public_config_hides_webhook_secret(self):
        payload = {"user_token": "x" * 40, "forward_rules": [{"channel_id": "123", "webhook_url": "https://discord.com/api/webhooks/1/very-secret"}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"; path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(web, "CONFIG_PATH", path): config = web.public_config()
        self.assertNotIn("webhook_url", config["forward_rules"][0])
        self.assertTrue(config["forward_rules"][0]["webhook_configured"])

    def test_blank_webhook_preserves_existing_value(self):
        url = "https://discord.com/api/webhooks/1/very-secret"
        payload = {"user_token": "x" * 40, "forward_rules": [{"channel_id": "123", "webhook_url": url, "allow_automated": True}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"; path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(web, "CONFIG_PATH", path):
                result = web.normalize_config({"user_token": "", "forward_rules": [{"channel_id": "123", "webhook_url": "", "allow_automated": True}]})
        self.assertEqual(result["forward_rules"][0]["webhook_url"], url)
        self.assertTrue(result["forward_rules"][0]["allow_automated"])

    def test_multiple_accounts_preserve_tokens_and_allow_same_channel(self):
        payload = {"accounts": [{"id": "one", "name": "一号", "user_token": "a" * 40}, {"id": "two", "name": "二号", "user_token": "b" * 40}], "forward_rules": [{"account_id": "one", "channel_id": "123", "webhook_url": "https://discord.com/api/webhooks/1/key"}, {"account_id": "two", "channel_id": "123", "webhook_url": "https://discord.com/api/webhooks/2/key"}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"; path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            request = {"accounts": [{"id": "one", "name": "一号", "user_token": ""}, {"id": "two", "name": "二号", "user_token": ""}], "forward_rules": [{"account_id": "one", "channel_id": "123", "webhook_url": ""}, {"account_id": "two", "channel_id": "123", "webhook_url": ""}]}
            with patch.object(web, "CONFIG_PATH", path): result = web.normalize_config(request)
        self.assertEqual([item["user_token"] for item in result["accounts"]], ["a" * 40, "b" * 40])
        self.assertEqual(len(result["forward_rules"]), 2)

    def test_browser_log_redacts_credentials(self):
        web._logs.clear()
        web.append_log("https://discord.com/api/webhooks/123/secret-value")
        self.assertNotIn("secret-value", web._logs[-1]["text"])

    def test_restart_breaker_detects_three_fast_failures(self):
        web._fast_failures.clear()
        web._fast_failures.extend([10.0, 20.0, 30.0])
        self.assertEqual(len(web._fast_failures), 3)

    def test_manual_action_resets_restart_breaker(self):
        web._fast_failures.extend([10.0, 20.0, 30.0]); web._restart_blocked = True
        web.reset_restart_breaker()
        self.assertFalse(web._restart_blocked)
        self.assertEqual(len(web._fast_failures), 0)


if __name__ == "__main__":
    unittest.main()
