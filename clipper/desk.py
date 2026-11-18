"""Reading the clips the engine made (each clip is an .mp4 with a .json beside it)."""
import json

from . import OUT


def clips(status: str | None = None) -> list[dict]:
    out = []
    for f in sorted(OUT.glob("*/clip_*.json"), reverse=True):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if status is None or m.get("status") == status:
            out.append(m)
    return out


def _meta_path(clip_id: str):
    """The .json for a clip id, refusing anything that points outside the clips folder."""
    job, name = clip_id.split("/", 1)
    p = (OUT / job / f"{name}.json").resolve()
    if OUT.resolve() not in p.parents:
        raise ValueError("bad clip id")
    return p
