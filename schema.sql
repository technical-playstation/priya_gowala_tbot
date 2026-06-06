-- ============================================================
-- PriyaBot Supabase Schema
-- Run this in Supabase SQL Editor:
-- https://app.supabase.com → SQL Editor → New Query
-- ============================================================

-- ── Users ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id                  BIGSERIAL PRIMARY KEY,
    chat_id             BIGINT      NOT NULL UNIQUE,
    username            TEXT        DEFAULT '',
    first_name          TEXT        DEFAULT '',
    is_subscribed       BOOLEAN     NOT NULL DEFAULT FALSE,
    subscription_end    TIMESTAMPTZ,
    messages_remaining  INTEGER     NOT NULL DEFAULT 5,   -- free trial messages
    total_messages_used INTEGER     NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_chat_id ON users (chat_id);

-- ── Payment sessions ──────────────────────────────────────────────────────────
-- Short-lived UPI QR sessions
CREATE TABLE IF NOT EXISTS payment_sessions (
    id          BIGSERIAL PRIMARY KEY,
    session_id  UUID        NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    chat_id     BIGINT      NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
    plan_key    TEXT        NOT NULL,
    amount      INTEGER     NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','completed','expired','cancelled')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_payment_sessions_chat_id   ON payment_sessions (chat_id);
CREATE INDEX IF NOT EXISTS idx_payment_sessions_status    ON payment_sessions (status);
CREATE INDEX IF NOT EXISTS idx_payment_sessions_expires   ON payment_sessions (expires_at);

-- ── Pending payments (awaiting admin UTR verification) ────────────────────────
CREATE TABLE IF NOT EXISTS pending_payments (
    id           BIGSERIAL PRIMARY KEY,
    chat_id      BIGINT      NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
    session_id   UUID        NOT NULL REFERENCES payment_sessions(session_id),
    utr          TEXT        NOT NULL UNIQUE,          -- prevents duplicate submissions
    amount       INTEGER     NOT NULL,
    plan_key     TEXT        NOT NULL,
    status       TEXT        NOT NULL DEFAULT 'pending_admin'
                             CHECK (status IN ('pending_admin','approved','rejected')),
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at  TIMESTAMPTZ,
    reviewed_by  BIGINT                                -- admin chat_id
);

CREATE INDEX IF NOT EXISTS idx_pending_payments_chat_id ON pending_payments (chat_id);
CREATE INDEX IF NOT EXISTS idx_pending_payments_utr     ON pending_payments (utr);
CREATE INDEX IF NOT EXISTS idx_pending_payments_status  ON pending_payments (status);

-- ── Conversation memory ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS conversation_memory (
    id         BIGSERIAL PRIMARY KEY,
    chat_id    BIGINT NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
    role       TEXT   NOT NULL CHECK (role IN ('user','assistant')),
    content    TEXT   NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conv_memory_chat_id    ON conversation_memory (chat_id);
CREATE INDEX IF NOT EXISTS idx_conv_memory_created_at ON conversation_memory (chat_id, created_at);

-- ── Audit logs ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_logs (
    id         BIGSERIAL PRIMARY KEY,
    chat_id    BIGINT NOT NULL,
    event      TEXT   NOT NULL,
    details    JSONB  DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_chat_id    ON audit_logs (chat_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_event      ON audit_logs (event);
CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs (created_at);

-- ── Row Level Security (optional but recommended) ─────────────────────────────
-- Enable RLS and only allow service-role key access (your backend uses service key)
ALTER TABLE users               ENABLE ROW LEVEL SECURITY;
ALTER TABLE payment_sessions    ENABLE ROW LEVEL SECURITY;
ALTER TABLE pending_payments    ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversation_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_logs          ENABLE ROW LEVEL SECURITY;

-- Allow service role full access (used by backend)
-- The supabase service_role key bypasses RLS automatically.
-- If you use the anon key anywhere, add policies here.
