-- Durable execution command chain.
--
-- This migration is forward-only.  The v3_* tables are intentionally separate
-- from the legacy in-memory/compatibility tables until the complete store
-- adapter and replay tests are ready.  No runtime may treat this schema as
-- active merely because the migration exists.

CREATE TABLE IF NOT EXISTS v3_order_intents (
    intent_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    client_order_id TEXT UNIQUE,
    account_venue_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    quantity TEXT NOT NULL,
    price TEXT,
    time_in_force TEXT NOT NULL,
    payload JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (state IN ('PENDING', 'SENDING', 'SENT', 'ACKED', 'UNKNOWN', 'FAILED', 'DEAD_LETTER')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS v3_risk_approvals (
    approval_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL UNIQUE REFERENCES v3_order_intents(intent_id),
    decision TEXT NOT NULL CHECK (decision IN ('APPROVED', 'REJECTED')),
    signature TEXT NOT NULL,
    proposal_hash TEXT NOT NULL,
    intent_hash TEXT NOT NULL,
    account_snapshot_hash TEXT NOT NULL,
    risk_snapshot_hash TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    nonce TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS v3_transactional_outbox (
    message_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL UNIQUE REFERENCES v3_order_intents(intent_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    client_order_id TEXT UNIQUE,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'SENDING', 'SENT', 'ACKED', 'UNKNOWN', 'FAILED', 'DEAD_LETTER')),
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    fencing_token BIGINT NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 10,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_error TEXT,
    dead_letter_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TIMESTAMPTZ,
    acked_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_v3_outbox_claim
    ON v3_transactional_outbox(status, next_attempt_at, created_at)
    WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_v3_outbox_lease
    ON v3_transactional_outbox(lease_until, fencing_token)
    WHERE status IN ('SENDING', 'SENT');

CREATE TABLE IF NOT EXISTS v3_outbox_events (
    event_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES v3_transactional_outbox(message_id),
    intent_id TEXT NOT NULL REFERENCES v3_order_intents(intent_id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    lease_owner TEXT,
    fencing_token BIGINT NOT NULL DEFAULT 0,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_v3_outbox_events_message
    ON v3_outbox_events(message_id, occurred_at, event_id);
