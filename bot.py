"""
bot.py - Production Telegram AI Companion Bot.

KEY FIXES vs previous version
──────────────────────────────
1. EVENT LOOP BUG FIXED:
   - asyncio.run() inside a sync Flask route creates a NEW loop each call,
     then closes it → "Event loop is closed" on the next httpx call that was
     still holding a reference to the old loop's transport.
   - Fix: one persistent asyncio event loop runs in a background thread.
     Flask routes submit coroutines to that loop via
     asyncio.run_coroutine_threadsafe() and block until done. The PTB
     Application and its httpx client live entirely inside that loop — they
     are never torn down between requests.

2. LANGUAGE SELECTION FLOW:
   - /start → language picker inline keyboard (no paywall, no subscription talk)
   - Callback "lang_<code>" saves preference and sends the welcome intro

3. 1500-MESSAGE FREE TIER:
   - New users start with messages_remaining = 1500
   - Every AI reply decrements by 1
   - At 0 (and no active subscription) → paywall message with /subscription CTA
"""

import asyncio
import os
import threading
import traceback
from functools import wraps
from typing import Optional

from flask import Flask, Response, jsonify, request
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database as db
from ai_engine import generate_response
from config import settings
from logger import admin_logger, bot_logger as log
from payment import initiate_payment, submit_utr
from utils.helpers import days_remaining, format_subscription_end
from utils.rate_limiter import rate_limiter
from voice_engine import cleanup_voice_file, generate_voice

# ─────────────────────────────────────────────────────────────────────────────
# PART 1 FIX — Persistent background event loop
# ─────────────────────────────────────────────────────────────────────────────
# One loop runs forever in a daemon thread.  All async work is submitted to it.
# The PTB Application (and its internal httpx client) are created once inside
# this loop and never closed between webhook requests.

_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
_loop_thread = threading.Thread(target=_loop.run_forever, daemon=True)
_loop_thread.start()

_ptb_app: Optional[Application] = None
_ptb_lock = threading.Lock()  # guard first-time initialisation


def _run(coro) -> None:
    """Submit a coroutine to the persistent loop and wait for completion."""
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    future.result()  # blocks the Flask worker thread until done


# ─────────────────────────────────────────────────────────────────────────────
# PTB Application — initialised once
# ─────────────────────────────────────────────────────────────────────────────

async def _init_ptb() -> Application:
    app = (
        Application.builder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        # PTB creates its own httpx.AsyncClient here; it lives in _loop
        .build()
    )
    _register_handlers(app)
    await app.initialize()   # opens the httpx session — stays open
    log.info("PTB Application initialised")
    return app


def get_ptb_app() -> Application:
    """Return the singleton PTB app, initialising it on first call."""
    global _ptb_app
    if _ptb_app is not None:
        return _ptb_app
    with _ptb_lock:
        if _ptb_app is None:
            future = asyncio.run_coroutine_threadsafe(_init_ptb(), _loop)
            _ptb_app = future.result()
    return _ptb_app


# ─────────────────────────────────────────────────────────────────────────────
# Flask app
# ─────────────────────────────────────────────────────────────────────────────

flask_app = Flask(__name__)


@flask_app.route("/health", methods=["GET"])
def health():
    ok = db.health_check()
    return jsonify({"status": "ok" if ok else "degraded", "db": ok}), 200 if ok else 503


@flask_app.route("/", methods=["GET"])
def index():
    return jsonify({"service": "PriyaBot", "status": "running"}), 200


@flask_app.route("/webhook", methods=["POST"])
def webhook():
    # Optional webhook secret verification
    if settings.WEBHOOK_SECRET:
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if token != settings.WEBHOOK_SECRET:
            log.warning("Webhook: invalid secret token")
            return Response("Forbidden", status=403)

    data = request.get_json(force=True, silent=True)
    if not data:
        return Response("Bad Request", status=400)

    try:
        app = get_ptb_app()
        _run(_dispatch(app, data))
    except Exception as exc:
        log.error("Webhook dispatch error: %s\n%s", exc, traceback.format_exc())

    return Response("OK", status=200)


async def _dispatch(app: Application, data: dict) -> None:
    update = Update.de_json(data, app.bot)
    await app.process_update(update)


# ─────────────────────────────────────────────────────────────────────────────
# Language config
# ─────────────────────────────────────────────────────────────────────────────

