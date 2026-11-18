"""The clip engine: long video in, vertical captioned clips out (transcribe, pick moments, render)."""
import config

HOME = config.DATA_DIR / "clipper"
SOURCES = HOME / "sources"
TRANSCRIPTS = HOME / "transcripts"
OUT = HOME / "out"
for _d in (HOME, SOURCES, TRANSCRIPTS, OUT):
    _d.mkdir(parents=True, exist_ok=True)
