import json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import discord_forwarder as core


class FakeObject:
    def __init__(self, **values): self.__dict__.update(values)

class CoreTests(unittest.TestCase):
    def test_webhook_validation(self):
        self.assertTrue(core.is_valid_webhook_url("https://discord.com/api/webhooks/123/secret")); self.assertFalse(core.is_valid_webhook_url("http://discord.com/api/webhooks/123/secret")); self.assertFalse(core.is_valid_webhook_url("https://evil.test/api/webhooks/123/secret"))
    def test_split_content_preserves_text(self):
        original = "a" * 1995 + "\n" + "b" * 40; parts = core.split_content(original); self.assertEqual("\n".join(parts), original); self.assertTrue(all(len(part) <= 2000 for part in parts))
    def test_forwarded_snapshot_content_and_attachments(self):
        raw = {"content": "", "attachments": [], "message_snapshots": [{"message": {"content": "forwarded", "attachments": [{"url": "https://cdn.example/image.png"}]}}]}
        self.assertEqual(core.message_content(raw), "forwarded\nhttps://cdn.example/image.png")
        snapshot = FakeObject(content="forwarded", attachments=[FakeObject(url="https://cdn.example/image.png")])
        message = FakeObject(content="", attachments=[], message_snapshots=[snapshot])
        self.assertEqual(core.message_content(message), "forwarded\nhttps://cdn.example/image.png")
        message.embeds = []; snapshot.embeds = [core.discord.Embed(title="snapshot card")]
        self.assertEqual(core.build_embeds(message)[0]["title"], "snapshot card")
    def test_load_settings_bounds(self):
        payload = {"user_token": "x" * 40, "forward_rules": [{"channel_id": "123", "webhook_url": "https://discord.com/api/webhooks/1/key"}], "options": {"max_retries": 99, "queue_size": 1}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"; path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(core, "CONFIG_PATH", path): settings = core.load_settings()
        self.assertEqual(settings.account_id, "default"); self.assertEqual(settings.max_retries, 10); self.assertEqual(settings.queue_size, 10); self.assertIn(123, settings.rules); self.assertEqual(settings.automated_channels, frozenset())

    def test_load_all_settings_routes_rules_by_account(self):
        payload = {"accounts": [{"id": "one", "name": "一号", "user_token": "a" * 40}, {"id": "two", "name": "二号", "user_token": "b" * 40}], "forward_rules": [{"account_id": "one", "channel_id": "123", "webhook_url": "https://discord.com/api/webhooks/1/key"}, {"account_id": "two", "channel_id": "123", "webhook_url": "https://discord.com/api/webhooks/2/key"}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"; path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with patch.object(core, "CONFIG_PATH", path): settings = core.load_all_settings()
        self.assertEqual([item.account_id for item in settings], ["one", "two"])
        self.assertEqual([item.account_name for item in settings], ["一号", "二号"])
        self.assertTrue(all(123 in item.rules for item in settings))

    def test_load_settings_allows_automated_per_rule(self):
        payload = {"user_token": "x" * 40, "forward_rules": [{"channel_id": "123", "webhook_url": "https://discord.com/api/webhooks/1/key", "allow_automated": True}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"; path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(core, "CONFIG_PATH", path): settings = core.load_settings()
        self.assertEqual(settings.automated_channels, frozenset({123}))
    def test_ca_bundle_path_is_ascii_on_windows(self):
        if core.os.name == "nt":
            self.assertTrue(str(core.certifi.where()).isascii())
            self.assertTrue(Path(core.certifi.where()).is_file())

    def test_runtime_state_write_excludes_internal_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_status.json"
            with patch.object(core, "STATUS_PATH", path): core.RuntimeState().write()
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("_write_lock", payload)
        self.assertIn("updated_at", payload)

    def test_source_ids_include_thread_parent_and_category(self):
        category = FakeObject(id=30)
        parent = FakeObject(id=20, category_id=30, category=category)
        channel = FakeObject(id=10, parent_id=20, category_id=None, parent=parent, category=None)
        message = FakeObject(channel=channel)
        self.assertEqual(core.Forwarder._source_ids(message), [10, 20, 30])
if __name__ == "__main__": unittest.main()
