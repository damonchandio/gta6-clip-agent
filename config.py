"""Settings, read from the .env file next to this one (copy .env.example to .env to start)."""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:          # setup.bat installs it; without it, plain environment variables still work
    load_dotenv = None

ROOT = Path(__file__).resolve().parent
if load_dotenv:
    load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR") or ROOT / "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3-turbo")      # on an NVIDIA GPU; small.en etc. on the CPU
WHISPER_CPU_MODEL = os.getenv("WHISPER_CPU_MODEL", "small.en")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").replace("://localhost", "://127.0.0.1")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "30m")
TELEGRAM_BOT_TOKEN = ""      # the agent has its own bot: GTA6_TELEGRAM_TOKEN
TELEGRAM_CHAT_ID = ""
