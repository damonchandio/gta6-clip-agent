"""Your posting accounts. Each slot is one persona that can be logged in to Instagram, TikTok and YouTube.
Change ACCOUNTS in .env to have more or fewer than 5. You make and own the accounts; the agent only posts."""
import json
import os
import re

from . import HOME

FILE = HOME / "accounts.json"
COUNT = max(1, min(9, int(os.getenv("ACCOUNTS", "5") or 5)))


def load() -> list[dict]:
    try:
        accts = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        accts = []
    have = {a["slot"] for a in accts}
    accts += [{"slot": s, "handle": ""} for s in range(1, COUNT + 1) if s not in have]
    return sorted(accts, key=lambda a: a["slot"])[:COUNT]


def set_handle(slot: int, handle: str) -> dict:
    accts = load()
    handle = "@" + handle.strip().lstrip("@").lower()
    if not re.fullmatch(r"@[a-z0-9._]{1,30}", handle):
        raise ValueError("Handles are letters, numbers, dots and underscores, 30 at most.")
    for a in accts:
        if a["slot"] == slot:
            a["handle"] = handle
            FILE.write_text(json.dumps(accts, indent=1), encoding="utf-8")
            return a
    raise ValueError(f"There's no account {slot} (1 to {len(accts)}).")


def for_campaign(rules: dict) -> dict | None:
    return None


def label(a: dict | None) -> str:
    if not a:
        return "any account"
    return a.get("handle") or f"account {a['slot']}"
