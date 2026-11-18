"""Every web request the agent makes goes through here (one place to test, one place to set timeouts)."""
from __future__ import annotations

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DamonDynamics-GTA6ClipAgent/2.0"


def req(method: str, url: str, timeout: float = 60, **kw) -> requests.Response:
    kw.setdefault("headers", {})
    kw["headers"].setdefault("User-Agent", UA)
    return requests.request(method, url, timeout=timeout, **kw)


def body(r) -> dict:
    """The JSON a service answered with, or {} when it answered with something else."""
    try:
        j = r.json()
        return j if isinstance(j, dict) else {"data": j}
    except ValueError:
        return {}
