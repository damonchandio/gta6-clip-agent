"""The agent: sources, campaigns, research, the clip loop and the poster. The window (app.py) only calls into this."""
from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import math
import random
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import config

HOME = config.DATA_DIR / "gta6"
FILE = HOME / "state.json"
RESEARCH = HOME / "research.json"
log = logging.getLogger("gta6")

GTA = re.compile(r"\bgta\s*(6|vi)\b|grand theft auto\s*(6|vi)\b|\bleonida\b", re.I)
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".m4v")
PLATFORMS = ("instagram", "tiktok", "youtube")
CHECK_EVERY = 20 * 60
HOURS = (8, 22)               # when new clips are made
POST_HOURS = (11, 22)         # when approved clips go out (UK time)
POST_GAP_MIN = 45             # between two posts on the same account and platform
POST_PER_DAY = 10             # per account, per platform
RETRY_AFTER_MIN = 15
MAX_TRIES = 3
BATCH = 3                     # clips made from one video
MAX_WAITING = 50              # stop making more while this many are waiting for a yes or no
PER_ACCOUNT_DAY = 10          # clips made per account per day
MIN_OWN_SECONDS = 20
MIN_CHANNEL_SECONDS = 240     # skip Shorts and trailers: long videos give real moments to cut
LIVE_FIRST = (10, 4)          # a stream's first stretch: minutes recorded, clips made (short, so clips arrive early)
LIVE_NEXT = (30, 6)           # every stretch after that
_live_seen: set = set()       # streams that have had their first stretch
RESEARCH_EVERY = 24 * 3600
SEEDS = ("gta 6", "gta 6 how to", "gta 6 best", "gta 6 funny", "gta 6 secret", "gta 6 vs", "gta vi")
BAD_TAGS = re.compile(r"tiktok|fyp|foryou|for_you|viral|reels?$|shorts|youtube|trending|explore|subscribe", re.I)

_state_lock = threading.Lock()
_meta_lock = threading.Lock()
_busy = threading.Lock()
_posting = threading.Lock()
status = {"doing": "", "note": "Starting up.", "posting": ""}
events: collections.deque = collections.deque(maxlen=120)


def note(text: str) -> str:
    status["note"] = text
    if not events or events[0]["text"] != text:
        events.appendleft({"t": dt.datetime.now().strftime("%d %b %H:%M"), "text": text})
        log.info(text)
    return text


