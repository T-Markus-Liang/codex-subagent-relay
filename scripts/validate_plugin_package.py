#!/usr/bin/env python3
"""Validate the tracked Codex plugin package without external dependencies."""

from __future__ import annotations

import json
import stat
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "codex-subagent-relay"
MARKETPLACE = ROOT / ".agents" / "plugins" / "marketplace.json"


def fail(message: str) -> None:
    raise SystemExit(f"plugin package validation failed: {message}")


def load_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read valid JSON from {path.relative_to(ROOT)}: {exc}")
    if not isinstance(value, dict):
        fail(f"{path.relative_to(ROOT)} must contain an object")
    return value


def nonempty_string(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        fail(f"{label} must be a non-empty string")


def main() -> int:
    manifest = load_object(PLUGIN / ".codex-plugin" / "plugin.json")
    if manifest.get("name") != "codex-subagent-relay":
        fail("plugin name does not match the package directory")
    nonempty_string(manifest.get("version"), "plugin version")
    nonempty_string(manifest.get("description"), "plugin description")
    if manifest.get("skills") != "./skills/" or manifest.get("mcpServers") != "./.mcp.json":
        fail("plugin must declare its tracked skills and MCP manifest")
    interface = manifest.get("interface")
    if not isinstance(interface, dict):
        fail("plugin interface must be an object")
    for key in ("displayName", "shortDescription", "longDescription", "developerName", "category", "defaultPrompt"):
        nonempty_string(interface.get(key), f"interface.{key}")
    if not isinstance(interface.get("capabilities"), list) or not all(isinstance(item, str) for item in interface["capabilities"]):
        fail("interface.capabilities must be an array of strings")
    mcp = load_object(PLUGIN / ".mcp.json")
    server = mcp.get("mcpServers", {}).get("codex-subagent-relay") if isinstance(mcp.get("mcpServers"), dict) else None
    if not isinstance(server, dict) or server.get("command") != "./mcp/relay-mcp" or server.get("cwd") != ".":
        fail("MCP manifest does not point to the bundled launcher")
    launcher = PLUGIN / "mcp" / "relay-mcp"
    if not launcher.is_file() or not (launcher.stat().st_mode & stat.S_IXUSR):
        fail("MCP launcher is missing or not executable")
    marketplace = load_object(MARKETPLACE)
    entries = marketplace.get("plugins")
    if not isinstance(entries, list) or len(entries) != 1:
        fail("marketplace must contain exactly one plugin entry")
    entry = entries[0]
    if not isinstance(entry, dict) or entry.get("name") != manifest["name"]:
        fail("marketplace plugin name does not match manifest")
    if entry.get("source") != {"source": "local", "path": "./plugins/codex-subagent-relay"}:
        fail("marketplace source must point to the repository plugin")
    if entry.get("policy") != {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}:
        fail("marketplace policy is incomplete")
    if entry.get("category") != interface["category"]:
        fail("marketplace category does not match plugin interface")
    print(json.dumps({"status": "success", "plugin": manifest["name"], "marketplace": marketplace["name"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
