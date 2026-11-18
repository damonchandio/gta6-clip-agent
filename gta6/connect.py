"""Connecting the accounts that clips get posted to: Instagram, TikTok and YouTube.

Each one is the platform's own sign-in page: the account owner signs in there, the platform sends this app back
a token, and no password ever passes through here. The app keys (one set per platform, made once in that
platform's developer console) go in .env (pasted on the Accounts page, or typed in by hand):

    IG_APP_ID, IG_APP_SECRET              Meta app with "Instagram API with Instagram Login"
    TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET   TikTok app with Login Kit (desktop) + Content Posting API
    YT_CLIENT_ID, YT_CLIENT_SECRET        Google Cloud OAuth client, type "Desktop app", YouTube Data API v3 on

Tokens are kept in data/gta6/tokens.json (Instagram's in data/clipper/ig_tokens.json, where the Instagram
poster and its token refresh read them). Nothing here ever prints or returns a token.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from pathlib import Path

import config

from . import net

HOME = config.DATA_DIR / "gta6"
TOKENS = HOME / "tokens.json"
PORT = 8771
PLATFORMS = ("instagram", "tiktok", "youtube")
NAMES = {"instagram": "Instagram", "tiktok": "TikTok", "youtube": "YouTube"}
KEYS = {"instagram": ("IG_APP_ID", "IG_APP_SECRET"), "tiktok": ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"),
        "youtube": ("YT_CLIENT_ID", "YT_CLIENT_SECRET")}
DEFAULT_REDIRECT = {"instagram": f"http://localhost:{PORT}/oauth/instagram/",
                    "tiktok": f"http://127.0.0.1:{PORT}/oauth/tiktok/",
                    "youtube": f"http://127.0.0.1:{PORT}/oauth/youtube/"}
IG_SCOPES = "instagram_business_basic,instagram_business_content_publish"
TT_SCOPES = "user.info.basic,user.info.profile,video.upload,video.publish"
YT_SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
TT_API = "https://open.tiktokapis.com/v2"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
SIGN_IN_MINUTES = 15

_lock = threading.Lock()
_pending: dict[str, dict] = {}


class ConnectError(RuntimeError):
    pass


def reload_env() -> None:
    """Pick up keys just added to .env, without restarting the app."""
    root = getattr(config, "ROOT", None)
    if root is not None:
        try:
            from dotenv import load_dotenv
            load_dotenv(root / ".env", override=True)
        except Exception:  # noqa: BLE001  no .env yet: nothing to pick up
            pass


def keys(platform: str) -> tuple[str, str]:
    a, b = KEYS[platform]
    return (os.getenv(a) or "").strip(), (os.getenv(b) or "").strip()


def has_keys(platform: str) -> bool:
    return all(keys(platform))


def redirect(platform: str) -> str:
    return (os.getenv(f"{platform.upper()}_REDIRECT_URI") or "").strip() or DEFAULT_REDIRECT[platform]


# --- token store ----------------------------------------------------------------------------------
def _load() -> dict:
    try:
        d = json.loads(TOKENS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    for p in ("tiktok", "youtube"):
        d.setdefault(p, {})
    return d


def _save(d: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = TOKENS.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    tmp.replace(TOKENS)
    try:
        os.chmod(TOKENS, 0o600)
    except OSError:
        pass


def _entry(platform: str, slot: int) -> dict:
    return _load()[platform].get(str(slot)) or {}


def _put(platform: str, slot: int, entry: dict | None) -> None:
    with _lock:
        d = _load()
        if entry is None:
            d[platform].pop(str(slot), None)
        else:
            d[platform][str(slot)] = entry
        _save(d)


# --- who is connected -------------------------------------------------------------------------------
def connected(platform: str, slot: int) -> bool:
    if platform == "instagram":
        try:
            from clipper import instagram
            return instagram.account_ok(slot)
        except Exception:  # noqa: BLE001
            return False
    e = _entry(platform, slot)
    return bool(e.get("refresh") and not e.get("broken"))


def describe(platform: str, slot: int) -> dict:
    """What the Connect page shows for one account on one platform (never a token)."""
    if platform == "instagram":
        try:
            from clipper import accounts, instagram
            saved = instagram._saved().get(str(slot)) or {}
            env = bool((os.getenv(f"IG_TOKEN_{slot}") or "").strip())
            ok = instagram.account_ok(slot)
            handle = next((a.get("handle") for a in accounts.load() if a["slot"] == slot), "") or ""
            name = ("@" + saved["username"]) if saved.get("username") else handle
            problem = "" if ok else ("Instagram said this token is for another account or stopped working."
                                     if saved.get("bad") else "")
            return {"connected": ok, "name": name if ok else "", "how": "sign-in" if saved.get("signed_in") else
                    (".env" if env else ""), "problem": problem}
        except Exception as e:  # noqa: BLE001
            return {"connected": False, "name": "", "how": "", "problem": str(e)[:120]}
    e = _entry(platform, slot)
    return {"connected": connected(platform, slot), "name": e.get("name", ""), "how": "sign-in" if e else "",
            "problem": e.get("broken", ""), "note": e.get("note", "")}


# --- signing in -------------------------------------------------------------------------------------
def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def challenge(platform: str, verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    # TikTok's desktop sign-in wants the SHA-256 as hex; Google wants it base64url (both say "S256")
    return digest.hex() if platform == "tiktok" else _b64url(digest)


def start(platform: str, slot: int) -> str:
    """The sign-in page to open for this account. Raises with the next step when the app keys are missing."""
    if platform not in PLATFORMS:
        raise ConnectError("unknown platform")
    if not 1 <= int(slot) <= 9:
        raise ConnectError("unknown account")
    reload_env()
    cid, secret = keys(platform)
    if not (cid and secret):
        a, b = KEYS[platform]
        raise ConnectError(f"{NAMES[platform]} needs {a} and {b} in .env first (steps are on this page), "
                           "then press Check keys.")
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(64)
    with _lock:
        now = time.time()
        for k in [k for k, v in _pending.items() if now - v["t"] > SIGN_IN_MINUTES * 60]:
            _pending.pop(k, None)
        _pending[state] = {"platform": platform, "slot": int(slot), "verifier": verifier, "t": now}
    q = urllib.parse.urlencode
    if platform == "instagram":
        return "https://www.instagram.com/oauth/authorize?" + q({
            "client_id": cid, "redirect_uri": redirect(platform), "response_type": "code", "scope": IG_SCOPES,
            "state": state, "force_reauth": "true", "enable_fb_login": "false"})
    if platform == "tiktok":
        return "https://www.tiktok.com/v2/auth/authorize/?" + q({
            "client_key": cid, "scope": (os.getenv("TIKTOK_SCOPES") or TT_SCOPES).strip(),
            "redirect_uri": redirect(platform), "state": state, "response_type": "code",
            "code_challenge": challenge("tiktok", verifier), "code_challenge_method": "S256"})
    return "https://accounts.google.com/o/oauth2/v2/auth?" + q({
        "client_id": cid, "redirect_uri": redirect(platform), "response_type": "code", "scope": YT_SCOPES,
        "access_type": "offline", "prompt": "consent select_account", "include_granted_scopes": "true",
        "state": state, "code_challenge": challenge("youtube", verifier), "code_challenge_method": "S256"})


def finish(platform: str, params: dict) -> str:
    """The platform sent the person back here: swap the code for tokens and remember them. Returns a sentence."""
    state = str(params.get("state") or "")
    with _lock:
        p = _pending.pop(state, None)
    if not p or p["platform"] != platform or time.time() - p["t"] > SIGN_IN_MINUTES * 60:
        raise ConnectError("That sign-in wasn't started from this app, or it took too long. Press Connect again.")
    if params.get("error"):
        why = params.get("error_description") or params.get("error_reason") or params.get("error")
        raise ConnectError(f"{NAMES[platform]} didn't connect: {str(why)[:160]}")
    code = str(params.get("code") or "").split("#")[0]
    if not code:
        raise ConnectError(f"{NAMES[platform]} sent no sign-in code back. Press Connect again.")
    slot = p["slot"]
    return {"instagram": _finish_instagram, "tiktok": _finish_tiktok, "youtube": _finish_youtube}[platform](
        slot, code, p["verifier"])


def _finish_instagram(slot: int, code: str, verifier: str) -> str:
    cid, secret = keys("instagram")
    r = net.req("POST", "https://api.instagram.com/oauth/access_token", data={
        "client_id": cid, "client_secret": secret, "grant_type": "authorization_code",
        "redirect_uri": redirect("instagram"), "code": code})
    j = net.body(r)
    j = (j.get("data") or [j])[0] if isinstance(j.get("data"), list) else j
    short = j.get("access_token")
    if r.status_code != 200 or not short:
        raise ConnectError("Instagram refused the sign-in: " + str(j.get("error_message") or j.get("error") or r.status_code)[:160])
    r = net.req("GET", "https://graph.instagram.com/access_token",
                params={"grant_type": "ig_exchange_token", "client_secret": secret, "access_token": short})
    long_tok = net.body(r).get("access_token") or short
    r = net.req("GET", "https://graph.instagram.com/me", params={"fields": "user_id,username", "access_token": long_tok})
    me = net.body(r)
    username = (me.get("username") or "").lower()
    if not username:
        raise ConnectError("Signed in, but Instagram wouldn't say which account it is. Is it a Professional account?")
    from clipper import accounts, instagram
    with _lock:
        saved = instagram._saved()
        for k, v in list(saved.items()):          # the same account can't sit in two slots
            if k != str(slot) and (v.get("username") or "").lower() == username and v.get("signed_in"):
                raise ConnectError(f"@{username} is already connected as account {k}. Disconnect it there first.")
        saved[str(slot)] = {"token": long_tok, "refreshed": time.time(), "signed_in": True,
                            "from_env": (os.getenv(f"IG_TOKEN_{slot}") or "").strip(),
                            "user_id": me.get("user_id") or me.get("id"), "username": username,
                            "for_token": long_tok, "bad": False}
        instagram.TOKENS.write_text(json.dumps(saved, indent=1), encoding="utf-8")
    accounts.set_handle(slot, username)
    return f"Instagram @{username} is connected as account {slot}."


def _tiktok_token(form: dict) -> dict:
    cid, secret = keys("tiktok")
    r = net.req("POST", f"{TT_API}/oauth/token/", data=dict(form, client_key=cid, client_secret=secret),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"})
    j = net.body(r)
    if r.status_code != 200 or not j.get("access_token"):
        raise ConnectError("TikTok refused: " + str(j.get("error_description") or j.get("error") or r.status_code)[:160])
    now = time.time()
    return {"access": j["access_token"], "refresh": j.get("refresh_token", ""), "open_id": j.get("open_id", ""),
            "scope": j.get("scope", ""), "expires": now + int(j.get("expires_in") or 7200) - 120,
            "refresh_expires": now + int(j.get("refresh_expires_in") or 0)}


def _tiktok_me(access: str) -> dict:
    for fields in ("open_id,display_name,username", "open_id,display_name"):
        r = net.req("GET", f"{TT_API}/user/info/", params={"fields": fields},
                    headers={"Authorization": f"Bearer {access}"})
        j = net.body(r)
        if r.status_code == 200 and (j.get("data") or {}).get("user"):
            return j["data"]["user"]
    return {}


def _finish_tiktok(slot: int, code: str, verifier: str) -> str:
    e = _tiktok_token({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect("tiktok"),
                       "code_verifier": verifier})
    me = _tiktok_me(e["access"])
    e.update(username=me.get("username", ""), name=("@" + me["username"]) if me.get("username") else me.get("display_name", ""))
    for k, v in _load()["tiktok"].items():
        if k != str(slot) and v.get("open_id") and v.get("open_id") == e["open_id"]:
            raise ConnectError(f"That TikTok account is already connected as account {k}. Disconnect it there first.")
    _put("tiktok", slot, e)
    return f"TikTok {e['name'] or 'account'} is connected as account {slot}."


def _google_token(form: dict) -> dict:
    cid, secret = keys("youtube")
    r = net.req("POST", GOOGLE_TOKEN, data=dict(form, client_id=cid, client_secret=secret))
    j = net.body(r)
    if r.status_code != 200 or not j.get("access_token"):
        raise ConnectError("Google refused: " + str(j.get("error_description") or j.get("error") or r.status_code)[:160])
    return j


def _finish_youtube(slot: int, code: str, verifier: str) -> str:
    j = _google_token({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect("youtube"),
                       "code_verifier": verifier})
    if not j.get("refresh_token"):
        raise ConnectError("Google didn't give a lasting sign-in. Remove this app at myaccount.google.com/permissions "
                           "and press Connect again.")
    r = net.req("GET", "https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet", "mine": "true"},
                headers={"Authorization": f"Bearer {j['access_token']}"})
    items = net.body(r).get("items") or []
    if not items:
        raise ConnectError("That Google account has no YouTube channel. Pick the channel's account when signing in.")
    ch = items[0]
    sn = ch.get("snippet") or {}
    for k, v in _load()["youtube"].items():
        if k != str(slot) and v.get("channel_id") == ch.get("id"):
            raise ConnectError(f"That channel is already connected as account {k}. Disconnect it there first.")
    _put("youtube", slot, {"access": j["access_token"], "refresh": j["refresh_token"],
                           "expires": time.time() + int(j.get("expires_in") or 3600) - 120,
                           "channel_id": ch.get("id"), "name": sn.get("customUrl") or sn.get("title") or "channel"})
    return f"YouTube {sn.get('title') or 'channel'} is connected as account {slot}."


# --- using the tokens ----------------------------------------------------------------------------------
def access(platform: str, slot: int) -> str:
    """A working access token for posting, refreshed when it has run out."""
    e = _entry(platform, slot)
    if not e.get("refresh") or e.get("broken"):
        raise ConnectError(f"{NAMES[platform]} isn't connected for account {slot}.")
    if time.time() < e.get("expires", 0):
        return e["access"]
    try:
        if platform == "tiktok":
            new = _tiktok_token({"grant_type": "refresh_token", "refresh_token": e["refresh"]})
            e.update({k: v for k, v in new.items() if v})          # TikTok hands back a new refresh token each time
        else:
            j = _google_token({"grant_type": "refresh_token", "refresh_token": e["refresh"]})
            e.update(access=j["access_token"], expires=time.time() + int(j.get("expires_in") or 3600) - 120)
            if j.get("refresh_token"):
                e["refresh"] = j["refresh_token"]
    except ConnectError as err:
        if "invalid_grant" in str(err) or "invalid" in str(err).lower() or "expired" in str(err).lower():
            e["broken"] = "The sign-in ran out. Press Connect again."
            _put(platform, slot, e)
        raise
    _put(platform, slot, e)
    return e["access"]


def name(platform: str, slot: int) -> str:
    return describe(platform, slot).get("name") or f"account {slot}"


def remove(platform: str, slot: int) -> str:
    if platform == "instagram":
        from clipper import instagram
        with _lock:
            saved = instagram._saved()
            saved.pop(str(slot), None)
            instagram.TOKENS.write_text(json.dumps(saved, indent=1), encoding="utf-8")
        if (os.getenv(f"IG_TOKEN_{slot}") or "").strip():
            return f"Forgot the sign-in, but IG_TOKEN_{slot} is still in .env, so account {slot} still posts. Remove that line to stop it."
        return f"Instagram disconnected from account {slot}."
    _put(platform, slot, None)
    return f"{NAMES[platform]} disconnected from account {slot}."


ENV_ALLOWED = {k for pair in KEYS.values() for k in pair} | {"GTA6_TELEGRAM_TOKEN"}
ENV_VALUE = re.compile(r"[A-Za-z0-9_\-.:~/+=@]{4,300}")


def env_file() -> Path:
    root = getattr(config, "ROOT", None)
    if root is None:
        raise ConnectError("I can't find the .env file.")
    return Path(root) / ".env"


def save_env(pairs: dict) -> None:
    """An app key pasted into the window goes into .env as one line each, replacing an older
    line of the same name. Only these key names can be written, and the values never leave this laptop."""
    clean = {}
    for k, v in pairs.items():
        v = str(v or "").strip().strip('"').strip("'")
        if k not in ENV_ALLOWED:
            raise ConnectError("that key can't be set from here")
        if not ENV_VALUE.fullmatch(v):
            raise ConnectError(f"That doesn't look right for {k}: paste it again, all of it, with no spaces.")
        clean[k] = v
    with _lock:
        f = env_file()
        try:
            lines = f.read_text(encoding="utf-8-sig").splitlines()
        except FileNotFoundError:
            lines = []
        for k, v in clean.items():
            pat = re.compile(rf"^\s*(export\s+)?{re.escape(k)}\s*=")
            hits = [i for i, line in enumerate(lines) if pat.match(line)]
            if hits:
                lines[hits[0]] = f"{k}={v}"
                for i in reversed(hits[1:]):
                    del lines[i]
            else:
                lines.append(f"{k}={v}")
        tmp = f.with_name(".env.gta6tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(f)
        os.environ.update(clean)


def save_keys(platform: str, key_id: str, secret: str) -> dict:
    if platform not in PLATFORMS:
        raise ConnectError("unknown platform")
    a, b = KEYS[platform]
    save_env({a: key_id, b: secret})
    return keys_status()


def keys_status() -> dict:
    reload_env()
    out = {p: {"ready": has_keys(p), "keys": list(KEYS[p]), "redirect": redirect(p)} for p in PLATFORMS}
    out["tiktok"]["public"] = (os.getenv("TIKTOK_AUDITED") or "").strip().lower() in ("1", "yes", "true")
    try:                                   # Instagram fetches each video from a link: is somewhere set up to host it?
        from clipper import storage
        storage._cfg()
        out["instagram"]["hosting"] = True
    except ImportError:                    # a host app hosts the videos its own way
        out["instagram"]["hosting"] = None
    except Exception:  # noqa: BLE001
        out["instagram"]["hosting"] = False
    return out
