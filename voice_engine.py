"""
voice_engine.py - Premium voice note generation using edge-tts.

Supports Hindi, Hinglish, Bengali, and English.
Async-safe with temp file cleanup and retry.
"""

import asyncio
import os
import re
import uuid
from typing import Optional

import edge_tts

from config import settings
from logger import voice_logger as log

# ─────────────────────────────────────────────────────────────────────────────
# Voice map
# ─────────────────────────────────────────────────────────────────────────────

_VOICES = {
    "hindi":    "hi-IN-SwaraNeural",
    "hinglish": "hi-IN-MadhurNeural",
    "bengali":  "bn-IN-TanishaaNeural",
    "english":  "en-IN-NeerjaNeural",    # Indian English accent
}

_FALLBACK_VOICE = "en-IN-NeerjaNeural"


# ─────────────────────────────────────────────────────────────────────────────
# Language detection (mirrors ai_engine logic)
# ─────────────────────────────────────────────────────────────────────────────

def _detect_language(text: str) -> str:
    hindi_chars = sum(1 for c in text if "\u0900" <= c <= "\u097F")
    bengali_chars = sum(1 for c in text if "\u0980" <= c <= "\u09FF")
    total = max(len(text), 1)
    if hindi_chars / total > 0.15:
        return "hindi"
    if bengali_chars / total > 0.15:
        return "bengali"
    if hindi_chars > 0:
        return "hinglish"
    return "english"


# ─────────────────────────────────────────────────────────────────────────────
# TTS generation
# ─────────────────────────────────────────────────────────────────────────────

async def generate_voice(text: str, chat_id: int) -> Optional[str]:
    """
    Generate a voice note MP3 for the given text.
    Returns the local file path on success, None on failure.
    Caller is responsible for deleting the file after sending.
    """
    lang = _detect_language(text)
    voice = _VOICES.get(lang, _FALLBACK_VOICE)
    filename = os.path.join(settings.VOICE_TEMP_DIR, f"voice_{chat_id}_{uuid.uuid4().hex[:8]}.mp3")

    log.info("Voice generation: chat_id=%s  lang=%s  voice=%s", chat_id, lang, voice)

    for attempt in range(1, settings.EDGE_TTS_RETRY_ATTEMPTS + 1):
        try:
            communicate = edge_tts.Communicate(text=text, voice=voice)
            await communicate.save(filename)
            if os.path.exists(filename) and os.path.getsize(filename) > 0:
                log.info("Voice saved: %s  (attempt %d)", filename, attempt)
                return filename
            log.warning("Voice file empty on attempt %d", attempt)
        except Exception as exc:
            log.warning("Voice attempt %d failed: %s", attempt, exc)
            # Try fallback voice on second attempt
            if attempt == 1 and voice != _FALLBACK_VOICE:
                voice = _FALLBACK_VOICE
                log.info("Falling back to default voice: %s", voice)

        if attempt < settings.EDGE_TTS_RETRY_ATTEMPTS:
            await asyncio.sleep(1.0 * attempt)

    log.error("Voice generation failed for chat_id=%s after %d attempts", chat_id, settings.EDGE_TTS_RETRY_ATTEMPTS)
    return None


def cleanup_voice_file(path: Optional[str]) -> None:
    """Safely delete a temp voice file."""
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
            log.debug("Temp voice file deleted: %s", path)
    except Exception as exc:
        log.warning("Failed to delete voice file %s: %s", path, exc)
