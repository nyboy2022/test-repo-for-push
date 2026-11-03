import unittest

from config_web import is_valid_webhook_url, normalize_payload


class ConfigValidationTests(unittest.TestCase):
    def test_accepts_discord_webhook(self):
        self.assertTrue(
            is_valid_webhook_url("https://discord.com/api/webhooks/123/token")
        )

    def test_rejects_non_discord_webhook(self):
        self.assertFalse(
            is_valid_webhook_url("https://example.com/api/webhooks/123/token")
        )

    def test_normalizes_rules(self):
        payload = normalize_payload(
            {
                "user_token": " token ",
                "forward_rules": [
                    {
                        "channel_id": " 123 ",
                        "webhook_url": " https://discord.com/api/webhooks/1/a ",
                    }
                ],
                "debug": False,
            }
        )
        self.assertEqual(payload["user_token"], "token")
        self.assertEqual(payload["forward_rules"][0]["channel_id"], "123")
        self.assertFalse(payload["debug"])


if __name__ == "__main__":
    unittest.main()
