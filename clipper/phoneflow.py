"""Stand-in for a host app's Telegram flow. On its own, the agent sends clips through its own bot (gta6/tg.py)."""


def send_clip(meta: dict, approved: bool = False) -> bool:
    return False
