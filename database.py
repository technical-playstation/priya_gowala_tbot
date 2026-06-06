"""
database.py - Supabase database layer.

Tables (see schema.sql):
  users               – subscription state, message quota
  pending_payments    – awaiting UTR verification
  payment_sessions    – UPI QR sessions (expire after N minutes)
  conversation_memory – per-user message history
  audit_logs          – immutable trail of all important events
"""

import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from supabase import create_client, Client

from config import settings
from logger import db_logger as log

# ─────────────────────────────────────────────────────────────────────────────
# Client singleton
# ─────────────────────────────────────────────────────────────────────────────

_client: Optional[Client] = None


def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
        log.info("Supabase client initialised")
    return _client


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _retry(fn, attempts: int = 3, delay: float = 1.0) -> Any:
    """Synchronous retry wrapper for Supabase calls."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            log.warning("DB attempt %d/%d failed: %s", attempt, attempts, exc)
            if attempt < attempts:
                time.sleep(delay * attempt)
    log.error("DB operation failed after %d attempts: %s", attempts, last_exc)
    raise last_exc


# ─────────────────────────────────────────────────────────────────────────────
# Users
# ─────────────────────────────────────────────────────────────────────────────

def get_or_create_user(
    chat_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
) -> dict:
    db = get_client()

    def _fetch():
        return db.table("users").select("*").eq("chat_id", chat_id).execute()

    result = _retry(_fetch)
    if result.data:
        return result.data[0]

    # Create new user
    row = {
        "chat_id": chat_id,
        "username": username or "",
        "first_name": first_name or "",
        "is_subscribed": False,
        "subscription_end": None,
        "messages_remaining": 0,
        "total_messages_used": 0,
        "created_at": _now_utc(),
        "updated_at": _now_utc(),
    }

    def _insert():
        return db.table("users").insert(row).execute()

    inserted = _retry(_insert)
    log.info("New user created: %s", chat_id)
    audit_log(chat_id, "user_created", {"username": username})
    return inserted.data[0] if inserted.data else row


def get_user(chat_id: int) -> Optional[dict]:
    db = get_client()

    def _fetch():
        return db.table("users").select("*").eq("chat_id", chat_id).execute()

    result = _retry(_fetch)
    return result.data[0] if result.data else None


def is_subscribed(chat_id: int) -> bool:
    user = get_user(chat_id)
    if not user:
        return False
    if not user.get("is_subscribed"):
        return False
    end = user.get("subscription_end")
    if not end:
        return False
    try:
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return end_dt > datetime.now(timezone.utc)
    except Exception:
        return False


def decrement_message(chat_id: int) -> bool:
    """Atomically decrement messages_remaining. Returns False if quota exhausted."""
    user = get_user(chat_id)
    if not user:
        return False
    remaining = user.get("messages_remaining", 0)
    if remaining <= 0 and not is_subscribed(chat_id):
        return False

    db = get_client()
    new_remaining = max(0, remaining - 1)
    new_total = user.get("total_messages_used", 0) + 1

    def _update():
        return (
            db.table("users")
            .update({
                "messages_remaining": new_remaining,
                "total_messages_used": new_total,
                "updated_at": _now_utc(),
            })
            .eq("chat_id", chat_id)
            .execute()
        )

    _retry(_update)
    return True


def apply_subscription(chat_id: int, plan_key: str) -> dict:
    """Extend or start a subscription. Returns updated user row."""
    plan = settings.PLANS.get(plan_key)
    if not plan:
        raise ValueError(f"Unknown plan: {plan_key}")

    user = get_user(chat_id)
    now = datetime.now(timezone.utc)

    # Extend from current end if still active, else from now
    current_end_str = user.get("subscription_end") if user else None
    base = now
    if current_end_str:
        try:
            current_end = datetime.fromisoformat(current_end_str.replace("Z", "+00:00"))
            if current_end > now:
                base = current_end
        except Exception:
            pass

    new_end = base + timedelta(days=plan["days"])
    extra_messages = plan["days"] * 50  # 50 AI messages per day

    db = get_client()

    def _update():
        return (
            db.table("users")
            .update({
                "is_subscribed": True,
                "subscription_end": new_end.isoformat(),
                "messages_remaining": (user.get("messages_remaining", 0) if user else 0) + extra_messages,
                "updated_at": _now_utc(),
            })
            .eq("chat_id", chat_id)
            .execute()
        )

    result = _retry(_update)
    log.info("Subscription applied: chat_id=%s plan=%s end=%s", chat_id, plan_key, new_end)
    audit_log(chat_id, "subscription_applied", {"plan": plan_key, "new_end": str(new_end)})
    return result.data[0] if result.data else {}


# ─────────────────────────────────────────────────────────────────────────────
# Payment sessions
# ─────────────────────────────────────────────────────────────────────────────

def create_payment_session(chat_id: int, plan_key: str, amount: int) -> dict:
    db = get_client()
    session_id = str(uuid.uuid4())
    expires_at = (
        datetime.now(timezone.utc)
        + timedelta(minutes=settings.PAYMENT_SESSION_EXPIRY_MINUTES)
    ).isoformat()

    row = {
        "session_id": session_id,
        "chat_id": chat_id,
        "plan_key": plan_key,
        "amount": amount,
        "status": "pending",
        "created_at": _now_utc(),
        "expires_at": expires_at,
    }

    def _insert():
        return db.table("payment_sessions").insert(row).execute()

    _retry(_insert)
    log.info("Payment session created: %s  chat=%s  plan=%s  ₹%s", session_id, chat_id, plan_key, amount)
    audit_log(chat_id, "payment_session_created", {"session_id": session_id, "plan": plan_key})
    return row


def get_payment_session(session_id: str) -> Optional[dict]:
    db = get_client()

    def _fetch():
        return db.table("payment_sessions").select("*").eq("session_id", session_id).execute()

    result = _retry(_fetch)
    return result.data[0] if result.data else None


def expire_old_sessions() -> None:
    db = get_client()

    def _update():
        return (
            db.table("payment_sessions")
            .update({"status": "expired"})
            .lt("expires_at", _now_utc())
            .eq("status", "pending")
            .execute()
        )

    _retry(_update)


# ─────────────────────────────────────────────────────────────────────────────
# Pending payments (UTR verification)
# ─────────────────────────────────────────────────────────────────────────────

def utr_exists(utr: str) -> bool:
    """Prevent duplicate UTR submissions."""
    db = get_client()

    def _fetch():
        return db.table("pending_payments").select("id").eq("utr", utr).execute()

    result = _retry(_fetch)
    return bool(result.data)


def submit_payment(chat_id: int, session_id: str, utr: str, amount: int, plan_key: str) -> dict:
    if utr_exists(utr):
        raise ValueError("Duplicate UTR")

    db = get_client()
    row = {
        "chat_id": chat_id,
        "session_id": session_id,
        "utr": utr,
        "amount": amount,
        "plan_key": plan_key,
        "status": "pending_admin",
        "submitted_at": _now_utc(),
    }

    def _insert():
        return db.table("pending_payments").insert(row).execute()

    result = _retry(_insert)
    log.info("Payment submitted for review: chat=%s  UTR=%s  ₹%s", chat_id, utr, amount)
    audit_log(chat_id, "payment_submitted", {"utr": utr, "amount": amount, "plan": plan_key})
    return result.data[0] if result.data else row


def get_pending_payments() -> list[dict]:
    db = get_client()

    def _fetch():
        return (
            db.table("pending_payments")
            .select("*")
            .eq("status", "pending_admin")
            .order("submitted_at", desc=False)
            .execute()
        )

    result = _retry(_fetch)
    return result.data or []


def approve_payment(payment_id: int, admin_chat_id: int) -> Optional[dict]:
    db = get_client()

    def _fetch():
        return db.table("pending_payments").select("*").eq("id", payment_id).execute()

    result = _retry(_fetch)
    if not result.data:
        return None
    payment = result.data[0]

    # Mark approved
    def _update_payment():
        return (
            db.table("pending_payments")
            .update({"status": "approved", "reviewed_at": _now_utc(), "reviewed_by": admin_chat_id})
            .eq("id", payment_id)
            .execute()
        )

    _retry(_update_payment)

    # Apply subscription
    apply_subscription(payment["chat_id"], payment["plan_key"])

    # Mark session complete
    def _update_session():
        return (
            db.table("payment_sessions")
            .update({"status": "completed"})
            .eq("session_id", payment["session_id"])
            .execute()
        )

    _retry(_update_session)

    log.info("Payment approved: id=%s  chat=%s  plan=%s", payment_id, payment["chat_id"], payment["plan_key"])
    audit_log(payment["chat_id"], "payment_approved", {"payment_id": payment_id, "admin": admin_chat_id})
    return payment


def reject_payment(payment_id: int, admin_chat_id: int) -> Optional[dict]:
    db = get_client()

    def _fetch():
        return db.table("pending_payments").select("*").eq("id", payment_id).execute()

    result = _retry(_fetch)
    if not result.data:
        return None
    payment = result.data[0]

    def _update():
        return (
            db.table("pending_payments")
            .update({"status": "rejected", "reviewed_at": _now_utc(), "reviewed_by": admin_chat_id})
            .eq("id", payment_id)
            .execute()
        )

    _retry(_update)
    log.info("Payment rejected: id=%s  chat=%s", payment_id, payment["chat_id"])
    audit_log(payment["chat_id"], "payment_rejected", {"payment_id": payment_id, "admin": admin_chat_id})
    return payment


# ─────────────────────────────────────────────────────────────────────────────
# Conversation memory
# ─────────────────────────────────────────────────────────────────────────────

def get_conversation_history(chat_id: int, limit: int = None) -> list[dict]:
    limit = limit or settings.MAX_MEMORY_MESSAGES
    db = get_client()

    def _fetch():
        return (
            db.table("conversation_memory")
            .select("role, content")
            .eq("chat_id", chat_id)
            .order("created_at", desc=False)
            .limit(limit)
            .execute()
        )

    result = _retry(_fetch)
    return result.data or []


def save_message(chat_id: int, role: str, content: str) -> None:
    db = get_client()
    row = {
        "chat_id": chat_id,
        "role": role,
        "content": content,
        "created_at": _now_utc(),
    }

    def _insert():
        return db.table("conversation_memory").insert(row).execute()

    _retry(_insert)

    # Prune old messages (keep last MAX_MEMORY_MESSAGES)
    _prune_memory(chat_id)


def _prune_memory(chat_id: int) -> None:
    db = get_client()
    limit = settings.MAX_MEMORY_MESSAGES

    def _fetch_ids():
        return (
            db.table("conversation_memory")
            .select("id")
            .eq("chat_id", chat_id)
            .order("created_at", desc=False)
            .execute()
        )

    result = _retry(_fetch_ids)
    rows = result.data or []
    if len(rows) <= limit:
        return
    ids_to_delete = [r["id"] for r in rows[: len(rows) - limit]]

    def _delete():
        return db.table("conversation_memory").delete().in_("id", ids_to_delete).execute()

    _retry(_delete)


def clear_conversation(chat_id: int) -> None:
    db = get_client()

    def _delete():
        return db.table("conversation_memory").delete().eq("chat_id", chat_id).execute()

    _retry(_delete)
    log.info("Conversation cleared: chat_id=%s", chat_id)


# ─────────────────────────────────────────────────────────────────────────────
# Audit log
# ─────────────────────────────────────────────────────────────────────────────

def audit_log(chat_id: int, event: str, details: dict = None) -> None:
    db = get_client()
    row = {
        "chat_id": chat_id,
        "event": event,
        "details": details or {},
        "created_at": _now_utc(),
    }
    try:
        db.table("audit_logs").insert(row).execute()
    except Exception as exc:
        log.warning("Audit log insert failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────────────────────────────────────

def health_check() -> bool:
    try:
        db = get_client()
        db.table("users").select("chat_id").limit(1).execute()
        return True
    except Exception as exc:
        log.error("DB health check failed: %s", exc)
        return False