# --- state ------------------------------------------------------------------------------------------
def load() -> dict:
    st = {"on": True, "folders": [], "channels": [], "links": [], "campaigns": {}, "off": [], "used": [],
          "last_check": 0, "ig_refreshed": 0}
    try:
        st.update(json.loads(FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    st["folders"] = [f if isinstance(f, dict) else {"path": f} for f in st["folders"]]   # v1 kept bare paths
    return st


def save(st: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    st["used"] = st["used"][-3000:]
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
    tmp.replace(FILE)


def change(fn) -> dict:
    with _state_lock:
        st = load()
        fn(st)
        save(st)
        return st


def _campaign(st: dict, cid: str | None) -> dict | None:
    return (st.get("campaigns") or {}).get(cid) if cid else None


def add_folder(path: str, campaign_id: str = "") -> str:
    p = Path(path.strip().strip('"'))
    if not p.is_dir():
        raise ValueError("That folder doesn't exist on this laptop.")

    def fn(st):
        if campaign_id and campaign_id not in st["campaigns"]:
            raise ValueError("Use that campaign first (Campaigns tab).")
        same = next((f for f in st["folders"] if f["path"] == str(p)), None)
        if same and (not campaign_id or same.get("campaign_id") == campaign_id):
            raise ValueError("That folder is already a source.")
        if same:                               # an existing folder now feeds this campaign
            same["campaign_id"] = campaign_id
        else:
            st["folders"].append({"path": str(p), "campaign_id": campaign_id or ""})
    change(fn)
    return str(p)


def add_channel(url: str, credit: str, permitted: bool, campaign_id: str = "") -> None:
    url = url.strip()
    if not re.match(r"https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", url, re.I):
        raise ValueError("Paste a YouTube channel, playlist or video link.")
    if not permitted and not campaign_id:
        raise ValueError("Tick the box first: I only clip creators who allow it, or campaigns you've joined.")

    def fn(st):
        if campaign_id and campaign_id not in st["campaigns"]:
            raise ValueError("Use that campaign first (Campaigns tab).")
        if any(c["url"] == url for c in st["channels"]):
            raise ValueError("That channel is already a source.")
        st["channels"].append({"url": url, "credit": credit.strip()[:60], "permitted": True, "last": 0,
                               "campaign_id": campaign_id or ""})
    change(fn)


def use_campaign(cid: str, folder: str = "") -> str:
    """You have joined this campaign on the site: clip it. Its YouTube footage becomes a source, other footage
    links are tried once each, and a capture folder can be tied to it for footage he downloads himself."""
    from . import campaigns
    c = campaigns.get(cid)
    if not c:
        raise ValueError("I don't have that campaign any more. Search again.")
    rules = campaigns.rules(c)
    yt = [u for u in c.get("footage") or [] if re.match(r"https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", u, re.I)]
    other = [u for u in c.get("footage") or [] if u not in yt]

    def fn(st):
        st["campaigns"][cid] = rules
        for u in yt:
            if not any(ch["url"] == u for ch in st["channels"]):
                st["channels"].append({"url": u, "credit": "", "permitted": True, "last": 0, "campaign_id": cid})
        for u in other:
            if not any(lk["url"] == u for lk in st["links"]):
                st["links"].append({"url": u, "campaign_id": cid, "tried": False})
    change(fn)
    if folder.strip():
        add_folder(folder, cid)
    found = len(yt) + len(other)
    return note(f"Clipping '{c['name']}' now: " + (f"{found} footage links added." if found else
                "it lists no footage links, so add a folder or channel for it under Sources."))


def drop_campaign(cid: str) -> None:
    def fn(st):
        st["campaigns"].pop(cid, None)
        st["channels"] = [c for c in st["channels"] if c.get("campaign_id") != cid]
        st["links"] = [lk for lk in st["links"] if lk.get("campaign_id") != cid]
        for f in st["folders"]:
            if f.get("campaign_id") == cid:
                f["campaign_id"] = ""
    change(fn)


def remove_source(kind: str, i: int) -> None:
    key = {"folder": "folders", "channel": "channels", "link": "links"}[kind]
    change(lambda st: st[key].pop(i))


def toggle(slot: int, platform: str, on: bool) -> None:
    key = f"{int(slot)}:{platform}"
    if platform not in PLATFORMS:
        raise ValueError("unknown platform")

    def fn(st):
        st["off"] = [k for k in st["off"] if k != key] + ([] if on else [key])
    change(fn)


# --- research: what people search for, and the hashtags the top videos carry --------------------------
def suggestions(seed: str) -> list[str]:
    url = "https://suggestqueries.google.com/complete/search?client=firefox&ds=yt&q=" + urllib.parse.quote(seed)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return [s for s in json.loads(r.read().decode("utf-8", "replace"))[1] if isinstance(s, str)]


def top_videos() -> list[dict]:
    import yt_dlp
    opts = {"quiet": True, "extract_flat": True, "skip_download": True, "noprogress": True}
    with yt_dlp.YoutubeDL(opts) as y:
        return [e for e in (y.extract_info("ytsearch40:gta 6 shorts", download=False).get("entries") or []) if e]


def rank_keywords(by_seed: dict[str, list[str]]) -> list[dict]:
    """A phrase scores higher the nearer the top it is suggested, and again for every seed that suggests it."""
    score: dict[str, float] = collections.defaultdict(float)
    for seed, found in by_seed.items():
        for pos, phrase in enumerate(found[:10]):
            phrase = phrase.strip().lower()
            if phrase and phrase != seed and GTA.search(phrase):
                score[phrase] += 10 - pos
    return [{"phrase": p, "score": round(s, 1)} for p, s in sorted(score.items(), key=lambda kv: -kv[1])][:30]


def rank_hashtags(videos: list[dict]) -> list[dict]:
    """Hashtags in the titles and descriptions of the top videos, weighted by how many views each video has."""
    score: dict[str, float] = collections.defaultdict(float)
    seen: dict[str, int] = collections.defaultdict(int)
    for v in videos:
        text = f"{v.get('title') or ''} {v.get('description') or ''}".lower()
        for tag in set(re.findall(r"#(\w{3,30})", text)):
            if not BAD_TAGS.search(tag) and not tag.isdigit():
                score[tag] += math.log10((v.get("view_count") or 0) + 10)
                seen[tag] += 1
    return [{"tag": t, "score": round(s, 1), "videos": seen[t]}
            for t, s in sorted(score.items(), key=lambda kv: -kv[1])][:30]


def research(force: bool = False) -> dict:
    try:
        old = json.loads(RESEARCH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {"at": 0, "keywords": [], "hashtags": [], "errors": []}
    if not force and time.time() - old.get("at", 0) < RESEARCH_EVERY:
        return old
    new = {"at": time.time(), "keywords": old["keywords"], "hashtags": old["hashtags"], "errors": []}
    try:
        new["keywords"] = rank_keywords({s: suggestions(s) for s in SEEDS}) or old["keywords"]
    except Exception as e:  # noqa: BLE001  research must never stop the clipping: keep yesterday's list
        new["errors"].append(f"keywords: {str(e)[:120]}")
    try:
        new["hashtags"] = rank_hashtags(top_videos()) or old["hashtags"]
    except Exception as e:  # noqa: BLE001
        new["errors"].append(f"hashtags: {str(e)[:120]}")
    HOME.mkdir(parents=True, exist_ok=True)
    RESEARCH.write_text(json.dumps(new, indent=1), encoding="utf-8")
    note("Research refreshed: " + ("; ".join(new["errors"]) if new["errors"] else
                                   f"{len(new['keywords'])} search phrases, {len(new['hashtags'])} hashtags."))
    return new


def pick_tags(r: dict, n: int, seed: str) -> list[str]:
    """#gta6 always, then a different mix of the strongest researched tags for each clip (same clip, same tags)."""
    pool = [h for h in r.get("hashtags") or [] if h["tag"] != "gta6"][:15]
    rng, out = random.Random(seed), ["gta6"]
    while pool and len(out) < n:
        h = rng.choices(pool, weights=[max(x["score"], 0.1) for x in pool])[0]
        pool.remove(h)
        out.append(h["tag"])
    return out


# --- clips this agent made ----------------------------------------------------------------------------
def mine() -> list[dict]:
    from clipper.desk import clips
    return [c for c in clips() if c.get("agent") == "gta6"]


def _path(clip_id: str):
    from clipper.desk import _meta_path
    p = _meta_path(clip_id)                      # refuses ids that point outside the clips folder
    if not p.exists():
        raise ValueError("no such clip")
    return p


def meta_of(clip_id: str) -> dict:
    m = json.loads(_path(clip_id).read_text(encoding="utf-8"))
    if m.get("agent") != "gta6":
        raise ValueError("not one of my clips")
    return m


def _edit(clip_id: str, fn) -> dict:
    with _meta_lock:
        p = _path(clip_id)
        m = json.loads(p.read_text(encoding="utf-8"))
        if m.get("agent") != "gta6":
            raise ValueError("not one of my clips")
        fn(m)
        p.write_text(json.dumps(m, indent=1), encoding="utf-8")
        return m


def retag(job: str, src: dict, r: dict) -> list[dict]:
    """Mark the job's clips as this agent's and write the final caption: text, credit line, researched hashtags.
    Campaign clips keep the caption the engine built from the campaign's own rules (#ad, required tags)."""
    from clipper import OUT
    out = []
    for f in sorted((OUT / job).glob("clip_*.json")):
        m = json.loads(f.read_text(encoding="utf-8"))
        m.update(agent="gta6", source_kind=src["kind"], source_title=src.get("title", ""))
        camp = src.get("campaign")
        if camp:
            m.update(campaign=camp["title"], campaign_id=camp["campaign_id"], campaign_url=camp.get("url", ""))
            cap = m.get("caption") or ""
            for t in ["#ad"] + camp.get("required_hashtags", []) + camp.get("required_mentions", []):
                if t.lower() not in cap.lower():          # the brand's must-haves, whatever the engine wrote
                    cap = f"{cap} {t}".strip()
            m["caption"] = cap
        else:
            body = re.sub(r"\s*#\w+", "", m.get("caption") or "").strip()      # drops the engine's default #ad too
            if src.get("credit"):
                body += f"\n🎥 {src['credit']}"
            m["caption"] = f"{body}\n{' '.join('#' + t for t in pick_tags(r, 5, m['id']))}".strip()
        f.write_text(json.dumps(m, indent=1), encoding="utf-8")
        out.append(m)
    return out


def decide(clip_id: str, decision: str, caption: str | None = None, source: str = "app") -> str:
    if decision not in ("approve", "reject"):
        raise ValueError("approve or reject only")

    def fn(m):
        if m.get("status") not in ("pending", "approved", "queued", "rejected"):
            raise ValueError(f"that clip is already {m.get('status')}")
        if caption is not None:
            m["caption"] = caption.strip()[:2200]
        if decision == "approve":
            m.update(status="queued", approved_at=dt.datetime.now().isoformat(timespec="seconds"))
        else:
            m["status"] = "rejected"
    m = _edit(clip_id, fn)
    if source != "telegram":
        try:
            from . import tg
            tg.resolved(clip_id, m["status"])
        except Exception:  # noqa: BLE001
            pass
    return m["status"]


def set_caption(clip_id: str, caption: str) -> dict:
    caption = caption.strip()[:2200]
    if not caption:
        raise ValueError("an empty caption won't post")

    def fn(m):
        if m.get("status") not in ("pending", "queued", "approved"):
            raise ValueError("it has already gone out")
        m["caption"] = caption
    return _edit(clip_id, fn)


def retry(clip_id: str, platform: str) -> None:
    def fn(m):
        (m.get("posts") or {}).pop(platform, None)
        if m.get("status") in ("posted", "failed"):
            m["status"] = "queued"
    _edit(clip_id, fn)


# --- where clips go -------------------------------------------------------------------------------------
def targets(slot, st: dict | None = None) -> list[str]:
    from . import connect
    if not slot:
        return []
    st = st or load()
    return [p for p in PLATFORMS if f"{slot}:{p}" not in st["off"] and connect.connected(p, int(slot))]


def where_label(meta: dict) -> str:
    from . import connect
    slot = meta.get("account_slot")
    plats = targets(slot)
    if not plats:
        return f"account {slot} (no platform connected yet)"
    return ", ".join(f"{connect.NAMES[p]} {connect.name(p, int(slot))}" for p in plats)


def slots() -> list[int]:
    from clipper import accounts
    return [a["slot"] for a in accounts.load()]


def pick_slot(today: str | None = None) -> int | None:
    """The connected account with the fewest clips made today, or None when every account has its share."""
    today = today or dt.date.today().strftime("%Y%m%d")
    made = collections.Counter(c.get("account_slot") for c in mine()
                               if c.get("job", "")[-13:-5] == today and c.get("status") != "rejected")
    st = load()
    every = slots()
    ready = [s for s in every if targets(s, st)] or every      # nothing connected yet: still make clips
    free = [s for s in ready if made[s] < PER_ACCOUNT_DAY]
    return min(free, key=lambda s: made[s]) if free else None


def notify_clip(m: dict) -> bool:
    from . import tg
    if tg.own():
        from clipper import OUT
        return tg.send_clip(m, OUT / m["job"] / m["file"], where_label(m))
    from clipper import phoneflow                # no bot of its own: a host app's bot may carry it
    return bool(phoneflow.send_clip(m))


def say(text: str) -> None:
    from . import tg
    if tg.own():
        tg.say(text)
        return
    try:
        import approvals
        approvals.say(text)
    except Exception:  # noqa: BLE001
        pass


# --- sources ------------------------------------------------------------------------------------------
def live_now(link: str) -> dict | None:
    """What a channel (or a stream link) is broadcasting right now, or None when it isn't live."""
    link = link.split("?si=")[0].rstrip("/")
    if re.search(r"youtube\.com/(@[^/?]+|channel/[^/?]+|c/[^/?]+|user/[^/?]+)$", link, re.I):
        link += "/live"
    elif not re.search(r"watch\?|/live/|youtu\.be/", link, re.I):
        return None                                               # a playlist can't be live
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "noprogress": True, "noplaylist": True,
                               "format": "b[height<=1080]/bv*[height<=1080]+ba/b/bv*+ba"}) as y:
            info = y.extract_info(link, download=False)
    except Exception as e:  # noqa: BLE001  a channel that isn't live has nothing behind /live
        log.info("live check on %s: %s", link, str(e).splitlines()[0][:120])
        return None
    return info if info.get("is_live") and _streams(info) else None


def _streams(info: dict) -> list[str]:
    """One link when YouTube offers picture and sound together, otherwise one for each."""
    return [info["url"]] if info.get("url") else [f["url"] for f in info.get("requested_formats") or [] if f.get("url")]


def record_live(info: dict, minutes: float) -> Path:
    """Save the next few minutes of a live stream as a normal video file."""
    from clipper import engine
    out = HOME / "live" / f"{info['id']}-{int(time.time())}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    agent_ua = (info.get("http_headers") or {}).get("User-Agent") or "Mozilla/5.0"
    try:
        inputs = [a for u in _streams(info) for a in ("-user_agent", agent_ua, "-i", u)]
        subprocess.run([engine.ffmpeg(), "-y", "-loglevel", "error", *inputs,
                        "-t", str(minutes * 60), "-c", "copy", str(out)], capture_output=True,
                       timeout=minutes * 60 + 180)
    except subprocess.TimeoutExpired:
        pass                                                      # keep whatever was saved
    if not out.exists() or engine.duration(out) < MIN_OWN_SECONDS:
        out.unlink(missing_ok=True)
        raise RuntimeError("the stream gave no video")
    return out


def next_source(st: dict) -> dict | None:
    from clipper import autopilot, engine
    used = set(st["used"])
    live_ids = set()                                              # never downloaded as a video: that only ends when the stream does
    for ch in st["channels"]:                                     # a live stream goes first: it's gone once it ends
        camp = _campaign(st, ch.get("campaign_id")) or ch.get("campaign")
        info = live_now(ch["url"]) if ch.get("permitted") or camp else None
        if info:
            live_ids.add(info.get("id"))
        if not info or not (camp or GTA.search(info.get("title") or "")):
            continue
        minutes, count = LIVE_NEXT if info.get("id") in _live_seen else LIVE_FIRST
        status["doing"] = f"Recording {minutes} minutes of the live stream '{(info.get('title') or '')[:40]}'"
        try:
            note(f"'{(info.get('title') or 'A stream')[:50]}' is live. Recording {minutes} minutes of it.")
            path = record_live(info, minutes)
            _live_seen.add(info.get("id"))
        except Exception as e:  # noqa: BLE001
            note(f"Couldn't record the live stream on {ch['url']}: {str(e).splitlines()[0][:100]}")
            continue
        return {"kind": "campaign" if camp else "creator", "id": "live:" + path.name, "url": str(path), "live": True,
                "count": count,
                "title": info.get("title") or "Live stream", "credit": ch.get("credit", ""), "campaign": camp}
    for folder in st["folders"]:                                  # your own captures always go first
        try:
            files = sorted((p for p in Path(folder["path"]).rglob("*") if p.suffix.lower() in VIDEO_EXT),
                           key=lambda p: p.stat().st_mtime)
        except OSError:
            continue
        for p in files:
            key = f"{p.name}:{p.stat().st_size}"
            if key in used or time.time() - p.stat().st_mtime < 120:   # still being copied
                continue
            if engine.duration(p) >= MIN_OWN_SECONDS:
                camp = _campaign(st, folder.get("campaign_id"))
                return {"kind": "campaign" if camp else "own", "id": key, "url": str(p), "title": p.stem,
                        "campaign": camp}
            used.add(key)
            st["used"].append(key)                                # too short to cut: don't measure it again
    for lk in st["links"]:                                        # a campaign's own footage links, once each
        if not lk.get("tried") and _campaign(st, lk.get("campaign_id")):
            lk["tried"] = True
            return {"kind": "campaign", "id": "link:" + lk["url"], "url": lk["url"],
                    "title": urllib.parse.urlparse(lk["url"]).netloc, "campaign": _campaign(st, lk["campaign_id"])}
    for ch in sorted(st["channels"], key=lambda c: c.get("last", 0)):      # the channel waiting longest goes next
        camp = _campaign(st, ch.get("campaign_id")) or ch.get("campaign")
        if not ch.get("permitted") and not camp:
            continue
        ch["last"] = time.time()
        try:
            found = autopilot.candidates(ch["url"], 30)
        except Exception as e:  # noqa: BLE001  one dead link must not stop the others
            note(f"Couldn't read {ch['url']}: {str(e).splitlines()[0][:100]}")
            continue
        for v in found:
            long_enough = (v.get("seconds") or 0) >= MIN_CHANNEL_SECONDS or v.get("linked")
            if v["id"] and v["id"] not in used and v["id"] not in live_ids and long_enough and (camp or GTA.search(v["title"])):
                return {"kind": "campaign" if camp else "creator", "id": v["id"], "url": v["url"],
                        "title": v["title"], "credit": ch.get("credit", ""), "campaign": camp}
    return None


def rules_for(src: dict, slot: int, r: dict) -> dict:
    kw = ", ".join(k["phrase"] for k in (r.get("keywords") or [])[:8])
    rules = {"title": "GTA 6", "brand": "GTA 6", "slug": "gta6", "account_slot": slot, "min_seconds": 12,
             "max_seconds": 40,
             "content_notes": "GTA 6 clips for fans of the game." + (f" People search for: {kw}. Use one of those "
                              "phrases in the hook only when it truly matches the moment." if kw else ""),
             "caption_rules": "Never claim a leak, a cheat or anything the clip doesn't show."}
    camp = src.get("campaign") or {}
    rules.update({k: v for k, v in camp.items() if k not in ("url", "rate")})
    if camp.get("content_notes"):
        rules["content_notes"] = camp["content_notes"] + " " + rules.get("caption_rules", "")
    return rules


# --- making clips -------------------------------------------------------------------------------------
def tick(force: bool = False) -> str:
    if not _busy.acquire(blocking=False):
        return "busy"
    try:
        st = load()
        now = dt.datetime.now()
        if not force:
            if not st["on"]:
                return "off"
            if not (HOURS[0] <= now.hour < HOURS[1]) or time.time() - st["last_check"] < CHECK_EVERY:
                return "not now"
        change(lambda s: s.update(last_check=time.time()))
        status["doing"] = "Researching keywords and hashtags"
        r = research()
        if sum(c.get("status") == "pending" for c in mine()) >= MAX_WAITING:
            return note(f"{MAX_WAITING} clips are waiting for your yes or no. I'll make more once you've been through them.")
        slot = pick_slot()
        if slot is None:
            return note("Every account has its clips for today.")
        status["doing"] = "Looking for footage"
        src = next_source(st)
        if src:
            st["used"].append(src["id"])       # before the job, so a video that crashes it is never retried forever

        def merge(s):                          # only what the search touched: the window may have edited the rest
            s["used"] = list(dict.fromkeys(s["used"] + st["used"]))
            last = {c["url"]: c.get("last", 0) for c in st["channels"]}
            tried = {lk["url"] for lk in st["links"] if lk.get("tried")}
            for c in s["channels"]:
                c["last"] = max(c.get("last", 0), last.get(c["url"], 0))
            for lk in s["links"]:
                lk["tried"] = lk.get("tried") or lk["url"] in tried
        change(merge)
        if not src:
            return note("No new footage. Add a capture folder, a channel or a campaign, or drop a new video in.")
        from clipper import engine

        def progress(msg):
            status["doing"] = f"{src['title'][:50]}: {msg}"
        note(f"Cutting clips from '{src['title'][:60]}'" + (f" for {src['campaign']['title']}." if src.get("campaign") else "."))
        try:
            s = engine.make_clips(src["url"], rules_for(src, slot, r), src.get("count", BATCH), progress=progress)
        except Exception as e:  # noqa: BLE001
            if src["id"].startswith("link:"):
                return note(f"Couldn't download {src['url'][:80]}. Download that footage into a capture folder "
                            "tied to the campaign (Sources tab) and I'll clip it from there.")
            raise e
        made = retag(s["job"], src, r)
        if src.get("live"):
            Path(src["url"]).unlink(missing_ok=True)           # the recording is only raw material
            change(lambda s: s.update(last_check=0))           # still live? record the next stretch straight away
        sent = sum(bool(notify_clip(m)) for m in made)
        if sent:
            say(f"🎮 {sent} new GTA 6 clips from '{src['title'][:50]}'. Approve the keepers and I'll post them.")
        return note(f"Made {len(made)} clips from '{src['title'][:50]}'" +
                    (", sent to Telegram." if sent else ". Approve them here (Telegram isn't connected)."))
    except Exception as e:  # noqa: BLE001  the loop must survive one bad video
        log.exception("gta6 tick failed")
        return note(f"That didn't work: {str(e).splitlines()[0][:160]}")
    finally:
        status["doing"] = ""
        _busy.release()


# --- posting --------------------------------------------------------------------------------------------
def _done(post: dict | None) -> bool:
    return bool(post) and bool(post.get("url") or post.get("inbox") or post.get("note") or post.get("final"))


def _due(post: dict | None, now: float) -> bool:
    """Not tried yet, or failed earlier and its wait is over."""
    if not post:
        return True
    if _done(post):
        return False
    return now - post.get("t", 0) >= RETRY_AFTER_MIN * 60


def next_post(clips: list[dict], slot: int, platform: str, now: dt.datetime) -> dict | None:
    """The approved clip to post now on this account and platform, or None (window, daily limit, spacing)."""
    if not (POST_HOURS[0] <= now.hour < POST_HOURS[1]):
        return None
    times = sorted(p["at"] for c in clips if c.get("account_slot") == slot
                   for p in [(c.get("posts") or {}).get(platform) or {}] if p.get("at"))
    if sum(t[:10] == now.date().isoformat() for t in times) >= POST_PER_DAY:
        return None
    if times and (now - dt.datetime.fromisoformat(times[-1])).total_seconds() < POST_GAP_MIN * 60:
        return None
    ready = [c for c in clips if c.get("account_slot") == slot and c.get("status") == "queued"
             and _due((c.get("posts") or {}).get(platform), now.timestamp())]
    return min(ready, key=lambda c: c.get("approved_at") or "") if ready else None


def _submit(m: dict, platform: str, url: str) -> str:
    """Hand a live post to its campaign: automatically where the host app can, otherwise you get the link."""
    camp_platforms = [p.lower() for p in (m.get("campaign_platforms") or [])]
    if not m.get("campaign_id") or not url or (camp_platforms and platform not in camp_platforms):
        return ""
    page = m.get("campaign_url") or f"https://contentrewards.com/c/campaigns/{m['campaign_id']}"
    try:
        from clipper import whop
        if whop.can_submit():
            return whop.submit({"id": m["campaign_id"], "name": m.get("campaign") or ""}, url)
    except Exception as e:  # noqa: BLE001
        log.info("whop submit failed: %s", e)
    return f"Submit it to the campaign: {page}"


def post_due(now: dt.datetime | None = None) -> str:
    """Post at most one clip, on the first account and platform that has one due."""
    if not _posting.acquire(blocking=False):
        return "busy"
    try:
        from . import connect, post
        now = now or dt.datetime.now()
        st = load()
        for c in mine():                  # approved through a host app's bot, which marks it "approved"
            if c.get("status") == "approved":
                _edit(c["id"], lambda m: m.update(status="queued"))
        clips = mine()
        for slot in slots():
            plats = targets(slot, st)
            for platform in plats:
                clip = next_post(clips, slot, platform, now)
                if not clip:
                    continue
                m = meta_of(clip["id"])
                camp = _campaign(st, m.get("campaign_id"))
                if camp:
                    m["campaign_platforms"] = camp.get("platforms") or []
                status["posting"] = f"Posting '{m.get('title')}' to {connect.NAMES[platform]}"
                prev = (m.get("posts") or {}).get(platform) or {}
                try:
                    res = post.publish(platform, slot, m)
                    res.update(at=now.isoformat(timespec="seconds"), t=now.timestamp())
                except Exception as e:  # noqa: BLE001
                    tries = prev.get("tries", 0) + 1
                    res = {"error": str(e)[:200], "tries": tries, "t": now.timestamp(), "final": tries >= MAX_TRIES}
                    if res["final"]:
                        say(f"⚠️ Gave up posting '{m.get('title')}' to {connect.NAMES[platform]} "
                            f"({connect.name(platform, slot)}): {str(e)[:150]}. Press Retry in the app to try again.")
                    upd = _edit(m["id"], lambda mm, r=res: mm.setdefault("posts", {}).__setitem__(platform, r))
                    _finish(upd, slot, st)
                    return note(f"{connect.NAMES[platform]} post failed: {str(e)[:120]}")
                upd = _edit(m["id"], lambda mm, r=res: mm.setdefault("posts", {}).__setitem__(platform, r))
                _finish(upd, slot, st)
                who = f"{connect.NAMES[platform]} {connect.name(platform, slot)}"
                if res.get("inbox"):
                    say(f"📥 '{m.get('title')}' is in the TikTok inbox of {connect.name(platform, slot)}. "
                        "Open TikTok and tap Post to put it live.")
                    return note(f"Sent '{m.get('title')}' to the TikTok inbox of {connect.name(platform, slot)}.")
                extra = _submit(m, platform, res.get("url", ""))
                if extra:
                    _edit(m["id"], lambda mm, e=extra: mm.setdefault("submitted_" + platform, e))
                say(f"📤 Posted on {who}: {res.get('url') or res.get('note')}" + (f"\n{extra}" if extra else ""))
                return note(f"Posted '{m.get('title')}' on {who}.")
        return "nothing to post"
    finally:
        status["posting"] = ""
        _posting.release()


def _finish(m: dict, slot: int, st: dict) -> None:
    """A clip is done once every platform it was meant for has it live, in the inbox, or has given up."""
    plats = targets(slot, st)
    posts = m.get("posts") or {}
    if plats and all(_done(posts.get(p)) for p in plats):
        url = next((posts[p].get("url") for p in ("instagram", "tiktok", "youtube") if (posts.get(p) or {}).get("url")), "")
        live = any((posts.get(p) or {}).get(k) for p in plats for k in ("url", "inbox", "note"))
        _edit(m["id"], lambda mm: mm.update(status="posted" if live else "failed", post_url=url, posted_by="gta6",
                                           posted_at=dt.datetime.now().isoformat(timespec="seconds")))


# --- the loop -----------------------------------------------------------------------------------------
_checking = threading.Lock()


def campaigns_check(force: bool = False) -> None:
    from . import campaigns
    if not _checking.acquire(blocking=False):
        return
    try:
        found, new = campaigns.auto(force)
    except Exception as e:  # noqa: BLE001
        log.info("campaign check failed: %s", e)
        return
    finally:
        _checking.release()
    for c in new[:3]:
        say(f"🎯 New GTA campaign on Content Rewards: {c['name']} — ${c.get('rate') or 0:.2f} per 1K views, "
            f"${(c.get('remaining') or 0):,.0f} left. Join it on the site ({c['url']}), then press Use in the app.")
    if new:
        note(f"Found {len(new)} new GTA campaign(s). See the Campaigns tab.")


def run_forever() -> None:
    from clipper import autopilot
    from . import tg
    threading.Thread(target=tg.poll_forever, name="gta6-telegram", daemon=True).start()
    last_post = 0.0
    while True:
        try:
            if autopilot._load().get("on"):     # one clipper at a time: a host app's autopilot stays off while this runs
                autopilot.set_on(False)
                note("Turned the other clip autopilot off. This app does the clipping now.")
            threading.Thread(target=tick, name="gta6-tick", daemon=True).start()   # returns at once unless due
            threading.Thread(target=campaigns_check, name="gta6-campaigns", daemon=True).start()
            if time.time() - last_post > 120:
                last_post = time.time()
                threading.Thread(target=post_due, name="gta6-post", daemon=True).start()
            st = load()
            if time.time() - st.get("ig_refreshed", 0) > 86400:
                change(lambda s: s.update(ig_refreshed=time.time()))
                from clipper import instagram
                threading.Thread(target=instagram.refresh_tokens, name="gta6-ig-refresh", daemon=True).start()
        except Exception:  # noqa: BLE001
            log.exception("gta6 loop")
        time.sleep(30)
