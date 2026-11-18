"""python -m clipper.transcribe_worker VIDEO OUT.json MODEL [cpu]

Transcribes a long video with word timings and writes the segments to OUT.json. It runs in its own
process because a Whisper model on the GPU can crash the whole program with no error; if this process dies,
the app lives on and retries on the CPU.
"""
import glob
import json
import os
import site
import sys


def _cuda_dlls() -> None:
    """Windows: let faster-whisper find the CUDA libraries from pip (requirements-gpu.txt)."""
    if os.name != "nt":
        return
    for base in site.getsitepackages() + [site.getusersitepackages()]:
        for d in glob.glob(os.path.join(base, "nvidia", "*", "bin")):
            os.add_dll_directory(d)
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def main(path: str, out: str, model_name: str, device: str = "cuda") -> int:
    from faster_whisper import WhisperModel
    if device == "cuda":
        _cuda_dlls()
        model = WhisperModel(model_name, device="cuda", compute_type="float16")
    else:
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
    lang = None if not model_name.endswith(".en") else "en"
    segs, _ = model.transcribe(path, word_timestamps=True, vad_filter=True, beam_size=1, language=lang)
    res = []
    for s in segs:
        words = [{"w": w.word.strip(), "s": round(w.start, 2), "e": round(w.end, 2)} for w in (s.words or [])
                 if w.word.strip()]
        if words:
            res.append({"s": round(s.start, 2), "e": round(s.end, 2), "text": s.text.strip(), "words": words})
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(res, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "cuda"))
