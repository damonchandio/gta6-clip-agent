"""Ranking paid clipping campaigns from Content Rewards' public feed: which ones pay best per hour of clipping.

(The searching itself is in gta6/campaigns.py; this is the scoring it uses.)
"""
import datetime as dt
import math
import re

MIN_REMAINING = 300          # below this a campaign is about to run dry


# Never clip these: promoting unlicensed gambling to UK audiences is illegal, adult content breaks platform rules
EXCLUDE = re.compile(r"casino|betting|\bbets?\b|gambl|\bslots?\b|sportsbook|stake\.com|poker|roulette|metawin|"
                     r"\bshuffle\b|onlyfans|nsfw|\badult\b|18\+", re.I)


# Aimed at another country or language: your accounts are UK / English
FOREIGN = re.compile(r"\b(germany|german|deutsch|france|french|spain|spanish|espa[nñ]ol|brazil|brasil|portugu|"
                     r"italia|italian|dutch|arabic|hindi|turkish|polish|japan|korea|latam|mexico)\b", re.I)


PLATFORMS = ("tiktok", "instagram", "youtube", "twitter", "snapchat", "facebook")


def platforms_in(text: str) -> list[str]:
    low = text.lower()
    return [p for p in PLATFORMS if re.search(rf"\b{p}\b", low)]


def _campaign(**kw) -> dict:
    c = {"name": "", "url": "", "rate": None, "spent": None, "total": None, "participants": None,
         "platforms": [], "application": False, "verified": False}
    c.update(kw)
    c["remaining"] = (c["total"] - c["spent"]) if c["total"] is not None and c["spent"] is not None else None
    c["slug"] = re.sub(r"[^a-z0-9]+", "-", c["name"].lower()).strip("-")[:60] or "campaign"
    return c


def from_api(items: list[dict]) -> list[dict]:
    """The site's own campaign records -> our campaign dicts."""
    out, seen = [], set()
    now = dt.datetime.now(dt.timezone.utc)
    for x in items:
        if x.get("id") in seen or x.get("status", "active") != "active" or x.get("private"):
            continue
        seen.add(x.get("id"))
        m = x.get("metrics") or {}
        pays = [p for p in (x.get("payouts") or []) if p.get("payoutType", "cpm") == "cpm"]
        rate = max([p.get("rateCents") or 0 for p in pays] + [x.get("primaryPayoutCents") or 0]) / 100
        min_pay = min([p.get("minPayoutCents") or 0 for p in pays] or [0]) / 100
        max_pay = max([p.get("maxPayoutCents") or 0 for p in pays] or [0]) / 100
        week = sorted(m.get("chartPoints") or [], key=lambda c: c.get("bucket", ""))[-7:]
        views_7d = sum(c.get("totalViews") or 0 for c in week)
        burn = views_7d / 7 / 1000 * rate if rate else 0          # dollars a day going out
        total, spent = (x.get("budgetCents") or 0) / 100, (m.get("budgetSpentCents") or 0) / 100
        try:
            age = (now - dt.datetime.fromisoformat(x.get("launchDate", "").replace("Z", "+00:00"))).days
        except Exception:  # noqa: BLE001
            age = None
        c = _campaign(
            name=x.get("name") or "Unnamed", url="", rate=rate, spent=spent, total=total,
            participants=m.get("creatorCount"), platforms=list(x.get("platforms") or []),
            application=bool(x.get("requiresApplication")), verified=bool(x.get("organizationVerified")))
        c.update(id=x.get("id"), brand=x.get("organizationName") or "", min_payout=min_pay, max_payout=max_pay,
                 views_to_first_payout=int(min_pay / rate * 1000) if rate and min_pay else 0,
                 burn_per_day=round(burn, 2), days_left=round((total - spent) / burn, 1) if burn > 0 else None,
                 categories=[k.get("name") for k in (x.get("categories") or []) if k.get("name")],
                 description=(x.get("description") or "")[:600], age_days=age,
                 sources=[r.get("url") for r in (x.get("referenceMaterials") or []) if r.get("url")],
                 total_views=m.get("totalViews"), paid_out=(m.get("paidOutCents") or 0) / 100,
                 per_platform={p.get("platform"): {"rate": (p.get("rateCents") or 0) / 100,
                                                   "min": (p.get("minPayoutCents") or 0) / 100,
                                                   "max": (p.get("maxPayoutCents") or 0) / 100} for p in pays})
        out.append(c)
    return out


# --- ranking --------------------------------------------------------------------
def score(c: dict) -> tuple[float, list[str]]:
    """Higher = more money per hour of clipping. Explains itself."""
    why = []
    rate = c.get("rate") or 0
    rem = c.get("remaining")
    ppl = c.get("participants")
    blob = " ".join([c.get("name", ""), c.get("brand", ""), c.get("description", "")] + (c.get("categories") or []))
    if EXCLUDE.search(blob):
        return 0.0, ["gambling or adult promotion: excluded"]
    if not rate:
        return 0.0, ["not a pay-per-view campaign"]
    if rem is not None and rem < MIN_REMAINING:
        return 0.0, [f"only ${rem:,.0f} left: about to run dry"]
    s = rate
    why.append(f"${rate:.2f} per 1K views")
    if rem is not None:
        per_head = rem / ((ppl or 0) + 5)
        s *= math.sqrt(min(per_head, 2000)) / 10
        why.append(f"${rem:,.0f} left" + (f" between {ppl:,.0f} clippers" if ppl is not None else ""))
    else:
        s *= 1.5
    need = c.get("views_to_first_payout") or 0
    if need:
        s *= 1.0 if need <= 5000 else 0.8 if need <= 15000 else 0.6 if need <= 40000 else 0.4
        why.append(f"pays from {need:,} views")
    if c.get("max_payout"):
        why.append(f"up to ${c['max_payout']:,.0f} a clip")
    days = c.get("days_left")
    if days is not None and days < 3:
        s *= 0.5
        why.append(f"budget gone in ~{days:.0f} days")
    if ppl is not None and ppl > 1000:
        why.append("very crowded")
    if c.get("age_days") is not None and c["age_days"] <= 3 and (ppl or 0) < 50:
        s *= 1.15
        why.append("brand new: early-mover advantage")
    if c.get("application"):
        s *= 0.85
        why.append("needs an application")
    if re.search(r"\bUGC\b", c.get("name", "") + " " + c.get("description", "")[:300]):
        s *= 0.7
        why.append("UGC: you film your own content rather than clipping")
    if FOREIGN.search(c.get("name", "") + " " + c.get("description", "")[:200]):
        s *= 0.3
        why.append("aimed at another country or language")
    if days is not None and days > 120:
        why.append("plenty of runway")
    elif days is not None and days >= 3:
        why.append(f"~{days:.0f} days of budget at today's pace")
    if c.get("verified"):
        s *= 1.1
    return round(s, 2), why


def rank(campaigns: list[dict], platforms: tuple = ("tiktok", "instagram", "youtube")) -> list[dict]:
    seen, out = set(), []
    for c in campaigns:
        key = c["name"].lower()
        if key in seen:
            continue
        seen.add(key)
        c["score"], c["why"] = score(c)
        if c["platforms"] and not set(c["platforms"]) & set(platforms):
            c["score"], c["why"] = 0.0, c["why"] + ["not on your platforms"]
        out.append(c)
    return sorted(out, key=lambda c: c["score"], reverse=True)

