import os
from dotenv import load_dotenv

load_dotenv()


def _safe_int(env_key: str, default: int) -> int:
    """Parse integer env var safely with a friendly error message."""
    val = os.getenv(env_key, str(default))
    try:
        return int(val.strip())
    except (ValueError, AttributeError):
        raise ValueError(
            f"Environment variable '{env_key}' must be an integer, got: '{val}'"
        )


class Config:
    # Telegram API credentials (from https://my.telegram.org)
    API_ID: int = _safe_int("API_ID", 0)
    API_HASH: str = os.getenv("API_HASH", "")
    STRING_SESSION: str = os.getenv("STRING_SESSION", "")

    # Channel IDs to monitor (comma-separated negative integers)
    # Example: -1001234567890,-1009876543210
    CHANNELS_RAW: str = os.getenv("CHANNELS", "")
    CHANNELS: list = []
    if CHANNELS_RAW:
        try:
            CHANNELS = [
                int(x.strip())
                for x in CHANNELS_RAW.split(",")
                if x.strip()
            ]
        except ValueError as e:
            raise ValueError(
                f"CHANNELS must be comma-separated integers, got error: {e}"
            )

    # n8n webhook URL — receives deal payload
    # n8n handles: Layer 2 dedup → Wishlink API → copyMessage → Sheets log
    N8N_WEBHOOK_URL: str = os.getenv("N8N_WEBHOOK_URL", "")

    # Seconds to sleep between queue items
    # Keeps Wishlink API rate within limits (5 calls/batch, 120s cooldown)
    POST_DELAY: int = _safe_int("POST_DELAY", 3)

    # Seconds between polling backup cycles
    POLLING_INTERVAL: int = _safe_int("POLLING_INTERVAL", 600)

    # Max messages to check per channel per polling cycle
    POLLING_LIMIT: int = _safe_int("POLLING_LIMIT", 5)

    # Flask server port (set automatically by Render)
    PORT: int = _safe_int("PORT", 10000)

    @classmethod
    def validate(cls):
        """Fail fast at startup if required env vars are missing."""
        errors = []
        if not cls.API_ID:
            errors.append("API_ID (must be non-zero integer)")
        if not cls.API_HASH:
            errors.append("API_HASH")
        if not cls.STRING_SESSION:
            errors.append("STRING_SESSION")
        if not cls.N8N_WEBHOOK_URL:
            errors.append("N8N_WEBHOOK_URL")
        if not cls.CHANNELS:
            errors.append("CHANNELS (comma-separated channel IDs)")
        if errors:
            raise ValueError(
                f"Missing/invalid environment variables:\n  - "
                + "\n  - ".join(errors)
            )


Config.validate()
