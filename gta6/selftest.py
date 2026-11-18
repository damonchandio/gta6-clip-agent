"""python -m gta6 --selftest: runs the agent against fakes of the clip engine's modules and of every web service
(Content Rewards, TikTok, YouTube, Instagram, Telegram). No network, no real clips, no real data touched."""
from __future__ import annotations

import datetime as dt
import hashlib
import base64
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class Web:
    """A fake internet: each service answers from a small script, and every call is recorded."""

    def __init__(self):
        self.calls, self.tt_status, self.yt_fail, self.updates = [], "SEND_TO_USER_INBOX", 0, []
        self.sent = []

    def __call__(self, method, url, timeout=60, **kw):
        u = urlsplit(url)
        self.calls.append((method, u.netloc + u.path, kw))
        data = kw.get("data")
        form = data if isinstance(data, dict) else {}
        body = json.loads(data) if isinstance(data, str) and data.startswith("{") else {}
        host, path = u.netloc, u.path
        if host == "contentrewards.com":
            word = (kw.get("params") or {}).get("search", "")
            items = [{"id": "c-gta", "name": "GTA 6 Clipping Campaign - Tokyo Girl", "organizationName": "Placements",
                      "description": "Clip the trailer. Use #TokyoGirl and tag @placements. Mail me at a@b.com",
                      "platforms": ["instagram", "tiktok"], "primaryPayoutCents": 70,
                      "referenceMaterials": [{"url": "https://www.youtube.com/watch?v=abc"},
                                             {"url": "https://drive.google.com/drive/folders/xyz"}]},
                     {"id": "c-fn", "name": "Fortnite map", "organizationName": "Epic fan", "description": "maps",
                      "platforms": ["youtube"], "primaryPayoutCents": 200, "referenceMaterials": []}]
            hits = [x for x in items if word.split()[0].lower() in (x["name"] + x["description"]).lower()] if word else items
            return Resp(200, {"data": hits, "pagination": {"nextCursor": None}})
        if host == "open.tiktokapis.com":
            if path == "/v2/oauth/token/":
                assert form["client_key"] == "tk" and form["client_secret"] == "ts", form
                if form["grant_type"] == "authorization_code":
                    assert form["code"] == "tt-code" and len(form["code_verifier"]) >= 43
                    return Resp(200, {"access_token": "TT-ACCESS-1", "refresh_token": "TT-REFRESH-1", "open_id": "o1",
                                      "expires_in": 7200, "refresh_expires_in": 15552000, "scope": "video.upload"})
                assert form["refresh_token"] == "TT-REFRESH-1"
                return Resp(200, {"access_token": "TT-ACCESS-2", "refresh_token": "TT-REFRESH-2", "open_id": "o1",
                                  "expires_in": 7200, "refresh_expires_in": 15552000})
            if path == "/v2/user/info/":
                return Resp(200, {"data": {"user": {"open_id": "o1", "display_name": "Clips", "username": "gta6daily"}}})
            if path == "/v2/post/publish/inbox/video/init/":
                assert body["source_info"]["source"] == "FILE_UPLOAD"
                return Resp(200, {"data": {"publish_id": "p1", "upload_url": "https://open-upload.tiktokapis.com/up?x=1"},
                                  "error": {"code": "ok"}})
            if path == "/v2/post/publish/status/fetch/":
                return Resp(200, {"data": {"status": self.tt_status}, "error": {"code": "ok"}})
        if host == "open-upload.tiktokapis.com":
            assert kw["headers"]["Content-Range"].startswith("bytes 0-")
            return Resp(201)
        if host == "oauth2.googleapis.com":
            if form["grant_type"] == "authorization_code":
                assert form["code"] == "yt-code"
                return Resp(200, {"access_token": "YT-ACCESS-1", "refresh_token": "YT-REFRESH-1", "expires_in": 3600})
            return Resp(200, {"access_token": "YT-ACCESS-2", "expires_in": 3600})
        if host == "www.googleapis.com" and path == "/youtube/v3/channels":
            return Resp(200, {"items": [{"id": "UC1", "snippet": {"title": "GTA 6 Daily", "customUrl": "@gta6daily"}}]})
        if host == "www.googleapis.com" and path == "/upload/youtube/v3/videos":
            if self.yt_fail:
                self.yt_fail -= 1
                return Resp(403, {"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}})
            assert body["status"]["privacyStatus"] == "public" and body["snippet"]["title"].endswith("#Shorts")
            return Resp(200, {}, {"Location": "https://www.googleapis.com/upload/session/1"})
        if host == "www.googleapis.com" and path == "/upload/session/1":
            return Resp(200, {"id": "vid123"})
        if host == "api.telegram.org":
            method_name = path.rsplit("/", 1)[1]
            self.sent.append((method_name, form))
            if method_name == "getMe":
                return Resp(200, {"ok": True, "result": {"username": "dd_clips_bot"}})
            if method_name == "getUpdates":
                ups, self.updates = self.updates, []
                return Resp(200, {"ok": True, "result": ups})
            return Resp(200, {"ok": True, "result": {"message_id": len(self.sent)}})
        raise AssertionError(f"unexpected call {method} {url}")


def fakes(root: Path, web: Web) -> dict:
    """Stand-ins for the clip engine's modules, put in place before the agent is imported."""
    out = root / "out"
    out.mkdir()
    said, posted, ig_saved = [], [], {}

    def mod(name, **kw):
        m = types.ModuleType(name)
        m.__dict__.update(kw)
        sys.modules[name] = m
        return m

    def clips():
        return [json.loads(f.read_text(encoding="utf-8")) for f in sorted(out.glob("*/clip_*.json"), reverse=True)]

    def meta_path(cid):
        job, name = cid.split("/", 1)
        p = (out / job / f"{name}.json").resolve()
        if out.resolve() not in p.parents:
            raise ValueError("bad clip id")
        return p

    def make_clips(src, rules, count, progress=print):
        if "drive.google.com" in src:
            raise RuntimeError("can't download folders")
        job = f"gta6-{len(list(out.iterdir()))}-" + time.strftime("%Y%m%d-%H%M")
        (out / job).mkdir(exist_ok=True)
        for i in (1, 2):
            m = {"id": f"{job}/clip_0{i}", "file": f"clip_0{i}.mp4", "job": job, "title": "Wild chase", "seconds": 20,
                 "caption": "#ad Cops never saw it coming #viral #ad", "status": "pending",
                 "account": "@a", "account_slot": rules["account_slot"], "campaign_id": rules.get("campaign_id", "")}
            (out / job / f"clip_0{i}.json").write_text(json.dumps(m), encoding="utf-8")
            (out / job / f"clip_0{i}.mp4").write_bytes(b"\0" * 5000)
        return {"job": job, "clips": 2}

    def candidates(link, limit):
        if "watch?v=abc" in link:
            return [{"id": "abc", "url": link, "title": "Tokyo Girl trailer", "seconds": 90, "linked": True}]
        return [{"id": "s1", "url": "u1", "title": "GTA 6 short", "seconds": 50, "linked": False},
                {"id": "x1", "url": "u2", "title": "Minecraft build", "seconds": 900, "linked": False},
                {"id": "g1", "url": "u3", "title": "GTA VI: 20 minutes of chaos", "seconds": 1200, "linked": False}]

    def from_api(items):
        return [{"id": x["id"], "name": x["name"], "brand": x.get("organizationName", ""), "rate": x["primaryPayoutCents"] / 100,
                 "remaining": 1900.0, "platforms": x["platforms"], "description": x["description"],
                 "sources": [r["url"] for r in x.get("referenceMaterials") or []]} for x in items]

    def rank(cs):
        for c in cs:
            c["score"], c["why"] = c["rate"], [f"${c['rate']:.2f} per 1K views"]
        return sorted(cs, key=lambda c: -c["score"])

    def handle(slot, h):
        accts[slot - 1]["handle"] = "@" + h.lstrip("@")
        return accts[slot - 1]

    accts = [{"slot": 1, "handle": "@a"}, {"slot": 2, "handle": "@b"}]
    mod("config", DATA_DIR=root, ROOT=root)
    pkg = mod("clipper", OUT=out)
    pkg.__path__ = []
    pkg.desk = mod("clipper.desk", clips=clips, _meta_path=meta_path)
    pkg.engine = mod("clipper.engine", make_clips=make_clips, duration=lambda p: 5 if "short" in p.name else 90)
    pkg.autopilot = mod("clipper.autopilot", candidates=candidates, _load=lambda: {"on": False}, set_on=lambda on: None)
    pkg.accounts = mod("clipper.accounts", load=lambda: accts, set_handle=handle)
    pkg.instagram = mod("clipper.instagram", account_ok=lambda s: s == 1, _saved=lambda: ig_saved,
                        TOKENS=root / "ig_tokens.json", refresh_tokens=lambda: None,
                        publish=lambda m: posted.append(m["id"]) or "https://www.instagram.com/reel/x/")
    pkg.phoneflow = mod("clipper.phoneflow", send_clip=lambda m: True)
    pkg.scout = mod("clipper.scout", from_api=from_api, rank=rank)
    pkg.whop = mod("clipper.whop", can_submit=lambda: False)
    mod("approvals", say=said.append)
    return {"out": out, "said": said, "posted": posted}


def must_fail(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except (ValueError, RuntimeError):
        return
    raise SystemExit(f"{fn.__name__}{a} should have refused")


def run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        web = Web()
        env = {"GTA6_TELEGRAM_TOKEN": "", "TELEGRAM_BOT_TOKEN": "host-bot", "TIKTOK_CLIENT_KEY": "tk",
               "TIKTOK_CLIENT_SECRET": "ts", "YT_CLIENT_ID": "yc", "YT_CLIENT_SECRET": "ys", "IG_APP_ID": "",
               "IG_APP_SECRET": "", "TIKTOK_AUDITED": ""}
        saved_env = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            _run(root, web)
        finally:
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def _run(root: Path, web: Web) -> None:
    f = fakes(root, web)
    for name in [n for n in sys.modules if n.startswith("gta6.") and n != "gta6.selftest"]:
        sys.modules.pop(name)
    from . import agent, app, campaigns, connect, net, post, tg
    net.req = web
    tags = agent.rank_hashtags([{"title": "Chaos #GTA6 #shorts #vicecity", "view_count": 990},
                                {"title": "x", "description": "#gta6 #2026 #fyp", "view_count": 90}])
    agent.research = lambda force=False: {"keywords": [{"phrase": "gta 6 map", "score": 9}], "hashtags": tags}

    # --- research ---
    kw = agent.rank_keywords({"gta 6": ["gta 6", "gta 6 map", "gta 6 trailer"], "gta vi": ["gta 6 map", "minecraft"]})
    assert [k["phrase"] for k in kw] == ["gta 6 map", "gta 6 trailer"] and kw[0]["score"] == 19, kw
    assert [t["tag"] for t in tags] == ["gta6", "vicecity"] and tags[0]["videos"] == 2, tags
    r = {"hashtags": tags + [{"tag": t, "score": 1.0} for t in "abcde"]}
    picked = agent.pick_tags(r, 5, "clip-1")
    assert picked[0] == "gta6" and len(set(picked)) == 5 and picked == agent.pick_tags(r, 5, "clip-1"), picked

    # --- sources ---
    must_fail(agent.add_channel, "https://example.com/x", "", True)
    must_fail(agent.add_channel, "https://www.youtube.com/@a", "", False)
    caps = root / "caps"
    caps.mkdir()
    old = time.time() - 600
    for name in ("short.mp4", "night drive.mp4", "notes.txt"):
        (caps / name).write_bytes(b"x" * 10)
        os.utime(caps / name, (old, old))
    (caps / "still copying.mp4").write_bytes(b"x")
    agent.add_folder(str(caps))
    must_fail(agent.add_folder, str(caps))
    agent.add_channel("https://www.youtube.com/@creator", "@creator on YouTube", True)
    st = agent.load()
    assert agent.next_source(st)["title"] == "night drive"
    assert agent.next_source({**st, "folders": []})["id"] == "g1"
    assert agent.next_source({**st, "folders": [], "used": ["g1"]}) is None

    # --- a clip round, through a host app's bot (no bot of its own yet) ---
    assert agent.tick(force=True).startswith("Made 2 clips"), agent.status
    mine = agent.mine()
    cap = mine[0]["caption"]
    assert "#ad" not in cap and "#viral" not in cap and cap.startswith("Cops never saw it coming") and "#gta6" in cap, cap
    assert agent.tick(force=True).startswith("Made 2 clips")
    assert len([m for m in agent.mine() if "🎥 @creator on YouTube" in m["caption"]]) == 2
    assert {m["account_slot"] for m in agent.mine()} == {1}, "only account 1 has a platform connected"

    # --- connecting TikTok and YouTube: the platform's own sign-in, state and PKCE checked ---
    url = connect.start("tiktok", 1)
    q = parse_qs(urlsplit(url).query)
    state = q["state"][0]
    ver = connect._pending[state]["verifier"]
    assert q["code_challenge"][0] == hashlib.sha256(ver.encode()).hexdigest() and q["redirect_uri"][0].startswith("http://127.0.0.1:8771/")
    must_fail(connect.finish, "tiktok", {"state": "forged", "code": "tt-code"})
    assert "@gta6daily" in connect.finish("tiktok", {"state": state, "code": "tt-code"})
    must_fail(connect.finish, "tiktok", {"state": state, "code": "tt-code"})        # a state works once
    url = connect.start("youtube", 1)
    q = parse_qs(urlsplit(url).query)
    ver = connect._pending[q["state"][0]]["verifier"]
    assert q["code_challenge"][0] == base64.urlsafe_b64encode(hashlib.sha256(ver.encode()).digest()).rstrip(b"=").decode()
    assert q["access_type"][0] == "offline"
    assert "GTA 6 Daily" in connect.finish("youtube", {"state": q["state"][0], "code": "yt-code"})
    must_fail(connect.start, "instagram", 1)                                    # no Instagram app keys in .env
    assert agent.targets(1) == ["instagram", "tiktok", "youtube"] and agent.targets(2) == []

    # --- approving and posting to all three, one per call, inside the window ---
    cid = mine[0]["id"]
    noon = dt.datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    assert agent.post_due(noon) == "nothing to post"
    assert agent.decide(cid, "approve", "new words #gta6 #vicecity") == "queued"
    assert agent.post_due(noon.replace(hour=23)) == "nothing to post", "outside posting hours"
    assert "Instagram" in agent.post_due(noon) and f["posted"] == [cid]
    assert "TikTok inbox" in agent.post_due(noon)
    web.yt_fail = 3
    for i in range(3):                                   # quota out three times: it gives up and says so
        assert "YouTube post failed" in agent.post_due(noon + dt.timedelta(minutes=20 * i)), i
    done = agent.meta_of(cid)
    assert done["posts"]["youtube"]["final"] and done["status"] == "posted", done
    assert any("Gave up posting" in s for s in f["said"])
    agent.retry(cid, "youtube")
    assert agent.meta_of(cid)["status"] == "queued"
    assert "Posted" in agent.post_due(noon + dt.timedelta(hours=2))
    done = agent.meta_of(cid)
    assert done["posts"]["youtube"]["url"] == "https://www.youtube.com/shorts/vid123" and done["status"] == "posted"
    assert done["post_url"] == "https://www.instagram.com/reel/x/" and done["caption"] == "new words #gta6 #vicecity"
    assert connect._entry("tiktok", 1)["access"] == "TT-ACCESS-1"
    e = connect._load()
    e["tiktok"]["1"]["expires"] = 0
    connect._save(e)
    assert connect.access("tiktok", 1) == "TT-ACCESS-2" and connect._entry("tiktok", 1)["refresh"] == "TT-REFRESH-2"

    # spacing and daily cap on one account and platform
    a, b = [x["id"] for x in agent.mine() if x["status"] == "pending"][:2]
    agent.toggle(1, "tiktok", False)
    agent.toggle(1, "youtube", False)
    agent.decide(a, "approve")
    agent.decide(b, "approve")
    t0 = noon + dt.timedelta(hours=3)
    assert "Instagram" in agent.post_due(t0)
    assert agent.post_due(t0 + dt.timedelta(minutes=10)) == "nothing to post", "45 minutes apart"
    assert "Instagram" in agent.post_due(t0 + dt.timedelta(minutes=50))
    agent.POST_PER_DAY = 3
    for x in agent.mine():
        if x["status"] == "pending":
            agent.decide(x["id"], "approve")
    assert agent.post_due(t0 + dt.timedelta(minutes=100)) == "nothing to post", "3 a day reached"
    agent.POST_PER_DAY = 4
    for bad_id, decision in ((cid, "approve"), ("../../x/y", "approve"), (a, "posted")):
        must_fail(agent.decide, bad_id, decision)
    foreign = f["out"] / "other-job"
    foreign.mkdir()
    (foreign / "clip_01.json").write_text(json.dumps({"id": "other-job/clip_01", "status": "pending"}))
    must_fail(agent.decide, "other-job/clip_01", "approve")

    # TikTok direct posting once the app is approved, and the chunk rule
    assert post.chunks(5000) == (5000, 1) and post.chunks(200 * post.MB) == (10 * post.MB, 20)
    os.environ["TIKTOK_AUDITED"] = "1"
    assert post.public_posting()
    os.environ["TIKTOK_AUDITED"] = ""

    # --- campaigns: found by itself, searched by keyword, used, clipped with the brand's rules ---
    found, new = campaigns.auto(force=True)
    assert [c["id"] for c in found] == ["c-gta"] and found[0]["gta6"] and [c["id"] for c in new] == ["c-gta"]
    assert found[0]["hashtags"] == ["#tokyogirl"] and found[0]["mentions"] == ["@placements"], found[0]
    assert campaigns.auto(force=True)[1] == [], "already seen: not new again"
    assert [c["id"] for c in campaigns.search("fortnite")] == ["c-fn"]
    must_fail(campaigns.search, "x")
    agent.campaigns_check(force=True)
    agent.use_campaign("c-gta", str(caps))
    st = agent.load()
    assert "c-gta" in st["campaigns"] and any(c.get("campaign_id") == "c-gta" for c in st["channels"])
    assert st["links"][0]["url"].startswith("https://drive.google.com")
    must_fail(agent.add_folder, str(root), "no-such-campaign")
    agent.change(lambda s: s.update(folders=[]))
    assert agent.pick_slot() is None, "account 1 already has its 4 clips today"
    agent.PER_ACCOUNT_DAY = 99
    got = agent.tick(force=True)
    assert "Download that footage" in got, (got, agent.load()["links"], agent.pick_slot())
    assert agent.tick(force=True).startswith("Made 2 clips")
    camp = [m for m in agent.mine() if m.get("campaign_id") == "c-gta"]
    assert len(camp) == 2 and all("#ad" in m["caption"] and "#tokyogirl" in m["caption"] and "@placements" in m["caption"]
                                  for m in camp), camp[0]["caption"]
    m = dict(camp[0], campaign_platforms=["instagram", "tiktok"])
    assert agent._submit(m, "youtube", "https://youtube.com/shorts/1") == "", "YouTube isn't one of its platforms"
    assert "contentrewards.com/c/campaigns/c-gta" in agent._submit(m, "instagram", "https://instagram.com/reel/1")
    agent.drop_campaign("c-gta")
    assert not agent.load()["campaigns"] and not any(c.get("campaign_id") for c in agent.load()["channels"])

    # --- Telegram through its own bot: linking, strangers ignored, approve and caption by tap ---
    os.environ["GTA6_TELEGRAM_TOKEN"] = "host-bot"
    assert not tg.own(), "a host app's token can't be reused"
    os.environ["GTA6_TELEGRAM_TOKEN"] = "own-bot"
    info = tg.link()
    assert info["url"] == f"https://t.me/dd_clips_bot?start={info['code']}"
    tg.handle({"message": {"chat": {"id": 666}, "text": "/start wrong", "from": {"first_name": "X"}}})
    assert not tg.linked()
    tg.handle({"message": {"chat": {"id": 42}, "text": f"/start {info['code']}", "from": {"first_name": "Damon"}}})
    assert tg.linked() and tg.status()["name"] == "Damon"
    pending = [m for m in agent.mine() if m["status"] == "pending"]
    for m in pending:
        agent.decide(m["id"], "reject")
    caps2 = root / "caps2"
    caps2.mkdir()
    (caps2 / "heist.mp4").write_bytes(b"x" * 20)
    os.utime(caps2 / "heist.mp4", (old, old))
    agent.add_folder(str(caps2))
    assert "sent to Telegram" in agent.tick(force=True), agent.status
    assert any(m == "sendVideo" for m, _ in web.sent)
    fresh = [m for m in agent.mine() if m["status"] == "pending"][0]
    refs = {v["clip"]: k for k, v in tg._load()["msgs"].items()}
    ref = refs[fresh["id"]]
    tg.handle({"callback_query": {"id": "q0", "data": f"g6|ok|{ref}", "message": {"chat": {"id": 666}}}})
    assert agent.meta_of(fresh["id"])["status"] == "pending", "a stranger's tap changes nothing"
    tg.handle({"callback_query": {"id": "q1", "data": f"g6|cap|{ref}", "message": {"chat": {"id": 42}}}})
    tg.handle({"message": {"chat": {"id": 42}, "text": "Better words #gta6"}})
    assert agent.meta_of(fresh["id"])["caption"] == "Better words #gta6"
    tg.handle({"callback_query": {"id": "q2", "data": f"g6|ok|{ref}", "message": {"chat": {"id": 42}}}})
    assert agent.meta_of(fresh["id"])["status"] == "queued"
    tg.handle({"callback_query": {"id": "q3", "data": f"g6|no|{ref}", "message": {"chat": {"id": 42}}}})
    assert agent.meta_of(fresh["id"])["status"] == "queued", "already sorted"
    other = [m for m in agent.mine() if m["status"] == "pending"][0]
    agent.decide(other["id"], "reject")             # decided in the window: its Telegram buttons come off
    assert any(m == "editMessageReplyMarkup" for m, _ in web.sent)

    # --- keys pasted into the window land in .env, one line each, nothing else touched ---
    env_f = root / ".env"
    env_f.write_text("\ufeffOPENAI_KEY=keep-me\nIG_APP_ID=old\n# note\nIG_APP_ID=older\n", encoding="utf-8")
    ks = connect.save_keys("instagram", " 1234567890 ", "s3cr3t_value")
    assert ks["instagram"]["ready"] and os.environ["IG_APP_SECRET"] == "s3cr3t_value"
    assert env_f.read_text(encoding="utf-8") == "OPENAI_KEY=keep-me\nIG_APP_ID=1234567890\n# note\nIG_APP_SECRET=s3cr3t_value\n", env_f.read_text()
    must_fail(connect.save_env, {"OPENAI_KEY": "steal-it"})
    must_fail(connect.save_keys, "youtube", "x\nEVIL=1", "y" * 10)
    assert "EVIL" not in env_f.read_text()
    os.environ["GTA6_TELEGRAM_TOKEN"] = ""
    must_fail(tg.save_token, "host-bot")
    must_fail(tg.save_token, "not a token")
    assert tg.save_token("999:own-bot-2") == "dd_clips_bot" and os.environ["GTA6_TELEGRAM_TOKEN"] == "999:own-bot-2"
    assert "GTA6_TELEGRAM_TOKEN=999:own-bot-2" in env_f.read_text() and not tg.linked(), "a new bot starts unlinked"
    os.environ["GTA6_TELEGRAM_TOKEN"] = "own-bot"

    # --- the window: this laptop only, tokens never shown, videos seekable ---
    snap = json.dumps(app.snapshot())
    for secret in ("TT-ACCESS", "TT-REFRESH", "YT-ACCESS", "YT-REFRESH", "own-bot", '"ts"', '"ys"', "s3cr3t", "1234567890"):
        assert secret not in snap, secret
    server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def call(method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request(method, path, body=body, headers={"Host": f"127.0.0.1:{app.PORT}", **(headers or {})})
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read()

    assert call("GET", "/api/state", {"Host": "evil.example"})[0] == 403
    assert call("POST", "/api/on", body=b"{}")[0] == 403, "no X-Agent header"
    assert call("GET", "/api/state")[0] == 200
    s, h, b = call("GET", "/media/" + cid.replace("/", "%2F"), {"Range": "bytes=100-199"})
    assert s == 206 and len(b) == 100 and h["Content-Range"] == "bytes 100-199/5000", (s, h)
    assert call("GET", "/media/..%2F..%2Fetc%2Fpasswd")[0] == 404
    assert call("GET", "/fonts/../../config.py")[0] == 404
    s, h, page = call("GET", "/oauth/tiktok/?state=forged&code=x")
    assert s == 200 and b"wasn&#x27;t started from this app" in page and "wasn't started" in agent.status["note"], page[:300]
    s, _, b = call("POST", "/api/keys/save", {"X-Agent": "gta6", "Content-Type": "application/json"},
                   json.dumps({"platform": "instagram", "id": "1234567", "secret": "abc def"}).encode())
    assert s == 400 and b"with no spaces" in b
    s, _, b = call("POST", "/api/campaigns/search", {"X-Agent": "gta6", "Content-Type": "application/json"},
                   json.dumps({"q": "gta"}).encode())
    assert s == 200 and json.loads(b)["result"] == 1
    server.shutdown()
    print("selftest ok: research, sources, campaigns (auto + search + rules), sign-in for TikTok and YouTube, "
          "posting to Instagram, TikTok and YouTube with retries, spacing and caps, Telegram approvals, keys saved to .env, window security")
