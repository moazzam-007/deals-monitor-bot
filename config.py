import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Telegram API credentials (from my.telegram.org)
    API_ID = int(os.getenv("API_ID", "0"))
    API_HASH = os.getenv("API_HASH", "")
    STRING_SESSION = os.getenv("STRING_SESSION", "")

    # Channel IDs to monitor (comma-separated)
    CHANNELS_RAW = os.getenv("CHANNELS", "")
    CHANNELS = []
    if CHANNELS_RAW:
        try:
            CHANNELS = [int(x.strip()) for x in CHANNELS_RAW.split(",") if x.strip()]
        except ValueError as e:
            raise ValueError(f"Invalid CHANNELS config: {e}")

    # Output channel where affiliate posts will be sent
    OUTPUT_CHANNEL_ID = int(os.getenv("OUTPUT_CHANNEL_ID", "0"))

    # n8n webhook URL for processing
    N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")

    # Delay between posts to output channel (seconds)
    POST_DELAY = int(os.getenv("POST_DELAY", "3"))

    # Polling backup interval (seconds) — fallback to catch missed messages
    POLLING_INTERVAL = int(os.getenv("POLLING_INTERVAL", "600"))

    # Polling message limit per channel per cycle
    POLLING_LIMIT = int(os.getenv("POLLING_LIMIT", "5"))

    # Flask server port
    PORT = int(os.getenv("PORT", "10000"))


    @classmethod
    def validate(cls):
        errors = []
        if not cls.API_ID:
            errors.append("API_ID")
        if not cls.API_HASH:
            errors.append("API_HASH")
        if not cls.STRING_SESSION:
            errors.append("STRING_SESSION")
        if not cls.N8N_WEBHOOK_URL:
            errors.append("N8N_WEBHOOK_URL")
        if cls.OUTPUT_CHANNEL_ID == 0:
            errors.append("OUTPUT_CHANNEL_ID")
        if not cls.CHANNELS:
            errors.append("CHANNELS")
        if errors:
            raise ValueError(f"Missing/invalid env vars: {', '.join(errors)}")

Config.validate()
