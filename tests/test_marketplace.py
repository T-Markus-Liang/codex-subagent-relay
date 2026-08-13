import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MARKETPLACE = ROOT / ".agents" / "plugins" / "marketplace.json"
PLUGIN = ROOT / "plugins" / "codex-subagent-relay"


class MarketplaceTests(unittest.TestCase):
    def test_repository_marketplace_points_to_valid_plugin(self):
        payload = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
        self.assertEqual(payload["name"], "codex-subagent-relay")
        self.assertEqual(len(payload["plugins"]), 1)
        entry = payload["plugins"][0]
        self.assertEqual(entry["name"], "codex-subagent-relay")
        self.assertEqual(entry["source"], {"source": "local", "path": "./plugins/codex-subagent-relay"})
        self.assertEqual(entry["policy"], {"installation": "AVAILABLE", "authentication": "ON_INSTALL"})
        self.assertEqual(entry["category"], "Productivity")
        self.assertTrue((ROOT / entry["source"]["path"][2:]).is_dir())
        self.assertTrue((PLUGIN / ".codex-plugin" / "plugin.json").is_file())
        self.assertTrue((PLUGIN / ".mcp.json").is_file())
        self.assertTrue((PLUGIN / "mcp" / "relay-mcp").stat().st_mode & 0o111)


if __name__ == "__main__":
    unittest.main()
