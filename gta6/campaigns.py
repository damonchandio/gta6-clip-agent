"""Finding paid clipping campaigns on Content Rewards (Whop): GTA ones by itself every few hours, and any keyword
from the search box. Reads the same public campaign feed the site's Discover page uses, gently: one page per
search word. Joining a campaign stays your job on the site (it means accepting the brand's terms)."""
from __future__ import annotations

import json
import re
import threading
import time

import config

from . import net

HOME = config.DATA_DIR / "gta6"
FILE = HOME / "campaigns.json"
API = "https://contentrewards.com/api/campaign/campaigns/discover"
PAGE = "https://contentrewards.com/c/campaigns/{id}"
AUTO_EVERY = 3 * 3600
AUTO_WORDS = ("gta 6", "gta vi", "grand theft auto", "gta")
GTA6 = re.compile(r"\bgta\s*(6|vi)\b|grand theft auto\s*(6|vi)\b|\bleonida\b|\bvice city\b", re.I)
GTA_ANY = re.compile(r"\bgta\b|grand theft auto|\bleonida\b|\bvice city\b|\brockstar\b", re.I)
KEEP = ("id", "name", "brand", "rate", "remaining", "total", "participants", "platforms", "application", "verified",
        "score", "why", "description", "categories", "days_left", "min_payout", "max_payout", "views_to_first_payout",
        "age_days", "sources")
_lock = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"at": 0, "found": [], "seen": [], "search": {"q": "", "at": 0, "results": []}, "error": ""}


def _save(d: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(d, indent=1), encoding="utf-8")


def fetch(word: str, pages: int = 1, sleep=time.sleep) -> list[dict]:
    """Raw campaign records for a search word, newest feed order."""
    out, cursor = [], None
    for i in range(pages):
        params = {"collapseGroups": "true", "limit": "50", "search": word}
        if cursor:
            params["cursor"] = cursor
        r = net.req("GET", API, params=params, timeout=30, headers={"Accept": "application/json"})
        if r.status_code != 200:
            if out:
                break
            raise RuntimeError(f"Content Rewards answered {r.status_code}"
                               + (" (it may be down for maintenance)" if r.status_code >= 500 else ""))
        j = net.body(r)
        out += [x for x in (j.get("data") or []) if isinstance(x, dict)]
        cursor = (j.get("pagination") or {}).get("nextCursor")
        if not cursor:
            break
        sleep(0.6)
    return out


def tags_in(text: str) -> tuple[list[str], list[str]]:
    """Hashtags and @mentions a campaign's description asks for (emails are not mentions)."""
    tags = list(dict.fromkeys("#" + t.lower() for t in re.findall(r"(?<![\w&])#([A-Za-z]\w{1,40})", text or "")))
    ments = list(dict.fromkeys("@" + m.rstrip(".") for m in re.findall(r"(?<![\w.])@([A-Za-z0-9_.]{2,30})", text or "")))
    return tags[:5], ments[:3]


def shape(items: list[dict]) -> list[dict]:
    from clipper.scout import from_api, rank
    out = []
    for c in rank(from_api(items)):
        x = {k: c.get(k) for k in KEEP}
        blob = f"{x.get('name')} {x.get('brand')} {x.get('description')}"
        x.update(url=PAGE.format(id=x["id"]), gta6=bool(GTA6.search(blob)), gta=bool(GTA_ANY.search(blob)),
                 footage=[u for u in (x.pop("sources") or []) if isinstance(u, str) and u.startswith(("http://", "https://"))][:10])
        x["hashtags"], x["mentions"] = tags_in(x.get("description") or "")
        out.append(x)
    return out


def search(word: str) -> list[dict]:
    word = re.sub(r"\s+", " ", (word or "").strip())[:60]
    if len(word) < 2:
        raise ValueError("Type at least two letters to search campaigns.")
    results = shape(fetch(word, pages=2))
    with _lock:
        d = _load()
        d["search"] = {"q": word, "at": time.time(), "results": results[:60]}
        _save(d)
    return results


def auto(force: bool = False) -> tuple[list[dict], list[dict]]:
    """GTA campaigns, refreshed every few hours. Returns (all found, the ones never seen before)."""
    d = _load()
    if not force and time.time() - d.get("at", 0) < AUTO_EVERY:
        return d["found"], []
    raw, seen_ids, errors = [], set(), []
    for w in AUTO_WORDS:
        try:
            for x in fetch(w):
                if x.get("id") not in seen_ids:
                    seen_ids.add(x.get("id"))
                    raw.append(x)
        except Exception as e:  # noqa: BLE001  one word failing still leaves the others
            errors.append(str(e)[:120])
    found = [c for c in shape(raw) if c["gta"]]
    found.sort(key=lambda c: (not c["gta6"], -(c.get("score") or 0)))
    with _lock:
        d = _load()
        if not raw and errors:          # the site didn't answer: keep the last good list
            d.update(at=time.time() - AUTO_EVERY + 1800, error=errors[0])
            _save(d)
            return d["found"], []
        new = [c for c in found if c["id"] not in set(d.get("seen") or []) and (c.get("score") or 0) > 0]
        d.update(at=time.time(), found=found, seen=list(dict.fromkeys((d.get("seen") or []) + [c["id"] for c in found]))[-2000:],
                 error=errors[0] if errors else "")
        _save(d)
    return found, new


def get(cid: str) -> dict | None:
    d = _load()
    for c in d["found"] + (d.get("search") or {}).get("results", []):
        if c.get("id") == cid:
            return c
    return None


def rules(c: dict) -> dict:
    """What the clip engine must follow for this campaign."""
    tags, ments = c.get("hashtags") or [], c.get("mentions") or []
    return {"title": c["name"][:80], "slug": re.sub(r"[^\w]+", "-", c["name"].lower()).strip("-")[:40],
            "campaign_id": c["id"], "brand": c.get("brand") or "", "required_hashtags": tags,
            "required_mentions": ments, "disclosure": "#ad", "platforms": c.get("platforms") or [],
            "url": c.get("url") or PAGE.format(id=c["id"]), "rate": c.get("rate"),
            "content_notes": ("Campaign brief: " + (c.get("description") or "")[:500]).strip()}


def snapshot() -> dict:
    d = _load()
    return {"at": d.get("at", 0), "found": d.get("found", [])[:40], "search": d.get("search") or {},
            "error": d.get("error", "")}
