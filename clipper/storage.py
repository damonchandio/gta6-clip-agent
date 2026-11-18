"""Instagram's API fetches each video from a public link, so a clip is put in a public Supabase Storage bucket
for the few minutes Instagram needs, then deleted. Free Supabase project: set SUPABASE_URL and SUPABASE_KEY
(the service_role / secret key) in .env, and make a public bucket named "reels" (or set SUPABASE_BUCKET)."""
import os

import requests


def _cfg() -> tuple[str, str, str]:
    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.getenv("SUPABASE_KEY") or "").strip()
    if not (url and key):
        raise RuntimeError("Instagram needs SUPABASE_URL and SUPABASE_KEY in .env to hand it the video (see README)")
    return url, key, (os.getenv("SUPABASE_BUCKET") or "reels").strip()


def _headers(key: str, extra: dict | None = None) -> dict:
    h = {"apikey": key}
    if key.startswith("eyJ"):            # legacy JWT keys also go in Authorization; new sb_secret_ keys must not
        h["Authorization"] = f"Bearer {key}"
    h.update(extra or {})
    return h


def host(video, name: str) -> str:
    url, key, bucket = _cfg()
    with open(video, "rb") as fh:
        r = requests.post(f"{url}/storage/v1/object/{bucket}/{name}", data=fh, timeout=300,
                          headers=_headers(key, {"Content-Type": "video/mp4", "x-upsert": "true"}))
    if r.status_code >= 300:
        raise RuntimeError(f"couldn't put the video online for Instagram ({r.status_code}: {r.text[:120]})")
    return f"{url}/storage/v1/object/public/{bucket}/{name}"


def unhost(name: str) -> None:
    try:
        url, key, bucket = _cfg()
        requests.delete(f"{url}/storage/v1/object/{bucket}/{name}", headers=_headers(key), timeout=30)
    except Exception:  # noqa: BLE001  a leftover copy is harmless; the next post overwrites nothing of it
        pass
