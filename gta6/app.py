"""The window: a page served to this laptop only (http://127.0.0.1:8771), opened as its own app window."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from . import agent, campaigns, connect, tg

PORT = connect.PORT
HOSTS = (f"127.0.0.1:{PORT}", f"localhost:{PORT}")
UI = Path(__file__).with_name("ui.html")
FONTS = Path(__file__).resolve().parents[1] / "assets" / "fonts"
log = logging.getLogger("gta6")
KEEP = ("id", "title", "caption", "seconds", "status", "account", "account_slot", "why", "source_kind",
        "source_title", "post_url", "campaign", "campaign_url", "approved_at", "posted_at", "job")


def _posts(c: dict) -> dict:
    """Per platform: where the clip is (never more than the page needs)."""
    out = {}
    for p, r in (c.get("posts") or {}).items():
        out[p] = {k: r.get(k) for k in ("url", "inbox", "note", "error", "tries", "final", "at") if r.get(k) is not None}
    return out


def snapshot() -> dict:
    from clipper import accounts
    st = agent.load()
    try:
        r = json.loads(agent.RESEARCH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        r = {"at": 0, "keywords": [], "hashtags": [], "errors": []}
    clips = agent.mine()
    today = time.strftime("%Y%m%d")
    accts = []
    for a in accounts.load():
        s = a["slot"]
        plats = {}
        for p in connect.PLATFORMS:
            d = connect.describe(p, s)
            d["on"] = f"{s}:{p}" not in st["off"]
            plats[p] = d
        accts.append({"slot": s, "handle": a.get("handle") or "", "platforms": plats,
                      "today": sum(c.get("account_slot") == s and c.get("job", "")[-13:-5] == today
                                   and c.get("status") != "rejected" for c in clips),
                      "posted": sum(c.get("account_slot") == s and c.get("status") == "posted" for c in clips)})
    out_clips = []
    for c in clips[:150]:
        x = {k: c.get(k) for k in KEEP}
        x["posts"] = _posts(c)
        x["targets"] = agent.targets(c.get("account_slot"), st)
        out_clips.append(x)
    return {"on": st["on"], "doing": agent.status["doing"], "posting": agent.status["posting"],
            "note": agent.status["note"],
            "next_check": max(0, int(st["last_check"] + agent.CHECK_EVERY - time.time())),
            "hours": agent.HOURS, "post_hours": agent.POST_HOURS,
            "folders": st["folders"],
            "channels": [{k: c.get(k) for k in ("url", "credit", "campaign_id")} for c in st["channels"]],
            "links": [{k: lk.get(k) for k in ("url", "campaign_id", "tried")} for lk in st["links"]],
            "active": [{"id": k, **{f: v.get(f) for f in ("title", "brand", "platforms", "url", "rate",
                                                          "required_hashtags", "required_mentions")}}
                       for k, v in st["campaigns"].items()],
            "accounts": accts, "clips": out_clips, "research": r, "campaigns": campaigns.snapshot(),
            "keys": connect.keys_status(), "telegram": tg.status(), "events": list(agent.events),
            "name": (os.getenv("DISPLAY_NAME") or "").strip()[:30]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def send(self, code: int, body, ctype: str = "application/json", headers: dict | None = None) -> None:
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def local(self) -> bool:
        return self.headers.get("Host") in HOSTS       # a web page elsewhere can't reach this by another name

    def do_GET(self):  # noqa: N802
        if not self.local():
            return self.send(403, {"error": "this laptop only"})
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        if path == "/":
            return self.send(200, UI.read_bytes(), "text/html; charset=utf-8",
                             {"Content-Security-Policy": "default-src 'self'; img-src 'self' data:; media-src 'self'; "
                              "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                              "font-src 'self'; connect-src 'self'; frame-ancestors 'none'"})
        if path == "/api/state":
            return self.send(200, snapshot())
        if path.startswith("/media/"):
            return self.media(path[7:])
        if path.startswith("/fonts/"):
            name = path[7:]
            f = FONTS / name
            if re.fullmatch(r"IBMPlex[\w-]+\.ttf", name) and f.is_file():
                return self.send(200, f.read_bytes(), "font/ttf", {"Cache-Control": "max-age=86400"})
            return self.send(404, {"error": "not found"})
        m = re.fullmatch(r"/oauth/(instagram|tiktok|youtube)/?", path)
        if m:
            ok = True
            try:
                msg = connect.finish(m.group(1), dict(parse_qsl(parts.query)))
            except Exception as e:  # noqa: BLE001  the reason goes onto the page
                ok, msg = False, str(e).splitlines()[0][:200]
            agent.note(msg)
            return self.send(200, done_page(ok, msg), "text/html; charset=utf-8",
                             {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                              "script-src 'unsafe-inline'; frame-ancestors 'none'"})
        self.send(404, {"error": "not found"})

    def media(self, clip_id: str):
        """The clip's video, with byte ranges so the player can seek."""
        try:
            from clipper import OUT
            m = agent.meta_of(clip_id)
            f = OUT / m["job"] / m["file"]
            size = f.stat().st_size
        except Exception:  # noqa: BLE001
            return self.send(404, {"error": "not found"})
        rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        start, end = 0, size - 1
        if rng and (rng.group(1) or rng.group(2)):
            if rng.group(1):
                start = int(rng.group(1))
                end = min(int(rng.group(2)), size - 1) if rng.group(2) else size - 1
            else:
                start = max(0, size - int(rng.group(2)))
            if start > end or start >= size:
                return self.send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
        with open(f, "rb") as fh:
            fh.seek(start)
            data = fh.read(end - start + 1)
        hdr = {"Accept-Ranges": "bytes"}
        if rng:
            hdr["Content-Range"] = f"bytes {start}-{end}/{size}"
        self.send(206 if rng else 200, data, "video/mp4", hdr)

    def do_POST(self):  # noqa: N802
        # the custom header means a form or script on another site can't trigger these
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= 100_000:
            self.close_connection = True
            return self.send(413, {"error": "too big"})
        raw = self.rfile.read(n)       # read it even when refusing: closing on unread data resets the connection
        if not self.local() or self.headers.get("X-Agent") != "gta6":
            return self.send(403, {"error": "this laptop only"})
        try:
            d = json.loads(raw or b"{}")
            out = act(urlsplit(self.path).path, d)
            self.send(200, {"ok": True, "result": out})
        except Exception as e:  # noqa: BLE001  the message goes straight onto the page
            self.send(400, {"error": str(e).splitlines()[0][:220]})


