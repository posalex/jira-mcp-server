#!/usr/bin/env python3
"""
Native Messaging host for the Jira Cookie Bridge Firefox extension.

Firefox spawns this script, sends a JSON message on stdin (length-prefixed),
and reads a JSON response from stdout (length-prefixed).

The script merges incoming cookies into the shared cookie file that the
Jira MCP server reads at runtime.

Configuration is read from .env.local next to this script's parent directory,
or falls back to sensible defaults.
"""

import json
import os
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
ENV_FILE = PROJECT_DIR / ".env.local"


def load_env() -> dict:
    """Parse .env.local into a dict (simple KEY=VALUE, no shell expansion)."""
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                # Strip surrounding quotes if present
                value = value.strip().strip("'\"")
                # Expand $HOME / $(HOME) / ${HOME}
                value = value.replace("$(HOME)", str(Path.home()))
                value = value.replace("${HOME}", str(Path.home()))
                value = value.replace("$HOME", str(Path.home()))
                env[key.strip()] = value
    return env


CONFIG = load_env()
COOKIE_FILE = Path(
    os.environ.get(
        "COOKIE_FILE",
        CONFIG.get("COOKIE_FILE", str(Path.home() / ".jira-mcp-cookies.json")),
    )
)

# ---------------------------------------------------------------------------
# Native Messaging I/O helpers (Firefox length-prefixed JSON protocol)
# ---------------------------------------------------------------------------


def read_message() -> dict:
    """Read a single native-messaging message from stdin."""
    raw_length = sys.stdin.buffer.read(4)
    if len(raw_length) < 4:
        sys.exit(0)
    length = struct.unpack("=I", raw_length)[0]
    payload = sys.stdin.buffer.read(length)
    return json.loads(payload.decode("utf-8"))


def send_message(obj: dict):
    """Write a single native-messaging message to stdout."""
    encoded = json.dumps(obj).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("=I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


# ---------------------------------------------------------------------------
# Cookie persistence (same format as server.py)
# ---------------------------------------------------------------------------


def load_cookies() -> dict:
    if COOKIE_FILE.exists():
        try:
            return json.loads(COOKIE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_cookies(cookies: dict):
    COOKIE_FILE.write_text(json.dumps(cookies, indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    try:
        msg = read_message()
        action = msg.get("action", "update")

        if action == "update":
            cookies = load_cookies()
            incoming = msg.get("cookies", {})
            expiry = msg.get("expiry", {})
            # Merge: only overwrite keys that have non-empty values
            for key, value in incoming.items():
                if value:
                    cookies[key] = value
            # Add metadata
            cookies["_updated_at"] = datetime.now(timezone.utc).isoformat()
            if expiry:
                cookies["_expiry"] = {
                    k: datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
                    for k, v in expiry.items()
                }
            save_cookies(cookies)
            send_message({"ok": True, "saved": list(incoming.keys())})

        elif action == "status":
            cookies = load_cookies()
            send_message({"ok": True, "keys": list(cookies.keys())})

        else:
            send_message({"ok": False, "error": f"Unknown action: {action}"})

    except Exception as exc:
        # Attempt to report the error back to the extension
        try:
            send_message({"ok": False, "error": str(exc)})
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
