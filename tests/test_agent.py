"""pytest: the agent's own self-test, the AI fallback chain, and a real render of a clip (offline)."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_py(code: str, tmp_path, **env):
    e = {**os.environ, "DATA_DIR": str(tmp_path / "data"), "PYTHONPATH": str(ROOT), "GROQ_API_KEY": "", **env}
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=e, capture_output=True, text=True, timeout=600)


def test_selftest():
    r = subprocess.run([sys.executable, "-m", "gta6", "--selftest"], cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0 and "selftest ok" in r.stdout, r.stdout + r.stderr


def test_ai_falls_back_across_models(tmp_path):
    code = r'''
import json, requests
from clipper import llm
calls = []
class R:
    def __init__(s, code, body): s.status_code, s._b, s.text = code, body, json.dumps(body)
    def json(s): return s._b
def post(url, **kw):
    calls.append(kw.get("json", {}).get("model") or url)
    if "groq" in url and kw["json"]["model"] == llm.GROQ_MODELS[0]:
        return R(429, {"error": "rate limited"})
    if "groq" in url:
        return R(200, {"choices": [{"message": {"content": '[{"start": 1, "end": 30}]'}}]})
    raise requests.ConnectionError("no ollama")
requests.post = post
got, cost = llm.ask_json("s", "u")
assert got == [{"start": 1, "end": 30}], got
assert calls[:2] == llm.GROQ_MODELS[:2], calls
assert "groq:" + llm.GROQ_MODELS[0] not in llm.backends()      # resting after its 429
llm.GROQ_MODELS[:] = []
try:
    llm.ask_json("s", "u")
    raise SystemExit("should have failed with no AI at all")
except RuntimeError as e:
    assert "Ollama" in str(e) or "GROQ" in str(e), e
print("ok")
'''
    r = run_py(code, tmp_path, GROQ_API_KEY="test-key")
    assert r.returncode == 0 and r.stdout.strip().endswith("ok"), r.stdout + r.stderr


def test_engine_renders_a_clip_offline(tmp_path):
    """A silent 40-second test video: no speech, no AI, so it's cut by its shots and rendered with captions off."""
    code = r'''
import json, subprocess, sys
from clipper import engine, OUT
src = OUT.parent / "test.mp4"
subprocess.run([engine.ffmpeg(), "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:d=40",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
s = engine.make_clips(str(src), {"account_slot": 1, "title": "GTA 6", "min_seconds": 12, "max_seconds": 20}, 2,
                      progress=lambda m: None)
assert s["clips"] >= 1, s
meta = json.loads((OUT / s["job"] / "clip_01.json").read_text())
w, h, _ = engine.probe(OUT / s["job"] / "clip_01.mp4")
assert (w, h) == (1080, 1920) and 12 <= meta["seconds"] <= 21, (w, h, meta)
print("ok", s["clips"])
'''
    r = run_py(code, tmp_path)
    assert r.returncode == 0 and "ok" in r.stdout, r.stdout + r.stderr
