"""Posting one approved clip to one platform. Each function returns a result dict for the clip's "posts" record:
{"url": link} when it is live, {"inbox": True} when TikTok put it in the account's inbox to finish in the app.
They raise PostError with a sentence a person can act on."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import connect, net

TT_API = connect.TT_API
MB = 1024 * 1024
STATUS_TRIES = 120          # TikTok: 10 minutes of checking every 5 seconds


class PostError(RuntimeError):
    def __init__(self, msg: str, code: str = ""):
        super().__init__(msg)
        self.code = code


def video_of(meta: dict) -> Path:
    from clipper import OUT
    p = OUT / meta["job"] / meta["file"]
    if not p.exists():
        raise PostError("the video file is gone from the clips folder")
    return p


def hashtags(caption: str) -> list[str]:
    return list(dict.fromkeys(t.lower() for t in re.findall(r"#(\w{2,40})", caption or "")))


# --- Instagram: the clipper's poster (official API; the video is hosted for a few minutes so Instagram can fetch it)
def instagram(meta: dict) -> dict:
    from clipper import instagram as ig
    try:
        return {"url": ig.publish(dict(meta)) or ""}
    except Exception as e:  # noqa: BLE001
        raise PostError(str(e)[:200]) from e


# --- YouTube Shorts: resumable upload, public, Gaming category ----------------------------------------
def youtube_title(meta: dict) -> str:
    first = (meta.get("caption") or "").strip().splitlines()[0] if (meta.get("caption") or "").strip() else ""
    t = re.sub(r"#\w+", "", first)
    t = re.sub(r"[<>]", "", t).strip(" -–|") or (meta.get("title") or "GTA 6 clip").strip()
    t = t[:88].rstrip()
    return f"{t} #Shorts"


def youtube(slot: int, meta: dict) -> dict:
    video = video_of(meta)
    tok = connect.access("youtube", slot)
    size = video.stat().st_size
    caption = meta.get("caption") or ""
    tags, total = [], 0
    for t in hashtags(caption):
        if total + len(t) + 1 > 450:
            break
        tags.append(t)
        total += len(t) + 1
    body = {"snippet": {"title": youtube_title(meta), "description": re.sub(r"[<>]", "", caption)[:4900],
                        "tags": tags, "categoryId": "20"},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    r = net.req("POST", "https://www.googleapis.com/upload/youtube/v3/videos",
                params={"uploadType": "resumable", "part": "snippet,status"}, data=json.dumps(body),
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json; charset=UTF-8",
                         "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)})
    if r.status_code != 200 or not r.headers.get("Location"):
        raise PostError("YouTube wouldn't start the upload: " + _google_error(r))
    with open(video, "rb") as fh:
        r = net.req("PUT", r.headers["Location"], data=fh, timeout=900,
                    headers={"Authorization": f"Bearer {tok}", "Content-Type": "video/mp4", "Content-Length": str(size)})
    vid = net.body(r).get("id")
    if r.status_code not in (200, 201) or not vid:
        raise PostError("YouTube didn't take the video: " + _google_error(r))
    return {"url": f"https://www.youtube.com/shorts/{vid}"}


def _google_error(r) -> str:
    err = net.body(r).get("error") or {}
    if isinstance(err, dict):
        reason = ((err.get("errors") or [{}])[0] or {}).get("reason", "")
        msg = err.get("message") or ""
        if reason in ("quotaExceeded", "uploadLimitExceeded"):
            return "today's upload limit is used up; it'll go out tomorrow"
        return f"{msg} ({reason})" if reason else (msg or str(r.status_code))
    return str(err or r.status_code)[:160]


# --- TikTok: Content Posting API ------------------------------------------------------------------
def chunks(size: int) -> tuple[int, int]:
    """TikTok's chunk rule: one piece up to 64 MB; bigger files in 10 MB pieces, the last one taking the rest."""
    if size <= 64 * MB:
        return size, 1
    chunk = 10 * MB
    return chunk, size // chunk


