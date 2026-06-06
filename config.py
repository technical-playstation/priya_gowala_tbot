"""
config.py - Centralized configuration and environment variable management.
Validates all required secrets on startup. Fails fast if anything is missing.
"""

import os
import sys
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Config:
    # ── Telegram ──────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = field(default_factory=lambda: os.environ["TELEGRAM_BOT_TOKEN"])

    # ── AI ────────────────────────────────────────────────────────────────────
    GEMINI_API_KEY: str = field(default_factory=lambda: os.environ["GEMINI_API_KEY"])
    GEMINI_MODEL: str = "gemini-1.5-flash"
    GEMINI_MAX_TOKENS: int = 1024
    GEMINI_TIMEOUT: int = 30
    GEMINI_RETRY_ATTEMPTS: int = 3
    GEMINI_RETRY_DELAY: float = 1.5

    # ── Supabase ──────────────────────────────────────────────────────────────
    SUPABASE_URL: str = field(default_factory=lambda: os.environ["SUPABASE_URL"])
    SUPABASE_KEY: str = field(default_factory=lambda: os.environ["SUPABASE_KEY"])

    # ── Admin ─────────────────────────────────────────────────────────────────
    ADMIN_CHAT_ID: int = field(
        default_factory=lambda: int(os.environ["ADMIN_CHAT_ID"])
    )

    # ── Payments ──────────────────────────────────────────────────────────────
    UPI_ID: str = field(default_factory=lambda: os.environ["UPI_ID"])
    PAYMENT_SESSION_EXPIRY_MINUTES: int = 30

    PLANS: dict = field(default_factory=lambda: {
        "basic":    {"price": 11,  "days": 3,  "label": "₹11 → 3 Days"},
        "monthly":  {"price": 29,  "days": 30, "label": "₹29 → 30 Days (First Purchase)"},
        "standard": {"price": 49,  "days": 30, "label": "₹49 → 30 Days Standard"},
    })

    # ── Render / Webhook ──────────────────────────────────────────────────────
    RENDER_APP_NAME: str = field(
        default_factory=lambda: os.environ.get("RENDER_APP_NAME", "")
    )
    PORT: int = field(default_factory=lambda: int(os.environ.get("PORT", 8080)))
    WEBHOOK_SECRET: str = field(
        default_factory=lambda: os.environ.get("WEBHOOK_SECRET", "")
    )

    # ── Rate limiting ─────────────────────────────────────────────────────────
    RATE_LIMIT_MESSAGES: int = 20       # max messages per window
    RATE_LIMIT_WINDOW_SECONDS: int = 60  # rolling window

    # ── Voice ─────────────────────────────────────────────────────────────────
    VOICE_TEMP_DIR: str = "temp"
    EDGE_TTS_RETRY_ATTEMPTS: int = 3

    # ── Conversation memory ───────────────────────────────────────────────────
    MAX_MEMORY_MESSAGES: int = 20

    # ── Logging ───────────────────────────────────────────────────────────────
    LOG_DIR: str = "logs"
    LOG_LEVEL: str = field(
        default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO")
    )

    @property
    def webhook_url(self) -> str:
        if self.RENDER_APP_NAME:
            return f"https://{self.RENDER_APP_NAME}.onrender.com/webhook"
        return ""

    def validate(self) -> None:
        """Raise on missing or obviously invalid values."""
        required = {
            "TELEGRAM_BOT_TOKEN": self.TELEGRAM_BOT_TOKEN,
            "GEMINI_API_KEY": self.GEMINI_API_KEY,
            "SUPABASE_URL": self.SUPABASE_URL,
            "SUPABASE_KEY": self.SUPABASE_KEY,
            "UPI_ID": self.UPI_ID,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            print(f"[FATAL] Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
            sys.exit(1)

        if self.ADMIN_CHAT_ID == 0:
            print("[FATAL] ADMIN_CHAT_ID must be a non-zero integer.", file=sys.stderr)
            sys.exit(1)

        # Ensure temp/logs dirs exist
        for d in (self.LOG_DIR, self.VOICE_TEMP_DIR):
            os.makedirs(d, exist_ok=True)


def _load() -> Config:
    """Build and validate the singleton config. Exits on misconfiguration."""
    required_keys = [
        "TELEGRAM_BOT_TOKEN",
        "GEMINI_API_KEY",
        "SUPABASE_URL",
        "SUPABASE_KEY",
        "ADMIN_CHAT_ID",
        "UPI_ID",
    ]
    missing = [k for k in required_keys if not os.environ.get(k)]
    if missing:
        print(f"[FATAL] Missing environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    cfg = Config()
    cfg.validate()
    return cfg


settings: Config = _load()
