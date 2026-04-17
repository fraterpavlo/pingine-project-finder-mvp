#!/usr/bin/env python3
"""
Secrets loader for ProjectFinder configs.

Secrets live in `config/secrets.json` (git-ignored). Non-sensitive config lives
in `config/*-config.json` (committed). At load time, secrets are deep-merged
over the base config — so scripts don't need to know which fields are secret.

Lookup order for any given secrets value:
  1. environment variable (if key is listed in ENV_OVERRIDES)
  2. config/secrets.json
  3. the base config file itself (fallback / dev default)

Usage:
    from pf_secrets import load_config
    cfg = load_config("email-config.json")
    password = cfg["email"]["smtp"]["password"]

File formats — secrets.json mirrors the shape of the real config:

    {
      "email-config.json": {
        "email": {
          "smtp": {"password": "app-pw-here"},
          "imap": {"password": "app-pw-here"}
        }
      },
      "notifications-config.json": {
        "telegram": {"bot_token": "1234:abcd"}
      },
      "telegram-client-config.json": {
        "telegram_client": {"api_id": 12345, "api_hash": "deadbeef"}
      }
    }
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = SCRIPT_DIR.parent / "config"
SECRETS_PATH = CONFIG_DIR / "secrets.json"


# Environment variable overrides for a few common secrets. Lets the user set
# them without touching secrets.json — useful for containerised runs.
ENV_OVERRIDES = {
    # path-in-cfg -> env var name
    ("email-config.json", "email", "smtp", "password"):  "PF_EMAIL_APP_PASSWORD",
    ("email-config.json", "email", "imap", "password"):  "PF_EMAIL_APP_PASSWORD",
    ("notifications-config.json", "telegram", "bot_token"): "PF_TELEGRAM_BOT_TOKEN",
    ("telegram-client-config.json", "telegram_client", "api_id"):   "PF_TELEGRAM_API_ID",
    ("telegram-client-config.json", "telegram_client", "api_hash"): "PF_TELEGRAM_API_HASH",
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge `overlay` into `base`. Returns a new dict."""
    out = dict(base)
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_config(config_filename: str) -> dict:
    """Read a config file and overlay secrets + env vars. Never mutates disk."""
    base = _load_json(CONFIG_DIR / config_filename)
    all_secrets = _load_json(SECRETS_PATH)
    overlay = all_secrets.get(config_filename, {}) if isinstance(all_secrets, dict) else {}
    merged = _deep_merge(base, overlay)

    # Apply env-var overrides (highest priority).
    for path, env_name in ENV_OVERRIDES.items():
        if path[0] != config_filename:
            continue
        env_val = os.environ.get(env_name)
        if env_val is None:
            continue
        # Cast to int for api_id.
        if path[-1] == "api_id":
            try:
                env_val = int(env_val)
            except ValueError:
                pass
        _set_nested(merged, path[1:], env_val)

    return merged


def _set_nested(d: dict, path: tuple, value: Any) -> None:
    cur = d
    for k in path[:-1]:
        if not isinstance(cur.get(k), dict):
            cur[k] = {}
        cur = cur[k]
    cur[path[-1]] = value


def secrets_configured() -> bool:
    """True if secrets.json exists — used for early-exit checks in scripts."""
    return SECRETS_PATH.exists()


if __name__ == "__main__":
    # Quick inspection tool: print merged configs with secrets redacted.
    import sys as _sys
    redact = {"password", "bot_token", "api_hash", "api_id"}

    def _redact(obj):
        if isinstance(obj, dict):
            return {k: ("***" if k in redact and v else _redact(v)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_redact(x) for x in obj]
        return obj

    for cfg_file in ("email-config.json", "notifications-config.json", "telegram-client-config.json"):
        print(f"--- {cfg_file} ---")
        try:
            print(json.dumps(_redact(load_config(cfg_file)), indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"(error: {e})")
        print()
