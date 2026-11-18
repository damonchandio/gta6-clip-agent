"""Listing the videos behind a YouTube link (a video, a playlist or a channel's newest uploads)."""
import logging
import re

log = logging.getLogger("clipper")


def _load() -> dict:
    return {"on": False}           # no other clipper to pause when the agent runs on its own


def set_on(on: bool) -> None:
    pass


def _listing_url(link: str) -> str:
    """A channel link -> its uploads tab (not Shorts, not the home page)."""
    link = link.split("?si=")[0].rstrip("/")
    if re.search(r"youtube\.com/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)$", link, re.I):
        return link + "/videos"
    return link


def candidates(link: str, limit: int = 60) -> list[dict]:
    """Videos behind a YouTube link: a single video, a playlist, or a channel's newest uploads."""
    import yt_dlp
    opts = {"quiet": True, "extract_flat": "in_playlist", "playlistend": limit, "skip_download": True,
            "noprogress": True}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(_listing_url(link), download=False)
    entries = info.get("entries")
    if entries is None:  # a single video: that one, then the newest uploads on the same channel
        entries = [info]
        chan = info.get("channel_url") or info.get("uploader_url")
        cid = info.get("channel_id") or ""
        if cid.startswith("UC"):  # every channel's uploads playlist: UU + the rest of its id
            chan = f"https://www.youtube.com/playlist?list=UU{cid[2:]}"
        more = []
        if chan:
            try:
                with yt_dlp.YoutubeDL(opts) as y:
                    more = list(y.extract_info(_listing_url(chan), download=False).get("entries") or [])[:limit]
            except Exception as e:  # noqa: BLE001
                log.info("autopilot: couldn't list the channel %s: %s", chan, e)
        if not more and cid and (info.get("channel") or info.get("uploader")):
            # Plan B: search YouTube for the channel's name, keep only videos from that same channel
            try:
                with yt_dlp.YoutubeDL(opts) as y:
                    found = y.extract_info(f"ytsearch40:{info.get('channel') or info.get('uploader')}",
                                           download=False).get("entries") or []
                more = [e for e in found if e and e.get("channel_id") == cid]
            except Exception as e:  # noqa: BLE001
                log.info("autopilot: couldn't search for the channel's videos: %s", e)
        log.info("autopilot: %s -> %d more videos from its channel", info.get("id"), len(more))
        more = [dict(e, _expanded=True) for e in more if e and e.get("id") != info.get("id")]
        entries += more
    out = []
    for e in list(entries)[:limit]:
        if not e:
            continue
        vid = e.get("id") or ""
        url = e.get("webpage_url") or e.get("url") or ""
        if vid and not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={vid}"
        if "/shorts/" in url:
            continue
        out.append({"id": vid, "url": url, "title": e.get("title") or "", "seconds": e.get("duration"),
                    "linked": e is entries[0] and info.get("entries") is None, "expanded": bool(e.get("_expanded"))})
    return out


