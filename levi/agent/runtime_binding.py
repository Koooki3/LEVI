"""Non-secret runtime binding; token refresh alone does not change an account."""

import json
import os
from pathlib import Path

from .store import digest, file_hash


def binding(runtime):
    if not runtime:
        return None
    from levi.paths import PROJECT

    if runtime == "codex":
        home = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex")))
        auth = home / "auth.json"
        config = home / "config.toml"
        try:
            data = json.loads(auth.read_text())
            identity = {
                "mode": data.get("auth_mode"),
                "account": (data.get("tokens") or {}).get("account_id"),
            }
        except (OSError, ValueError):
            identity = {"mode": "unknown"}
    else:
        home = Path(os.getenv("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
        config = home / "settings.json"
        try:
            data = json.loads((home.parent / (home.name + ".json")).read_text())
            identity = {"account": (data.get("oauthAccount") or {}).get("accountUuid")}
        except (OSError, ValueError):
            identity = {"mode": "unknown"}
    keys = (
        ["OPENAI_API_KEY", "CODEX_API_KEY"]
        if runtime == "codex"
        else ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"]
    )
    identity["environment"] = digest({k: os.getenv(k) for k in keys})
    lock = PROJECT / "integrations/pilot/bun.lock"
    return {
        "runtime": runtime,
        "identity_digest": digest(identity),
        "config_digest": file_hash(config) if config.is_file() else None,
        "adapter_lock": file_hash(lock) if lock.is_file() else None,
    }