LANGUAGES = {
    "en": {"label": "🇬🇧 English",   "greeting": "Hey {name}! I'm Priya 💕 I'm your personal AI companion. Feel free to talk to me about anything — I'm here for you!"},
    "hi": {"label": "🇮🇳 Hindi",     "greeting": "नमस्ते {name}! मैं प्रिया हूँ 💕 आपकी अपनी AI साथी। मुझसे कुछ भी बात करो — मैं हमेशा आपके लिए यहाँ हूँ!"},
    "hh": {"label": "🌐 Hinglish",   "greeting": "Hey {name}! Main Priya hoon 💕 Tumhari apni AI companion. Kuch bhi baat karo — main hamesha tumhare liye yahan hoon!"},
    "bn": {"label": "🇧🇩 Bengali",   "greeting": "হ্যালো {name}! আমি প্রিয়া 💕 আপনার AI সঙ্গী। যা খুশি বলুন — আমি সব সময় আপনার পাশে আছি!"},
}

FREE_MESSAGE_LIMIT = 1500


# ─────────────────────────────────────────────────────────────────────────────
# Decorators
# ─────────────────────────────────────────────────────────────────────────────

def rate_limited(func):
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not rate_limiter.is_allowed(chat_id):
            await update.effective_message.reply_text(
                "⏳ Too many messages. Please slow down a little."
            )
            return
        return await func(update, ctx)
    return wrapper


def require_admin(func):
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != settings.ADMIN_CHAT_ID:
            await update.effective_message.reply_text("⛔ Admin only.")
            return
        return await func(update, ctx)
    return wrapper


# ─────────────────────────────────────────────────────────────────────────────
# PART 2 — /start → language picker (no paywall)
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id

    # Ensure user row exists (1500 free messages on first creation)
    db.get_or_create_user(chat_id, username=user.username, first_name=user.first_name)

    # Language picker — first thing the user sees, no subscription mention
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(LANGUAGES["en"]["label"], callback_data="lang_en"),
         InlineKeyboardButton(LANGUAGES["hi"]["label"], callback_data="lang_hi")],
        [InlineKeyboardButton(LANGUAGES["hh"]["label"], callback_data="lang_hh"),
         InlineKeyboardButton(LANGUAGES["bn"]["label"], callback_data="lang_bn")],
    ])
    await update.message.reply_text(
        "👋 Welcome! Please choose your preferred language:\n\n"
        "अपनी भाषा चुनें / আপনার ভাষা বেছে নিন",
        reply_markup=kb,
    )
    log.info("/start: chat_id=%s  user=%s", chat_id, user.username)


# ─────────────────────────────────────────────────────────────────────────────
# /help
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = db.get_user(chat_id)
    lang = (user or {}).get("language", "en")

    if lang == "hi":
        text = (
            "🤖 <b>प्रिया बॉट — सहायता</b>\n\n"
            "• मुझसे कुछ भी बात करें\n"
            "• <code>voice: आपका संदेश</code> — आवाज़ नोट (Premium)\n"
            "• /status — आपका अकाउंट\n"
            "• /subscription — प्लान देखें\n"
        )
    elif lang == "hh":
        text = (
            "🤖 <b>Priya Bot — Help</b>\n\n"
            "• Mujhse kuch bhi baat karo\n"
            "• <code>voice: tumhara message</code> — voice note (Premium)\n"
            "• /status — tumhara account\n"
            "• /subscription — plans dekho\n"
        )
    elif lang == "bn":
        text = (
            "🤖 <b>প্রিয়া বট — সহায়তা</b>\n\n"
            "• আমার সাথে যা খুশি কথা বলুন\n"
            "• <code>voice: আপনার বার্তা</code> — ভয়েস নোট (Premium)\n"
            "• /status — আপনার অ্যাকাউন্ট\n"
            "• /subscription — প্ল্যান দেখুন\n"
        )
    else:
        text = (
            "🤖 <b>Priya Bot — Help</b>\n\n"
            "• Chat with me about anything\n"
            "• <code>voice: your message</code> — voice note (Premium)\n"
            "• /status — your account\n"
            "• /subscription — view plans\n"
        )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────────────────────────────────────────
