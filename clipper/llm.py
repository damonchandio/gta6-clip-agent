"""The AI calls that pick moments and write hooks, all returning JSON. Free first:

  1. Groq (free API key from console.groq.com) when GROQ_API_KEY is set. Each model has its own rate limit,
     so when one is used up the next one in GROQ_MODELS takes over.
  2. Ollama on this computer (free, offline) when it's running.
Each one that fails hands over to the next. When none answers, gameplay is cut by its shots instead.
"""
import json
import logging
import os
import re
import time

import requests

import config

log = logging.getLogger("clipper")
GROQ = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS = [m.strip() for m in os.getenv(
    "GROQ_MODELS", os.getenv("GROQ_MODEL", "openai/gpt-oss-120b") + ",llama-3.3-70b-versatile,openai/gpt-oss-20b").split(",") if m.strip()]
_off: dict = {}  # backend -> monotonic time it may be tried again


def parse_json(text: str):
    t = re.sub(r"```(?:json)?", "", text).strip()
    starts = [i for i in (t.find("["), t.find("{")) if i >= 0]
    if not starts:
        raise ValueError("no JSON in the reply")
    got = json.JSONDecoder().raw_decode(t[min(starts):])[0]
    if isinstance(got, dict) and len(got) == 1 and isinstance(next(iter(got.values())), list):
        return next(iter(got.values()))  # {"clips": [...]} -> [...]
    return got


def _usable(name: str) -> bool:
    return time.monotonic() >= _off.get(name, 0)


def _rest(name: str, hours: float) -> None:
    _off[name] = time.monotonic() + hours * 3600


def backends() -> list[str]:
    out = [f"groq:{m}" for m in GROQ_MODELS if os.getenv("GROQ_API_KEY") and _usable(f"groq:{m}")]
    if _usable("local"):
        out.append("local")
    return out


def window_seconds() -> int:
    """How much transcript to read in one go."""
    return 480


def _groq(model: str, system: str, user: str, max_tokens: int) -> str:
    r = requests.post(GROQ, timeout=120, headers={"Authorization": f"Bearer {os.getenv('GROQ_API_KEY')}"}, json={
        "model": model, "temperature": 0.3, "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    if r.status_code == 429:
        _rest(f"groq:{model}", 0.25)
    elif r.status_code in (401, 403):
        for m in GROQ_MODELS:
            _rest(f"groq:{m}", 24)
    elif r.status_code == 404:
        _rest(f"groq:{model}", 24)            # that model was retired
    if r.status_code != 200:
        raise RuntimeError(f"Groq error {r.status_code}: {r.text[:160]}")
    return r.json()["choices"][0]["message"]["content"]


def _local(system: str, user: str, max_tokens: int) -> str:
    body = {"model": config.OLLAMA_MODEL, "stream": False, "keep_alive": config.OLLAMA_KEEP_ALIVE,
            "messages": [{"role": "system", "content": system + "\nReply with the JSON only, nothing else."},
                         {"role": "user", "content": user}],
            "options": {"temperature": 0.3, "num_predict": min(max_tokens, 1800), "num_ctx": 8192}}
    try:
        r = requests.post(f"{config.OLLAMA_URL}/api/chat", json=body, timeout=300)
    except requests.ConnectionError:
        _rest("local", 0.5)                   # Ollama isn't running: don't knock again for half an hour
        raise RuntimeError("Ollama isn't running")
    r.raise_for_status()
    return (r.json().get("message") or {}).get("content", "")


def ask_json(system: str, user: str, max_tokens: int = 2500, model: str = "") -> tuple[object, float]:
    """Returns (parsed JSON, cost in USD, always 0 here). Tries each backend in turn."""
    errors = []
    for b in backends():
        try:
            if b.startswith("groq:"):
                return parse_json(_groq(b[5:], system, user, max_tokens)), 0.0
            return parse_json(_local(system, user, max_tokens)), 0.0
        except Exception as e:  # noqa: BLE001
            errors.append(f"{b}: {str(e)[:120]}")
            log.info("clipper AI: %s didn't work (%s), trying the next one", b, str(e)[:120])
    raise RuntimeError("no AI could answer: " + (" | ".join(errors) or "add GROQ_API_KEY to .env, or run Ollama"))