def done_page(ok: bool, msg: str) -> bytes:
    """What the login window shows when the platform sends it back: the result, then it closes itself."""
    import html
    colour = "#37c98a" if ok else "#f1705f"
    return f"""<!doctype html><meta charset="utf-8"><title>{'Connected' if ok else 'Not connected'}</title>
<body style="margin:0;display:grid;place-items:center;min-height:100vh;background:#07060f;color:#fbf8ff;font:16px 'Segoe UI',sans-serif">
<div style="max-width:460px;padding:28px;border:1px solid #352a5c;background:#110d24;text-align:center">
<div style="font-size:40px;color:{colour}">{'✓' if ok else '✕'}</div><p>{html.escape(msg)}</p>
<p style="color:#8b82ad;font-size:13px">{'This window closes by itself.' if ok else 'Close this window and try again.'}</p>
<a href="/#connect" style="color:#2ee6d6">Back to the app</a></div>
<script>{'setTimeout(function(){window.close();location.href="/#connect"},1800)' if ok else ''}</script>""".encode()


def _bg(fn, **kw) -> str:
    threading.Thread(target=fn, kwargs=kw, daemon=True).start()
    return "started"


def act(path: str, d: dict):
    s = lambda k: str(d.get(k) or "")  # noqa: E731
    if path == "/api/on":
        return agent.change(lambda st: st.update(on=bool(d.get("on"))))["on"]
    if path == "/api/run":
        return _bg(agent.tick, force=True)
    if path == "/api/post-now":
        return _bg(agent.post_due)
    if path == "/api/research":
        return _bg(agent.research, force=True)
    if path == "/api/decide":
        return agent.decide(s("id"), s("decision"), d.get("caption"))
    if path == "/api/caption":
        return agent.set_caption(s("id"), s("caption"))["caption"]
    if path == "/api/retry":
        return agent.retry(s("id"), s("platform"))
    if path == "/api/folder":
        return agent.add_folder(s("path"), s("campaign_id"))
    if path == "/api/channel":
        return agent.add_channel(s("url"), s("credit"), bool(d.get("permitted")), s("campaign_id"))
    if path == "/api/remove":
        return agent.remove_source(s("kind"), int(d["i"]))
    if path == "/api/campaigns/search":
        return len(campaigns.search(s("q")))
    if path == "/api/campaigns/refresh":
        return _bg(agent.campaigns_check, force=True)
    if path == "/api/campaigns/use":
        return agent.use_campaign(s("id"), s("folder"))
    if path == "/api/campaigns/drop":
        return agent.drop_campaign(s("id"))
    if path == "/api/connect":
        return connect.start(s("platform"), int(d.get("slot") or 0))
    if path == "/api/disconnect":
        return agent.note(connect.remove(s("platform"), int(d.get("slot") or 0)))
    if path == "/api/toggle":
        return agent.toggle(int(d.get("slot") or 0), s("platform"), bool(d.get("on")))
    if path == "/api/keys":
        connect.reload_env()
        return connect.keys_status()
    if path == "/api/keys/save":
        return connect.save_keys(s("platform"), s("id"), s("secret"))
    if path == "/api/telegram/token":
        return tg.save_token(s("token"))
    if path == "/api/telegram/link":
        return tg.link()
    if path == "/api/telegram/test":
        if not tg.say("👋 Test from the GTA 6 Clip Agent. Clips will arrive here with Approve and Bin buttons."):
            raise ValueError("Telegram isn't connected yet.")
        return "sent"
    if path == "/api/telegram/unlink":
        return tg.unlink()
    if path == "/api/account":
        from clipper import accounts
        return accounts.set_handle(int(d["slot"]), s("handle"))["handle"]
    raise ValueError("unknown action")


def open_window(url: str) -> None:
    """Its own window with no address bar when Edge or Chrome is there; otherwise a normal browser tab."""
    for name in ("msedge", "chrome"):
        exe = shutil.which(name) or next((p for p in (
            rf"C:\Program Files (x86)\Microsoft\Edge\Application\{name}.exe",
            rf"C:\Program Files\Microsoft\Edge\Application\{name}.exe",
            rf"C:\Program Files\Google\Chrome\Application\{name}.exe") if Path(p).exists()), None)
        if exe:
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1360,900"])
                return
            except OSError:
                pass
    webbrowser.open(url)


def main() -> None:
    agent.HOME.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(), logging.FileHandler(agent.HOME / "agent.log", encoding="utf-8")])
    url = f"http://127.0.0.1:{PORT}/"
    try:
        ThreadingHTTPServer.allow_reuse_address = os.name != "nt"    # on Windows, reuse lets a second copy share the port
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print("The GTA 6 clip agent is already running. Opening its window.")
        return open_window(url)
    threading.Thread(target=agent.run_forever, name="gta6-loop", daemon=True).start()
    open_window(url)
    print(f"GTA 6 Clip Agent (Damon Dynamics) is running at {url}\nLeave this window open. Close it to stop the agent.")
    server.serve_forever()