# /status
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = db.get_user(chat_id)
    if not user:
        await update.message.reply_text("Please send /start first.")
        return

    subscribed = db.is_subscribed(chat_id)
    end_str = format_subscription_end(user.get("subscription_end"))
    days_left = days_remaining(user.get("subscription_end"))
    remaining = user.get("messages_remaining", 0)
    total_used = user.get("total_messages_used", 0)
    lang = user.get("language", "en")
    lang_label = LANGUAGES.get(lang, LANGUAGES["en"])["label"]

    text = (
        f"📊 <b>Your Account</b>\n\n"
        f"👤 Name: {user.get('first_name', '—')}\n"
        f"🌐 Language: {lang_label}\n"
        f"🆔 Chat ID: <code>{chat_id}</code>\n\n"
        f"📋 Subscription: {'✅ Active' if subscribed else '❌ Inactive'}\n"
        f"📅 Expires: {end_str}\n"
        f"⏳ Days remaining: {days_left}\n"
        f"💬 Messages remaining: {remaining:,}\n"
        f"📈 Total messages used: {total_used:,}\n"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💎 Subscribe / Renew", callback_data="show_plans"),
    ]])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ─────────────────────────────────────────────────────────────────────────────
# /subscription
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_subscription(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _show_plans(update)


async def _show_plans(update: Update):
    lines = "💎 <b>Subscription Plans</b>\n\nChoose a plan to continue chatting:\n\n"
    for plan in settings.PLANS.values():
        lines += f"• {plan['label']}\n"
    buttons = [
        [InlineKeyboardButton(plan["label"], callback_data=f"buy_{key}")]
        for key, plan in settings.PLANS.items()
    ]
    await update.effective_message.reply_text(
        lines, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ─────────────────────────────────────────────────────────────────────────────
# /admin
# ─────────────────────────────────────────────────────────────────────────────

@require_admin
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pending = db.get_pending_payments()
    if not pending:
        await update.message.reply_text("✅ No pending payments.")
        return
    admin_logger.info("Admin reviewing %d pending payments", len(pending))
    for p in pending:
        text = (
            f"💳 <b>Payment #{p['id']}</b>\n"
            f"👤 Chat ID: <code>{p['chat_id']}</code>\n"
            f"📦 Plan: {p['plan_key']}\n"
            f"💰 Amount: ₹{p['amount']}\n"
            f"🔖 UTR: <code>{p['utr']}</code>\n"
            f"🕐 Submitted: {p['submitted_at']}\n"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{p['id']}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"reject_{p['id']}"),
        ]])
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ─────────────────────────────────────────────────────────────────────────────
# Callback query router
# ─────────────────────────────────────────────────────────────────────────────

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id

    # ── Language selection (from /start) ─────────────────────────────────────
    if data.startswith("lang_"):
        lang_code = data[5:]  # "en", "hi", "hh", "bn"
        if lang_code not in LANGUAGES:
            return
        db.set_user_language(chat_id, lang_code)
        name = update.effective_user.first_name or "there"
        greeting = LANGUAGES[lang_code]["greeting"].format(name=name)
        await query.edit_message_text(greeting)
        log.info("Language set: chat_id=%s  lang=%s", chat_id, lang_code)
        return

    # ── Show plans ────────────────────────────────────────────────────────────
    if data == "show_plans":
        await _show_plans(update)
        return

    if data == "help":
        await cmd_help(update, ctx)
        return

    # ── Buy a plan ────────────────────────────────────────────────────────────
    if data.startswith("buy_"):
        plan_key = data[4:]
        if plan_key not in settings.PLANS:
            await query.edit_message_text("Unknown plan.")
            return
        plan = settings.PLANS[plan_key]
        try:
            session, qr_bytes = initiate_payment(chat_id, plan_key)
        except Exception as exc:
            log.error("Payment initiation failed: %s", exc)
            await query.edit_message_text("⚠️ Payment setup failed. Please try again.")
            return

        caption = (
            f"💳 <b>Pay ₹{plan['price']} via UPI</b>\n\n"
            f"📤 UPI ID: <code>{settings.UPI_ID}</code>\n"
            f"💰 Amount: <b>₹{plan['price']}</b>\n"
            f"📦 Plan: {plan['label']}\n\n"
            f"1️⃣ Scan QR or pay to the UPI ID above\n"
            f"2️⃣ Copy your <b>UTR / Transaction ID</b>\n"
            f"3️⃣ Reply: <code>utr:{session['session_id']}:YOUR_UTR</code>\n\n"
            f"⏰ Expires in {settings.PAYMENT_SESSION_EXPIRY_MINUTES} minutes.\n"
            f"🆔 Session: <code>{session['session_id'][:12]}…</code>"
        )
        await update.effective_message.reply_photo(
            photo=qr_bytes, caption=caption, parse_mode=ParseMode.HTML,
        )
        return

    # ── Admin approve ─────────────────────────────────────────────────────────
    if data.startswith("approve_") and chat_id == settings.ADMIN_CHAT_ID:
        payment_id = int(data[8:])
        payment = db.approve_payment(payment_id, chat_id)
        if not payment:
            await query.edit_message_text("Payment not found.")
            return
        await query.edit_message_text(f"✅ Payment #{payment_id} approved.")
        try:
            plan = settings.PLANS.get(payment["plan_key"], {})
            await ctx.bot.send_message(
                chat_id=payment["chat_id"],
                text=(
                    f"🎉 <b>Payment Approved!</b>\n\n"
                    f"Your subscription is now active: {plan.get('label', payment['plan_key'])}\n"
                    "Use /status to see your updated account."
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            log.warning("Could not notify user after approval: %s", exc)
        admin_logger.info("Approved payment #%s", payment_id)
        return

    # ── Admin reject ──────────────────────────────────────────────────────────
    if data.startswith("reject_") and chat_id == settings.ADMIN_CHAT_ID:
        payment_id = int(data[7:])
        payment = db.reject_payment(payment_id, chat_id)
        if not payment:
            await query.edit_message_text("Payment not found.")
            return
        await query.edit_message_text(f"❌ Payment #{payment_id} rejected.")
        try:
            await ctx.bot.send_message(
                chat_id=payment["chat_id"],
                text=(
                    "❌ <b>Payment Rejected</b>\n\n"
                    "Your payment could not be verified. "
                    "Please re-check your UTR and try again, or contact support."
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            log.warning("Could not notify user after rejection: %s", exc)
        admin_logger.info("Rejected payment #%s", payment_id)
        return


# ─────────────────────────────────────────────────────────────────────────────
# PART 2 — Message handler with 1500-message free tier
# ─────────────────────────────────────────────────────────────────────────────

@rate_limited
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat_id = update.effective_chat.id
    text = (message.text or "").strip()
    if not text:
        return

    # ── UTR submission ────────────────────────────────────────────────────────
    if text.lower().startswith("utr:"):
        await _handle_utr(update, ctx, text)
        return

    # ── Ensure user exists ────────────────────────────────────────────────────
    user = db.get_or_create_user(
        chat_id,
        username=update.effective_user.username,
        first_name=update.effective_user.first_name,
    )

    # ── Language guard: if not set, nudge them to /start ─────────────────────
    if not user.get("language"):
        await message.reply_text("Please send /start to choose your language first.")
        return

    # ── Voice request prefix ──────────────────────────────────────────────────
    want_voice = text.lower().startswith("voice:")
    if want_voice:
        if not db.is_subscribed(chat_id):
            await message.reply_text(
                "🔒 Voice notes are a Premium feature.\n"
                "Use /subscription to subscribe."
            )
            return
        text = text[6:].strip()
        if not text:
            await message.reply_text("Please include a message after 'voice:'")
            return

    # ── 1500-message free-tier check ──────────────────────────────────────────
    remaining = user.get("messages_remaining", 0)
    subscribed = db.is_subscribed(chat_id)

    if remaining <= 0 and not subscribed:
        await _send_paywall(message, user.get("language", "en"))
        return

    # ── AI response ───────────────────────────────────────────────────────────
    await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    history = db.get_conversation_history(chat_id)

    try:
        ai_reply = await generate_response(text, history, chat_id)
    except Exception as exc:
        log.error("AI error: chat_id=%s  %s", chat_id, exc)
        await message.reply_text("⚠️ I couldn't reply right now. Please try again.")
        return

    # Persist and decrement
    db.save_message(chat_id, "user", text)
    db.save_message(chat_id, "assistant", ai_reply)
    db.decrement_message(chat_id)

    # Warn user when approaching limit (at 50 and 10 remaining)
    new_remaining = remaining - 1
    if not subscribed and new_remaining in (50, 10):
        ai_reply += (
            f"\n\n⚠️ <i>You have {new_remaining} free messages remaining. "
            "Use /subscription to continue after your limit.</i>"
        )

    # ── Voice note ────────────────────────────────────────────────────────────
    if want_voice:
        await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.RECORD_VOICE)
        voice_path = await generate_voice(ai_reply, chat_id)
        if voice_path:
            try:
                with open(voice_path, "rb") as f:
                    await message.reply_voice(voice=f)
            except Exception as exc:
                log.error("Voice send failed: %s", exc)
                await message.reply_text(ai_reply, parse_mode=ParseMode.HTML)
            finally:
                cleanup_voice_file(voice_path)
        else:
            await message.reply_text(ai_reply, parse_mode=ParseMode.HTML)
    else:
        await message.reply_text(ai_reply, parse_mode=ParseMode.HTML)


async def _send_paywall(message, lang: str = "en"):
    """Send the 1500-message limit / subscription paywall message."""
    if lang == "hi":
        text = (
            "💬 <b>आपकी 1500 मुफ्त बातचीत पूरी हो गई!</b>\n\n"
            "मुझसे बात करते रहने के लिए कृपया सदस्यता लें।\n\n"
            "👇 नीचे दिए गए बटन से प्लान चुनें:"
        )
    elif lang == "hh":
        text = (
            "💬 <b>Tumhare 1500 free messages khatam ho gaye!</b>\n\n"
            "Mujhse baat karte rehne ke liye subscribe karo.\n\n"
            "👇 Neeche se plan chuno:"
        )
    elif lang == "bn":
        text = (
            "💬 <b>আপনার ১৫০০ বিনামূল্যে বার্তা শেষ হয়ে গেছে!</b>\n\n"
            "আমার সাথে কথা বলতে চালিয়ে যেতে সদস্যতা নিন।\n\n"
            "👇 নীচে থেকে প্ল্যান বেছে নিন:"
        )
    else:
        text = (
            "💬 <b>You've reached your 1500 free message limit!</b>\n\n"
            "Subscribe to keep chatting with me — I miss you already 💕\n\n"
            "👇 Choose a plan below:"
        )
    buttons = [
        [InlineKeyboardButton(plan["label"], callback_data=f"buy_{key}")]
        for key, plan in settings.PLANS.items()
    ]
    buttons.append([InlineKeyboardButton("❓ Help", callback_data="help")])
    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ─────────────────────────────────────────────────────────────────────────────
# UTR submission helper
# ─────────────────────────────────────────────────────────────────────────────

async def _handle_utr(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    chat_id = update.effective_chat.id
    parts = text.split(":", 2)
    if len(parts) != 3:
        await update.effective_message.reply_text(
            "⚠️ Format: <code>utr:SESSION_ID:YOUR_UTR</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    _, session_id, utr = [p.strip() for p in parts]
    try:
        submit_utr(chat_id, session_id, utr)
    except ValueError as exc:
        await update.effective_message.reply_text(f"❌ {exc}")
        return
    except Exception as exc:
        log.error("UTR submission error: %s", exc)
        await update.effective_message.reply_text("⚠️ Submission failed. Please try again.")
        return

    await update.effective_message.reply_text(
        "✅ <b>UTR submitted for review!</b>\n\n"
        "An admin will verify your payment shortly. You'll be notified once approved.",
        parse_mode=ParseMode.HTML,
    )
    try:
        tg_user = update.effective_user
        await ctx.bot.send_message(
            chat_id=settings.ADMIN_CHAT_ID,
            text=(
                f"🔔 <b>New Payment Submission</b>\n\n"
                f"👤 @{tg_user.username or '—'} (<code>{chat_id}</code>)\n"
                f"🔖 UTR: <code>{utr}</code>\n"
                f"🆔 Session: <code>{session_id[:16]}</code>\n\n"
                "Use /admin to review."
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        log.warning("Admin notify failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Error handler
# ─────────────────────────────────────────────────────────────────────────────

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled exception: %s\n%s", ctx.error, traceback.format_exc())
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Something went wrong. Please try again.")
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Handler registration
# ─────────────────────────────────────────────────────────────────────────────

def _register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("help",         cmd_help))
    app.add_handler(CommandHandler("status",       cmd_status))
    app.add_handler(CommandHandler("subscription", cmd_subscription))
    app.add_handler(CommandHandler("admin",        cmd_admin))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)


# ─────────────────────────────────────────────────────────────────────────────
# Webhook registration (called from render.yaml start command)
# ─────────────────────────────────────────────────────────────────────────────

async def _set_webhook():
    if not settings.webhook_url:
        log.warning("RENDER_APP_NAME not set — skipping webhook registration")
        return
    async with Bot(token=settings.TELEGRAM_BOT_TOKEN) as bot:
        kwargs = {"url": settings.webhook_url}
        if settings.WEBHOOK_SECRET:
            kwargs["secret_token"] = settings.WEBHOOK_SECRET
        await bot.set_webhook(**kwargs)
    log.info("Webhook registered: %s", settings.webhook_url)


def setup_webhook():
    try:
        # Use the persistent loop so the Bot's httpx session shares it
        future = asyncio.run_coroutine_threadsafe(_set_webhook(), _loop)
        future.result(timeout=15)
    except Exception as exc:
        log.error("Webhook setup failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    setup_webhook()
    flask_app.run(host="0.0.0.0", port=settings.PORT, debug=False)
