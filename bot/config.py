import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _req(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"[config] Missing required environment variable: {name} (see .env.example)")
    return value


API_ID = int(_req("API_ID"))
API_HASH = _req("API_HASH")
BOT_TOKEN = _req("BOT_TOKEN")
OWNER_ID = int(_req("OWNER_ID"))
MONGO_URI = _req("MONGO_URI")
ENCRYPTION_KEY = _req("ENCRYPTION_KEY")

OWNER_USERNAME = os.getenv("OWNER_USERNAME", "").strip().lstrip("@")
DB_NAME = os.getenv("DB_NAME", "audio_forwarder").strip() or "audio_forwarder"
REQUIRE_APPROVAL = os.getenv("REQUIRE_APPROVAL", "true").strip().lower() in ("1", "true", "yes")

REC_SAMPLE_RATE = int(os.getenv("REC_SAMPLE_RATE", "48000"))
REC_CHANNELS = int(os.getenv("REC_CHANNELS", "2"))

PLAYBACK_MODE = os.getenv("PLAYBACK_MODE", "fifo").strip().lower()  # fifo | external  (default fifo)
if PLAYBACK_MODE not in ("fifo", "external", "auto"):
    PLAYBACK_MODE = "fifo"

LOGIN_TIMEOUT = 600  # seconds a login attempt may stay open
