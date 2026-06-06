"""
payment.py - UPI payment workflow.

Flow:
  1. User picks a plan → create_payment_session() → generate_upi_qr()
  2. User pays and submits UTR → submit_utr()
  3. Admin receives notification and approves / rejects
  4. On approval → apply_subscription() is called in database.py
"""

import io
import os
import urllib.parse
import uuid
from typing import Optional, Tuple

import qrcode

from config import settings
from database import (
    create_payment_session,
    get_payment_session,
    submit_payment,
    utr_exists,
)
from logger import payment_logger as log


# ─────────────────────────────────────────────────────────────────────────────
# UPI QR generation
# ─────────────────────────────────────────────────────────────────────────────

def _build_upi_url(amount: int, note: str, transaction_ref: str) -> str:
    """Construct a UPI deep-link URL."""
    params = {
        "pa": settings.UPI_ID,
        "pn": "PriyaBot",
        "am": str(amount),
        "cu": "INR",
        "tn": note,
        "tr": transaction_ref,
    }
    return "upi://pay?" + urllib.parse.urlencode(params)


def generate_upi_qr(amount: int, session_id: str) -> bytes:
    """Return PNG bytes of a UPI QR code for the given amount/session."""
    upi_url = _build_upi_url(
        amount=amount,
        note=f"PriyaBot subscription {session_id[:8]}",
        transaction_ref=session_id[:16],
    )
    qr = qrcode.QRCode(
        version=2,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=8,
        border=4,
    )
    qr.add_data(upi_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    log.info("QR generated: session=%s  amount=%s", session_id, amount)
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# Session helpers
# ─────────────────────────────────────────────────────────────────────────────

def initiate_payment(chat_id: int, plan_key: str) -> Tuple[dict, bytes]:
    """
    Create a payment session and generate QR.
    Returns (session_dict, qr_png_bytes).
    """
    plan = settings.PLANS.get(plan_key)
    if not plan:
        raise ValueError(f"Invalid plan key: {plan_key}")

    session = create_payment_session(chat_id, plan_key, plan["price"])
    qr_bytes = generate_upi_qr(plan["price"], session["session_id"])
    log.info("Payment initiated: chat_id=%s  plan=%s  session=%s", chat_id, plan_key, session["session_id"])
    return session, qr_bytes


def validate_utr(utr: str) -> Tuple[bool, str]:
    """Light-weight UTR format validation. Returns (ok, error_message)."""
    utr = utr.strip().upper()
    if not utr:
        return False, "UTR cannot be empty."
    if len(utr) < 10 or len(utr) > 22:
        return False, "UTR should be 10–22 characters."
    if not utr.isalnum():
        return False, "UTR should contain only letters and numbers."
    if utr_exists(utr):
        return False, "This UTR has already been submitted. Contact support if this is an error."
    return True, ""


def submit_utr(chat_id: int, session_id: str, utr: str) -> dict:
    """
    Record a UTR submission for admin review.
    Raises ValueError on duplicate UTR or missing session.
    """
    session = get_payment_session(session_id)
    if not session:
        raise ValueError("Payment session not found or expired.")
    if session["status"] not in ("pending",):
        raise ValueError(f"Payment session is no longer active (status: {session['status']}).")

    ok, msg = validate_utr(utr)
    if not ok:
        raise ValueError(msg)

    record = submit_payment(
        chat_id=chat_id,
        session_id=session_id,
        utr=utr.strip().upper(),
        amount=session["amount"],
        plan_key=session["plan_key"],
    )
    log.info("UTR submitted for review: chat_id=%s  UTR=%s", chat_id, utr)
    return record
