"""Approvals on Telegram through the agent's own bot, so it works on its own.

Setup (once): make a bot with @BotFather, put its token in .env as GTA6_TELEGRAM_TOKEN, press Connect Telegram
in the app and open the link on the phone of whoever approves clips. Only that chat can approve; anyone else
who finds the bot is ignored. Without GTA6_TELEGRAM_TOKEN, approve clips in the app window.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time

import config

from . import net

HOME = config.DATA_DIR / "gta6"
FILE = HOME / "telegram.json"
LINK_MINUTES = 30
VIDEO_LIMIT = 49 * 1024 * 1024        # bots can't send videos over 50 MB
log = logging.getLogger("gta6")
_lock = threading.Lock()


def token() -> str:
    t = (os.getenv("GTA6_TELEGRAM_TOKEN") or "").strip()
    # a host app's own bot (TELEGRAM_BOT_TOKEN) already has a reader; two readers on one bot steal each other's taps
    return "" if t and t == (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip() else t


def own() -> bool:
    return bool(token())


def _load() -> dict:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"chat_id": None, "name": "", "offset": 0, "msgs": {}}


def _save(d: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 30 * 86400
    d["msgs"] = {k: v for k, v in (d.get("msgs") or {}).items() if v.get("t", 0) > cutoff}
    FILE.write_text(json.dumps(d, indent=1), encoding="utf-8")


def _change(fn):
    with _lock:
        d = _load()
        out = fn(d)
        _save(d)
        return out


def linked() -> bool:
    return own() and bool(_load().get("chat_id"))


def _api(method: str, files: dict | None = None, timeout: float = 60, **params):
    for k in ("reply_markup",):
        if k in params and not isinstance(params[k], str):
            params[k] = json.dumps(params[k])
    url = f"https://api.telegram.org/bot{token()}/{method}"
    r = net.req("POST", url, data=params, files=files, timeout=timeout)
    j = net.body(r)
    if not j.get("ok"):
        raise RuntimeError(f"Telegram: {j.get('description') or r.status_code}")
    return j.get("result")


def status() -> dict:
    d = _load()
    out = {"own": own(), "linked": linked(), "name": d.get("name", ""), "bot": d.get("bot", ""),
           "via_host": not own() and bool((os.getenv("TELEGRAM_BOT_TOKEN") or "").strip())}
    if not own() and (os.getenv("GTA6_TELEGRAM_TOKEN") or "").strip():
        out["problem"] = "GTA6_TELEGRAM_TOKEN is the same as TELEGRAM_BOT_TOKEN. Make a separate bot with @BotFather."
    return out


def save_token(new: str) -> str:
    """A bot token pasted into the window: checked with Telegram first, then kept in .env."""
    from . import connect
    new = (new or "").strip()
    if new and new == (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip():
        raise ValueError("That bot is already used by another app here. Make a separate one with @BotFather.")
    if not connect.ENV_VALUE.fullmatch(new):
        raise ValueError("That doesn't look like a bot token. Copy the whole line BotFather sent.")
    r = net.req("POST", f"https://api.telegram.org/bot{new}/getMe", timeout=20)
    me = net.body(r)
    if not me.get("ok"):
        raise ValueError("Telegram doesn't know that token. Copy it again from @BotFather.")
    connect.save_env({"GTA6_TELEGRAM_TOKEN": new})
    _change(lambda d: d.update(bot=(me.get("result") or {}).get("username", ""), chat_id=None, name="", offset=0))
    return (me.get("result") or {}).get("username", "")


def link() -> dict:
    """A one-time link that ties the bot to whoever opens it."""
    if not own():
        raise ValueError("Add GTA6_TELEGRAM_TOKEN to .env first (steps are on this page), then press Check keys.")
    me = _api("getMe")
    code = secrets.token_hex(4)
    _change(lambda d: d.update(code=code, code_t=time.time(), bot=me.get("username", "")))
    return {"url": f"https://t.me/{me.get('username')}?start={code}", "bot": me.get("username", ""), "code": code}


def unlink() -> None:
    _change(lambda d: d.update(chat_id=None, name="", code="", await_caption=None))


def _keyboard(ref: str) -> dict:
    return {"inline_keyboard": [[{"text": "✅ Approve", "callback_data": f"g6|ok|{ref}"},
                                 {"text": "✏️ Caption", "callback_data": f"g6|cap|{ref}"},
                                 {"text": "❌ Bin", "callback_data": f"g6|no|{ref}"}]]}


def card(meta: dict, where: str) -> str:
    head = f"🎮 GTA 6 clip for {where}" + (f" · campaign: {meta['campaign']}" if meta.get("campaign") else "")
    return f"{head}\n{meta.get('title') or ''} ({meta.get('seconds')}s)\n\n{meta.get('caption') or ''}"[:1000]


def send_clip(meta: dict, video, where: str) -> bool:
    chat = _load().get("chat_id")
    if not (own() and chat):
        return False
    ref = secrets.token_hex(4)
    text = card(meta, where)
    try:
        if video and video.exists() and video.stat().st_size <= VIDEO_LIMIT:
            with open(video, "rb") as fh:
                msg = _api("sendVideo", files={"video": (video.name, fh, "video/mp4")}, timeout=300, chat_id=chat,
                           caption=text, supports_streaming="true", reply_markup=_keyboard(ref))
        else:
            msg = _api("sendMessage", chat_id=chat, text=text + "\n\n(watch it in the app)", reply_markup=_keyboard(ref))
    except Exception as e:  # noqa: BLE001
        log.warning("telegram send failed: %s", e)
        return False
    _change(lambda d: d["msgs"].__setitem__(ref, {"clip": meta["id"], "mid": msg.get("message_id"), "t": time.time(),
                                                  "video": "video" in msg}))
    return True


def say(text: str) -> bool:
    chat = _load().get("chat_id")
    if not (own() and chat):
        return False
    try:
        _api("sendMessage", chat_id=chat, text=text[:4000], disable_web_page_preview="true")
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("telegram message failed: %s", e)
        return False


def resolved(clip_id: str, verdict: str) -> None:
    """A clip was decided in the app: take the buttons off its Telegram message."""
    d = _load()
    for ref, m in (d.get("msgs") or {}).items():
        if m.get("clip") == clip_id and not m.get("done") and d.get("chat_id"):
            try:
                _api("editMessageReplyMarkup", chat_id=d["chat_id"], message_id=m["mid"],
                     reply_markup={"inline_keyboard": []})
            except Exception:  # noqa: BLE001  an old message may be gone already
                pass
            _change(lambda dd, r=ref: dd["msgs"].get(r, {}).update(done=verdict))


# --- reading taps and replies ---------------------------------------------------------------------
def handle(u: dict) -> None:
    from . import agent
    d = _load()
    chat = d.get("chat_id")
    if "message" in u:
        msg = u["message"]
        cid = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if text.startswith("/start"):
            code = text.split(maxsplit=1)[1].strip() if " " in text else ""
            if code and code == d.get("code") and time.time() - d.get("code_t", 0) < LINK_MINUTES * 60:
                who = (msg.get("from") or {})
                name = who.get("first_name") or who.get("username") or "you"
                _change(lambda dd: dd.update(chat_id=cid, name=name, code=""))
                _api("sendMessage", chat_id=cid, text=f"✅ Connected, {name}. New GTA 6 clips will come here for "
                                                         "your yes or no.")
                agent.note(f"Telegram connected to {name}.")
            elif cid != chat:
                _api("sendMessage", chat_id=cid, text="That link has expired. Press Connect Telegram in the "
                                                          "GTA 6 Clip Agent and open the new link.")
            return
        if cid != chat or not chat:
            return                                   # strangers can't approve anything
        ref = d.get("await_caption")
        if ref and text:
            m = d["msgs"].get(ref) or {}
            _change(lambda dd: dd.update(await_caption=None))
            try:
                meta = agent.set_caption(m["clip"], text)
            except Exception as e:  # noqa: BLE001
                _api("sendMessage", chat_id=chat, text=f"Couldn't change it: {str(e)[:120]}")
                return
            try:
                method = "editMessageCaption" if m.get("video") else "editMessageText"
                key = "caption" if m.get("video") else "text"
                _api(method, chat_id=chat, message_id=m["mid"], reply_markup=_keyboard(ref),
                     **{key: card(meta, agent.where_label(meta))})
            except Exception:  # noqa: BLE001
                pass
            _api("sendMessage", chat_id=chat, text="Caption changed. Tap ✅ Approve on the clip when you're happy.")
        return
    cq = u.get("callback_query")
    if not cq:
        return
    cid = ((cq.get("message") or {}).get("chat") or {}).get("id")
    parts = (cq.get("data") or "").split("|")
    if cid != chat or not chat or len(parts) != 3 or parts[0] != "g6":
        _api("answerCallbackQuery", callback_query_id=cq["id"], text="Not yours to approve.")
        return
    _, action, ref = parts
    m = d["msgs"].get(ref)
    if not m or m.get("done"):
        _api("answerCallbackQuery", callback_query_id=cq["id"], text="Already sorted.")
        return
    if action == "cap":
        _change(lambda dd: dd.update(await_caption=ref))
        _api("answerCallbackQuery", callback_query_id=cq["id"])
        _api("sendMessage", chat_id=chat, text="Send me the new caption (hashtags included).")
        return
    try:
        verdict = agent.decide(m["clip"], "approve" if action == "ok" else "reject", source="telegram")
    except Exception as e:  # noqa: BLE001
        _api("answerCallbackQuery", callback_query_id=cq["id"], text=str(e)[:150])
        return
    _api("answerCallbackQuery", callback_query_id=cq["id"], text="Approved" if verdict == "queued" else "Binned")
    _change(lambda dd: dd["msgs"][ref].update(done=verdict))
    try:
        _api("editMessageReplyMarkup", chat_id=chat, message_id=m["mid"], reply_markup={"inline_keyboard": []})
    except Exception:  # noqa: BLE001
        pass
    if verdict == "queued":
        meta = agent.meta_of(m["clip"])
        _api("sendMessage", chat_id=chat, text=f"Approved. It goes out on {agent.where_label(meta)} at the next "
                                                 "free slot, and I'll send the links.")


def poll_forever() -> None:
    warned = False
    while True:
        if not own():
            time.sleep(20)
            continue
        try:
            for u in _poll(_load().get("offset", 0)):
                _change(lambda dd, i=u["update_id"]: dd.update(offset=i + 1))
                try:
                    handle(u)
                except Exception:  # noqa: BLE001  one odd message must not stop the reader
                    log.exception("telegram update")
            warned = False
        except Exception as e:  # noqa: BLE001
            if not warned:
                log.warning("telegram reader: %s", e)
                warned = True
            time.sleep(15)


def _poll(offset: int) -> list:
    r = net.req("POST", f"https://api.telegram.org/bot{token()}/getUpdates", timeout=40, data={
        "offset": offset, "timeout": 25, "allowed_updates": json.dumps(["message", "callback_query"])})
    j = net.body(r)
    if not j.get("ok"):
        raise RuntimeError(j.get("description") or f"getUpdates {r.status_code}")
    return j.get("result") or []