def _tt(path: str, tok: str, body: dict | None = None) -> dict:
    r = net.req("POST", TT_API + path, data=json.dumps(body or {}),
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json; charset=UTF-8"})
    j = net.body(r)
    err = j.get("error") or {}
    if r.status_code != 200 or (err.get("code") not in (None, "", "ok")):
        code = err.get("code") or str(r.status_code)
        raise PostError(f"TikTok said {code}: {(err.get('message') or '')[:140]}", code)
    return j.get("data") or {}


def _upload(url: str, video: Path, size: int, chunk: int, count: int) -> None:
    with open(video, "rb") as fh:
        for i in range(count):
            start = i * chunk
            end = size - 1 if i == count - 1 else start + chunk - 1
            fh.seek(start)
            part = fh.read(end - start + 1)
            r = net.req("PUT", url, data=part, timeout=600, headers={
                "Content-Type": "video/mp4", "Content-Length": str(len(part)),
                "Content-Range": f"bytes {start}-{end}/{size}"})
            if r.status_code not in (200, 201, 206):
                raise PostError(f"TikTok stopped the upload at part {i + 1} of {count} ({r.status_code})")


def public_posting() -> bool:
    """TikTok only lets audited apps post publicly. Until TikTok has approved the app, clips go to the inbox."""
    return (os.getenv("TIKTOK_AUDITED") or "").strip().lower() in ("1", "yes", "true")


def tiktok(slot: int, meta: dict, sleep=time.sleep) -> dict:
    video = video_of(meta)
    tok = connect.access("tiktok", slot)
    size = video.stat().st_size
    chunk, count = chunks(size)
    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}
    data, direct = None, public_posting()
    if direct:
        info = _tt("/post/publish/creator_info/query/", tok)
        longest = info.get("max_video_post_duration_sec")
        if longest and (meta.get("seconds") or 0) > longest:
            raise PostError(f"this account can only post videos up to {longest}s")
        options = info.get("privacy_level_options") or []
        if "PUBLIC_TO_EVERYONE" not in options:
            raise PostError("this TikTok account can't post publicly (it may be set to private)")
        paid = bool(meta.get("campaign_id"))
        try:
            data = _tt("/post/publish/video/init/", tok, {
                "post_info": {"title": (meta.get("caption") or "")[:2200], "privacy_level": "PUBLIC_TO_EVERYONE",
                              "disable_duet": False, "disable_comment": False, "disable_stitch": False,
                              "brand_content_toggle": paid, "brand_organic_toggle": False},
                "source_info": source})
        except PostError as e:
            if "unaudited" not in e.code:
                raise
            direct = False            # TikTok hasn't approved the app for public posts yet: use the inbox instead
    if not direct:
        data = _tt("/post/publish/inbox/video/init/", tok, {"source_info": source})
    _upload(data["upload_url"], video, size, chunk, count)
    pid = data.get("publish_id")
    for _ in range(STATUS_TRIES):
        sleep(5)
        st = _tt("/post/publish/status/fetch/", tok, {"publish_id": pid})
        s = st.get("status")
        if s == "SEND_TO_USER_INBOX":
            return {"inbox": True}
        if s == "PUBLISH_COMPLETE":
            ids = st.get("publicaly_available_post_id") or st.get("publicly_available_post_id") or []
            user = connect._entry("tiktok", slot).get("username")
            if ids and user:
                return {"url": f"https://www.tiktok.com/@{user}/video/{ids[0]}"}
            return {"url": "", "note": "posted; TikTok didn't send the link yet"}
        if s == "FAILED":
            raise PostError(f"TikTok couldn't post it: {st.get('fail_reason') or 'no reason given'}")
    raise PostError("TikTok is still processing it after 10 minutes; check the app")


def publish(platform: str, slot: int, meta: dict) -> dict:
    if platform == "instagram":
        return instagram(meta)
    if platform == "youtube":
        return youtube(slot, meta)
    if platform == "tiktok":
        return tiktok(slot, meta)
    raise PostError("unknown platform")
