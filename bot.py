"""
bot.py - Production Telegram AI Companion Bot.

Runs as a webhook server (Flask + python-telegram-bot).
Handles all commands, conversation, voice notes, payments, and admin actions.
"""

import asyncio
import os
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
from logger import bot_logger as log, admin_logger
from payment import initiate_payment, submit_utr, validate_utr
from utils.helpers import days_remaining, format_subscription_end, truncate
from utils.rate_limiter import rate_limiter
from voice_engine import cleanup_voice_file, generate_voice

# ─────────────────────────────────────────────────────────────────────────────
# Flask health-check app (runs alongside PTB webhook)
# ─────────────────────────────────────────────────────────────────────────────

flask_app = Flask(__name__)


@flask_app.route("/health", methods=["GET"])
def health():
    ok = db.health_check()
    status = "ok" if ok else "degraded"
    return jsonify({"status": status, "db": ok}), 200 if ok else 503


@flask_app.route("/", methods=["GET"])
def index():
    return jsonify({"service": "PriyaBot", "status": "running"}), 200


# ─────────────────────────────────────────────────────────────────────────────
# Decorators
# ─────────────────────────────────────────────────────────────────────────────

def require_subscription(func):
    """Block handler if user has no active subscription."""
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not db.is_subscribed(chat_id):
            await update.effective_message.reply_text(
                "🔒 This feature requires an active subscription.\n"
                "Use /subscription to view plans and subscribe."
            )
            return
        return await func(update, ctx)
    return wrapper


def rate_limited(func):
    """Drop messages from users who exceed the rate limit."""
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not rate_limiter.is_allowed(chat_id):
            await update.effective_message.reply_text(
                "⏳ You're sending messages too fast. Please slow down."
            )
            return
        return await func(update, ctx)
    return wrapper


def require_admin(func):
    """Restrict handler to ADMIN_CHAT_ID only."""
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if chat_id != settings.ADMIN_CHAT_ID:
            await update.effective_message.reply_text("⛔ Admin only.")
            return
        return await func(update, ctx)
    return wrapper


# ─────────────────────────────────────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id

    db_user = db.get_or_create_user(
        chat_id,
        username=user.username,
        first_name=user.first_name,
    )

    name = user.first_name or "there"
    subscribed = db.is_subscribed(chat_id)
    sub_status = "✅ Active" if subscribed else "❌ No active subscription"

    text = (
        f"👋 Hey {name}! I'm <b>Priya</b>, your AI companion.\n\n"
        f"💬 Chat with me anytime — I speak Hindi, Hinglish, Bengali & English.\n"
        f"🎙️ Premium subscribers get voice notes too!\n\n"
        f"📋 Subscription: {sub_status}\n\n"
        f"Commands:\n"
        f"/help — What I can do\n"
        f"/subscription — Plans & subscribe\n"
        f"/status — Your account\n"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💎 Subscribe", callback_data="show_plans"),
        InlineKeyboardButton("❓ Help", callback_data="help"),
    ]])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    log.info("/start: chat_id=%s  user=%s", chat_id, user.username)


# ─────────────────────────────────────────────────────────────────────────────
# /help
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 <b>PriyaBot Help</b>\n\n"
        "<b>Free:</b>\n"
        "• Just send a message and I'll reply!\n\n"
        "<b>Premium:</b>\n"
        "• 🎙️ Voice notes — say <code>voice: your message</code>\n"
        "• Longer conversation memory\n"
        "• Priority responses\n\n"
        "<b>Commands:</b>\n"
        "/start — Welcome message\n"
        "/help — This menu\n"
        "/status — Account & subscription info\n"
        "/subscription — View plans & subscribe\n\n"
        "<b>Voice Notes:</b>\n"
        "Prefix your message with <code>voice:</code> to get a voice reply.\n"
        "Example: <code>voice: Kya haal hai?</code>"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────────────────────────────────────────
# /status
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = db.get_user(chat_id)
    if not user:
        await update.message.reply_text("Use /start first!")
        return

    subscribed = db.is_subscribed(chat_id)
    end_str = format_subscription_end(user.get("subscription_end"))
    days_left = days_remaining(user.get("subscription_end"))
    remaining_msgs = user.get("messages_remaining", 0)
    total_used = user.get("total_messages_used", 0)

    text = (
        f"📊 <b>Your Account</b>\n\n"
        f"👤 Name: {user.get('first_name', '—')}\n"
        f"🆔 Chat ID: <code>{chat_id}</code>\n\n"
        f"📋 Subscription: {'✅ Active' if subscribed else '❌ Inactive'}\n"
        f"📅 Expires: {end_str}\n"
        f"⏳ Days remaining: {days_left}\n"
        f"💬 Messages left: {remaining_msgs}\n"
        f"📈 Total messages used: {total_used}\n"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💎 Subscribe / Renew", callback_data="show_plans"),
    ]])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ─────────────────────────────────────────────────────────────────────────────
