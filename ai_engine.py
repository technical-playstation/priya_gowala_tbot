"""
ai_engine.py - Gemini AI integration.

Features:
  - Singleton model instance
  - Retry with exponential back-off
  - Conversation memory from database
  - Multilingual system prompt (Hindi / Hinglish / Bengali / English)
  - Response sanitisation
  - Structured logging
"""

import asyncio
import re
import time
from typing import Optional

import google.generativeai as genai

from config import settings
from logger import ai_logger as log

# ─────────────────────────────────────────────────────────────────────────────
# Singleton model
# ─────────────────────────────────────────────────────────────────────────────

_model: Optional[genai.GenerativeModel] = None


def _get_model() -> genai.GenerativeModel:
    global _model
    if _model is None:
        genai.configure(api_key=settings.GEMINI_API_KEY)
        _model = genai.GenerativeModel(
            model_name=settings.GEMINI_MODEL,
            generation_config=genai.GenerationConfig(
                max_output_tokens=settings.GEMINI_MAX_TOKENS,
                temperature=0.85,
                top_p=0.95,
            ),
        )
        log.info("Gemini model initialised: %s", settings.GEMINI_MODEL)
    return _model


# ─────────────────────────────────────────────────────────────────────────────
# System prompt
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """
You are Priya, a warm, caring, and playful AI companion. You speak naturally in
the same language the user writes in — Hindi, Hinglish (Hindi–English mix),
Bengali, or English. Keep replies concise (2–4 sentences for casual chat),
friendly, and personal. Never claim to be human if sincerely asked. Never
provide harmful, illegal, sexual, or dangerous content. Never share personal
data. Be emotionally supportive, curious, and fun. Remember context from
earlier in the conversation.
""".strip()


# ─────────────────────────────────────────────────────────────────────────────
# Language detection
# ─────────────────────────────────────────────────────────────────────────────

def _detect_language(text: str) -> str:
    """Best-effort language tag from Unicode ranges."""
    hindi_chars = sum(1 for c in text if "\u0900" <= c <= "\u097F")
    bengali_chars = sum(1 for c in text if "\u0980" <= c <= "\u09FF")
    total = max(len(text), 1)

    if hindi_chars / total > 0.15:
        return "hindi"
    if bengali_chars / total > 0.15:
        return "bengali"
    # Mixed latin + devanagari → hinglish
    if hindi_chars > 0:
        return "hinglish"
    return "english"


# ─────────────────────────────────────────────────────────────────────────────
# Response cleanup
# ─────────────────────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    """Remove markdown artifacts that look odd in Telegram plain-text."""
    # Strip bold/italic markers
    text = re.sub(r"\*{1,3}(.*?)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,2}(.*?)_{1,2}", r"\1", text)
    # Collapse excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Core generation
# ─────────────────────────────────────────────────────────────────────────────

def _build_prompt(history: list[dict], user_message: str) -> list[dict]:
    """Convert DB history + new message into Gemini content list."""
    contents = []

    # Inject system instruction as first user turn (Gemini 1.5 approach)
    contents.append({
        "role": "user",
        "parts": [{"text": _SYSTEM_PROMPT}],
    })
    contents.append({
        "role": "model",
        "parts": [{"text": "Understood! I am Priya and I am here for you. 😊"}],
    })

    for msg in history:
        role = "user" if msg["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})

    contents.append({"role": "user", "parts": [{"text": user_message}]})
    return contents


async def generate_response(
    user_message: str,
    history: list[dict],
    chat_id: int,
) -> str:
    """
    Generate an AI reply. Returns the cleaned response string.
    Raises on complete failure.
    """
    model = _get_model()
    contents = _build_prompt(history, user_message)
    lang = _detect_language(user_message)
    log.info("AI request: chat_id=%s  lang=%s  msg_len=%d", chat_id, lang, len(user_message))

    last_exc: Optional[Exception] = None
    for attempt in range(1, settings.GEMINI_RETRY_ATTEMPTS + 1):
        try:
            loop = asyncio.get_event_loop()
            response = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: model.generate_content(contents),
                ),
                timeout=settings.GEMINI_TIMEOUT,
            )
            text = response.text
            cleaned = _clean(text)
            log.info("AI response: chat_id=%s  attempt=%d  len=%d", chat_id, attempt, len(cleaned))
            return cleaned

        except asyncio.TimeoutError as exc:
            last_exc = exc
            log.warning("Gemini timeout on attempt %d for chat_id=%s", attempt, chat_id)
        except Exception as exc:
            last_exc = exc
            log.warning("Gemini error attempt %d: %s", attempt, exc)

        if attempt < settings.GEMINI_RETRY_ATTEMPTS:
            await asyncio.sleep(settings.GEMINI_RETRY_DELAY * attempt)

    log.error("AI generation failed for chat_id=%s after %d attempts: %s", chat_id, settings.GEMINI_RETRY_ATTEMPTS, last_exc)
    raise RuntimeError(f"AI unavailable: {last_exc}")
