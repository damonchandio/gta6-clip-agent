"""Posting Reels through Instagram's official API (Instagram API with Instagram Login: free, no password
stored). An account is connected by logging in from the Accounts page, or by putting a token from the Meta
app dashboard in .env as IG_TOKEN_<slot>. Tokens last 60 days and are refreshed weekly."""
from __future__ import annotations

import json
import logging
import os
import time

import requests

from . import HOME, OUT, storage

log = logging.getLogger("clipper.instagram")
API = "https://graph.instagram.com/" + os.getenv("IG_API_VERSION", "v25.0")
TOKENS = HOME / "ig_tokens.json"


class PostError(RuntimeError):
    pass


def _saved() -> dict:
    try:
        return json.loads(TOKENS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(saved: dict) -> None:
    TOKENS.write_text(json.dumps(saved, indent=1), encoding="utf-8")


def token(slot: int) -> str:
    saved = _saved().get(str(slot)) or {}
    env = (os.getenv(f"IG_TOKEN_{slot}") or "").strip()
    if env and env != saved.get("from_env"):      # a token newly pasted into .env wins over an older refreshed copy
        return env
    return saved.get("token") or env


def enabled(slot: int) -> bool:
    return bool(token(slot))


def _get(path: str, slot: int, **params) -> dict:
    r = requests.get(f"{API}/{path}", params=dict(params, access_token=token(slot)), timeout=30)
    data = r.json() if r.text.strip().startswith("{") else {}
    if r.status_code != 200:
        raise PostError((data.get("error") or {}).get("message") or f"Instagram said {r.status_code}")
    return data


def _post(path: str, slot: int, **params) -> dict:
    r = requests.post(f"{API}/{path}", data=dict(params, access_token=token(slot)), timeout=60)
    data = r.json() if r.text.strip().startswith("{") else {}
    if r.status_code != 200:
        raise PostError((data.get("error") or {}).get("message") or f"Instagram said {r.status_code}")
    return data


def whoami(slot: int) -> dict:
    """{'user_id', 'username'} for the account behind slot's token (cached)."""
    saved = _saved()
    s = saved.get(str(slot)) or {}
    if s.get("user_id") and s.get("for_token") == token(slot):
        return s
    me = _get("me", slot, fields="user_id,username")
    s.update(user_id=me.get("user_id") or me.get("id"), username=me.get("username"), for_token=token(slot))
    saved[str(slot)] = s
    _write(saved)
    return s


def refresh_tokens() -> None:
    """Long-lived tokens last 60 days; refresh each one weekly so they never run out."""
    saved = _saved()
    for slot in range(1, 10):
        tok = token(slot)
        if not tok:
            continue
        s = saved.get(str(slot)) or {}
        if time.time() - s.get("refreshed", 0) < 7 * 86400:
            continue
        try:
            r = requests.get("https://graph.instagram.com/refresh_access_token",
                             params={"grant_type": "ig_refresh_token", "access_token": tok}, timeout=30)
            if r.status_code == 200 and r.json().get("access_token"):
                s.update(token=r.json()["access_token"], refreshed=time.time(),
                         from_env=(os.getenv(f"IG_TOKEN_{slot}") or "").strip())
                saved[str(slot)] = s
        except Exception as e:  # noqa: BLE001
            log.info("instagram: token refresh failed for account %s: %s", slot, e)
    _write(saved)


def expected(slot: int) -> str:
    from .accounts import load
    return next(((a.get("handle") or "").lstrip("@").lower() for a in load() if a["slot"] == slot), "")


def account_ok(slot: int) -> bool:
    return enabled(slot) and not (_saved().get(str(slot)) or {}).get("bad")


def publish(meta: dict) -> str:
    """Post one clip as a Reel on its account. Returns the Reel's link."""
    slot = meta.get("account_slot")
    if not slot or not account_ok(slot):
        raise PostError("that account isn't connected to Instagram")
    me = whoami(slot)
    if expected(slot) and (me.get("username") or "").lower() != expected(slot):
        raise PostError(f"account {slot} is logged in as @{me.get('username')}, not {meta.get('account')}. "
                        "Rename it on the Accounts page if you changed the handle.")
    video = OUT / meta["job"] / meta["file"]
    name = f"{meta['job']}-{meta['file']}".replace("/", "-")
    link = storage.host(video, name)
    try:
        box = _post(f"{me['user_id']}/media", slot, media_type="REELS", video_url=link,
                    caption=meta.get("caption") or "", share_to_feed="true")
        cid = box["id"]
        for _ in range(40):  # Instagram processes the video (usually under 2 minutes)
            time.sleep(15)
            st = _get(cid, slot, fields="status_code,status").get("status_code")
            if st == "FINISHED":
                break
            if st in ("ERROR", "EXPIRED"):
                raise PostError(f"Instagram couldn't process the video ({st})")
        else:
            raise PostError("Instagram took too long to process the video")
        media = _post(f"{me['user_id']}/media_publish", slot, creation_id=cid)["id"]
        meta["media_id"] = media
        url = _get(media, slot, fields="permalink").get("permalink") or ""
    finally:
        storage.unhost(name)
    log.info("instagram: posted %s on @%s -> %s", meta["id"], me.get("username"), url)
    return url