# /subscription — plan picker
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_subscription(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _show_plans(update)


async def _show_plans(update: Update):
    text = (
        "💎 <b>Subscription Plans</b>\n\n"
        "Choose a plan to get started:\n\n"
    )
    for key, plan in settings.PLANS.items():
        text += f"• {plan['label']}\n"

    buttons = [
        [InlineKeyboardButton(plan["label"], callback_data=f"buy_{key}")]
        for key, plan in settings.PLANS.items()
    ]
    kb = InlineKeyboardMarkup(buttons)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ─────────────────────────────────────────────────────────────────────────────
# /admin — admin panel
# ─────────────────────────────────────────────────────────────────────────────

@require_admin
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pending = db.get_pending_payments()
    if not pending:
        await update.message.reply_text("✅ No pending payments.")
        return

    admin_logger.info("Admin viewed %d pending payments", len(pending))

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
# Callback query handler
# ─────────────────────────────────────────────────────────────────────────────

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id

    # ── Plan display ──────────────────────────────────────────────────────────
    if data == "show_plans":
        await _show_plans(update)

    elif data == "help":
        await cmd_help(update, ctx)

    # ── Buy a plan ────────────────────────────────────────────────────────────
    elif data.startswith("buy_"):
        plan_key = data[4:]
        if plan_key not in settings.PLANS:
            await query.edit_message_text("Unknown plan.")
            return

        plan = settings.PLANS[plan_key]
        try:
            session, qr_bytes = initiate_payment(chat_id, plan_key)
        except Exception as exc:
            log.error("Payment initiation failed: %s", exc)
            await query.edit_message_text("⚠️ Payment setup failed. Try again later.")
            return

        caption = (
            f"💳 <b>Pay ₹{plan['price']} via UPI</b>\n\n"
            f"📤 UPI ID: <code>{settings.UPI_ID}</code>\n"
            f"💰 Amount: <b>₹{plan['price']}</b>\n"
            f"📦 Plan: {plan['label']}\n\n"
            f"1️⃣ Scan QR or pay to UPI ID above\n"
            f"2️⃣ Copy your <b>UTR / Transaction ID</b>\n"
            f"3️⃣ Reply with: <code>utr:{session['session_id']}:{'{YOUR_UTR}'}</code>\n\n"
            f"⏰ This QR expires in {settings.PAYMENT_SESSION_EXPIRY_MINUTES} minutes.\n"
            f"🆔 Session: <code>{session['session_id'][:12]}…</code>"
        )
        await update.effective_message.reply_photo(
            photo=qr_bytes,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )

    # ── Admin: approve ────────────────────────────────────────────────────────
    elif data.startswith("approve_") and chat_id == settings.ADMIN_CHAT_ID:
        payment_id = int(data[8:])
        payment = db.approve_payment(payment_id, chat_id)
        if not payment:
            await query.edit_message_text("Payment not found.")
            return

        await query.edit_message_text(f"✅ Payment #{payment_id} approved.")

        # Notify user
        try:
            plan = settings.PLANS.get(payment["plan_key"], {})
            await ctx.bot.send_message(
                chat_id=payment["chat_id"],
                text=(
                    f"🎉 <b>Payment Approved!</b>\n\n"
                    f"Your subscription has been activated: {plan.get('label', payment['plan_key'])}\n"
                    f"Use /status to see your updated subscription."
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            log.warning("Could not notify user after approval: %s", exc)

        admin_logger.info("Approved payment #%s for chat_id=%s", payment_id, payment["chat_id"])

    # ── Admin: reject ─────────────────────────────────────────────────────────
    elif data.startswith("reject_") and chat_id == settings.ADMIN_CHAT_ID:
        payment_id = int(data[7:])
        payment = db.reject_payment(payment_id, chat_id)
        if not payment:
            await query.edit_message_text("Payment not found.")
            return

        await query.edit_message_text(f"❌ Payment #{payment_id} rejected.")

        # Notify user
        try:
            await ctx.bot.send_message(
                chat_id=payment["chat_id"],
                text=(
                    "❌ <b>Payment Rejected</b>\n\n"
                    "Your payment could not be verified. "
                    "Please double-check the UTR and try again, or contact support."
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            log.warning("Could not notify user after rejection: %s", exc)

        admin_logger.info("Rejected payment #%s for chat_id=%s", payment_id, payment["chat_id"])


# ─────────────────────────────────────────────────────────────────────────────
# Message handler — text messages
# ─────────────────────────────────────────────────────────────────────────────

@rate_limited
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat_id = update.effective_chat.id
    text = (message.text or "").strip()

    if not text:
        return

    # ── UTR submission: utr:<session_id>:<utr_code> ───────────────────────────
    if text.lower().startswith("utr:"):
        await _handle_utr_submission(update, ctx, text)
        return

    # ── Voice request: voice: <message> ──────────────────────────────────────
    want_voice = text.lower().startswith("voice:")
    if want_voice:
        if not db.is_subscribed(chat_id):
            await message.reply_text(
                "🔒 Voice notes are a premium feature.\n"
                "Use /subscription to subscribe."
            )
            return
        text = text[6:].strip()
        if not text:
            await message.reply_text("Please include a message after 'voice:'")
            return

    # ── Quota check ───────────────────────────────────────────────────────────
    user = db.get_or_create_user(
        chat_id,
        username=update.effective_user.username,
        first_name=update.effective_user.first_name,
    )
    if not db.is_subscribed(chat_id) and user.get("messages_remaining", 0) <= 0:
        await message.reply_text(
            "💬 You've used your free messages.\n"
            "Subscribe with /subscription to keep chatting!"
        )
        return

    # ── AI response ───────────────────────────────────────────────────────────
    await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    history = db.get_conversation_history(chat_id)

    try:
        ai_reply = await generate_response(text, history, chat_id)
    except Exception as exc:
        log.error("AI generation failed: chat_id=%s  error=%s", chat_id, exc)
        await message.reply_text(
            "⚠️ I couldn't generate a reply right now. Please try again in a moment."
        )
        return

    # Persist conversation
    db.save_message(chat_id, "user", text)
    db.save_message(chat_id, "assistant", ai_reply)
    db.decrement_message(chat_id)

    # ── Voice note ────────────────────────────────────────────────────────────
    if want_voice:
        await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.RECORD_VOICE)
        voice_path = await generate_voice(ai_reply, chat_id)
        if voice_path:
            try:
                with open(voice_path, "rb") as f:
                    await message.reply_voice(voice=f)
            except Exception as exc:
                log.error("Failed to send voice: %s", exc)
                await message.reply_text(ai_reply)
            finally:
                cleanup_voice_file(voice_path)
        else:
            await message.reply_text(ai_reply)
    else:
        await message.reply_text(ai_reply)


async def _handle_utr_submission(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    """Parse and submit a UTR from user message."""
    chat_id = update.effective_chat.id
    parts = text.split(":", 2)

    if len(parts) != 3:
        await update.effective_message.reply_text(
            "⚠️ Invalid format. Use:\n"
            "<code>utr:SESSION_ID:YOUR_UTR</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    _, session_id, utr = parts
    session_id = session_id.strip()
    utr = utr.strip()

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
        "An admin will verify your payment shortly (usually within a few hours).\n"
        "You'll be notified once approved.",
        parse_mode=ParseMode.HTML,
    )

    # Notify admin
    try:
        user = update.effective_user
        await ctx.bot.send_message(
            chat_id=settings.ADMIN_CHAT_ID,
            text=(
                f"🔔 <b>New Payment Submission</b>\n\n"
                f"👤 User: @{user.username or '—'} (<code>{chat_id}</code>)\n"
                f"🔖 UTR: <code>{utr}</code>\n"
                f"🆔 Session: <code>{session_id[:16]}</code>\n\n"
                "Use /admin to review."
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        log.warning("Could not notify admin of UTR submission: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Error handler
# ─────────────────────────────────────────────────────────────────────────────

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled exception: %s\n%s", ctx.error, traceback.format_exc())
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong. Please try again."
            )
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Application builder
# ─────────────────────────────────────────────────────────────────────────────

def build_application() -> Application:
    app = (
        Application.builder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("help",         cmd_help))
    app.add_handler(CommandHandler("status",       cmd_status))
    app.add_handler(CommandHandler("subscription", cmd_subscription))
    app.add_handler(CommandHandler("admin",        cmd_admin))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    return app


# ─────────────────────────────────────────────────────────────────────────────
# Webhook endpoint
# ─────────────────────────────────────────────────────────────────────────────

_ptb_app: Optional[Application] = None


async def _process_update(data: dict):
    global _ptb_app
    if _ptb_app is None:
        _ptb_app = build_application()
        await _ptb_app.initialize()

    update = Update.de_json(data, _ptb_app.bot)
    await _ptb_app.process_update(update)


@flask_app.route("/webhook", methods=["POST"])
def webhook():
    # Optional: verify Telegram secret token header
    secret = settings.WEBHOOK_SECRET
    if secret:
        header_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if header_token != secret:
            log.warning("Webhook received with invalid secret token")
            return Response("Forbidden", status=403)

    data = request.get_json(force=True, silent=True)
    if not data:
        return Response("Bad Request", status=400)

    try:
        asyncio.run(_process_update(data))
    except Exception as exc:
        log.error("Webhook processing error: %s\n%s", exc, traceback.format_exc())

    return Response("OK", status=200)


# ─────────────────────────────────────────────────────────────────────────────
# Startup: register webhook
# ─────────────────────────────────────────────────────────────────────────────

async def _set_webhook():
    if not settings.webhook_url:
        log.warning("RENDER_APP_NAME not set — skipping webhook registration")
        return
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    kwargs = {"url": settings.webhook_url}
    if settings.WEBHOOK_SECRET:
        kwargs["secret_token"] = settings.WEBHOOK_SECRET
    await bot.set_webhook(**kwargs)
    log.info("Webhook registered: %s", settings.webhook_url)


def setup_webhook():
    try:
        asyncio.run(_set_webhook())
    except Exception as exc:
        log.error("Webhook setup failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point (Gunicorn imports flask_app directly)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    setup_webhook()
    flask_app.run(host="0.0.0.0", port=settings.PORT, debug=False)
