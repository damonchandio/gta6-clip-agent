"""Stand-in for a host app's shared Telegram approvals. On its own, the agent uses its own bot (gta6/tg.py)."""


def ready() -> bool:
    return False


def say(text: str) -> None:
    pass


def ask(*a, **kw):
    return None
