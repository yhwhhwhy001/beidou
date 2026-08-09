-- V3 fact-chain schema.  This migration is forward-only and is not a
-- permission to switch the runtime to PostgreSQL before the full store
-- adapter and replay tests pass.

CREATE TABLE IF NOT EXISTS v3_reconciliation_snapshots (
    snapshot_id TEXT NOT NULL,
    side TEXT NOT NULL,
    account_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    balance_amount NUMERIC(30, 8) NOT NULL,
    balance_currency TEXT NOT NULL,
    balance_decimals INTEGER NOT NULL DEFAULT 8,
    positions JSONB NOT NULL DEFAULT '{}'::jsonb,
    open_orders JSONB NOT NULL DEFAULT '[]'::jsonb,
    margin_amount NUMERIC(30, 8),
    margin_currency TEXT,
    margin_decimals INTEGER,
    captured_at TIMESTAMPTZ NOT NULL,
    correlation_id TEXT,
    source TEXT NOT NULL,
    fact_version TEXT NOT NULL,
    complete BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (snapshot_id, side)
);

CREATE INDEX IF NOT EXISTS idx_v3_recon_snapshots_key
    ON v3_reconciliation_snapshots(account_id, venue_id, side, captured_at);

CREATE TABLE IF NOT EXISTS v3_reconciliation_results (
    result_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    status TEXT NOT NULL,
    matched BOOLEAN NOT NULL,
    differences JSONB NOT NULL DEFAULT '[]'::jsonb,
    checked_at TIMESTAMPTZ NOT NULL,
    system_snapshot_id TEXT,
    exchange_snapshot_id TEXT,
    event_snapshot_id TEXT
);

CREATE TABLE IF NOT EXISTS v3_user_stream_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    sequence BIGINT,
    event_time_ms BIGINT NOT NULL,
    transaction_time_ms BIGINT,
    order_id TEXT,
    client_order_id TEXT,
    symbol TEXT,
    side TEXT,
    order_type TEXT,
    order_status TEXT,
    execution_type TEXT,
    original_quantity TEXT,
    cumulative_quantity TEXT,
    last_quantity TEXT,
    last_price TEXT,
    average_price TEXT,
    trade_id TEXT,
    commission_amount TEXT,
    commission_currency TEXT,
    realized_pnl_amount TEXT,
    raw_event JSONB NOT NULL,
    continuity_status TEXT NOT NULL,
    applied_state TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (applied_state IN ('PENDING', 'APPLIED')),
    received_at TIMESTAMPTZ NOT NULL,
    applied_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_v3_user_stream_events_time
    ON v3_user_stream_events(event_time_ms, event_id);
CREATE INDEX IF NOT EXISTS idx_v3_user_stream_events_order
    ON v3_user_stream_events(order_id, event_time_ms);

CREATE TABLE IF NOT EXISTS v3_user_stream_projections (
    account_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    balance_amount NUMERIC(30, 8) NOT NULL,
    balance_currency TEXT NOT NULL,
    balance_decimals INTEGER NOT NULL DEFAULT 8,
    positions JSONB NOT NULL DEFAULT '{}'::jsonb,
    open_orders JSONB NOT NULL DEFAULT '[]'::jsonb,
    last_event_time_ms BIGINT,
    last_sequence BIGINT,
    source TEXT NOT NULL,
    fact_version TEXT NOT NULL,
    complete BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (account_id, venue_id)
);

CREATE TABLE IF NOT EXISTS v3_ledger_transactions (
    transaction_id TEXT PRIMARY KEY,
    transaction_type TEXT NOT NULL,
    source_event_id TEXT,
    correlation_id TEXT,
    occurred_at TIMESTAMPTZ NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_v3_ledger_source_event
    ON v3_ledger_transactions(source_event_id)
    WHERE source_event_id IS NOT NULL AND source_event_id <> '';

CREATE TABLE IF NOT EXISTS v3_ledger_postings (
    posting_id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES v3_ledger_transactions(transaction_id),
    account_id TEXT NOT NULL,
    account_type TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    instrument_id TEXT,
    amount TEXT NOT NULL,
    currency TEXT NOT NULL,
    decimals INTEGER NOT NULL DEFAULT 8,
    side TEXT NOT NULL CHECK (side IN ('DEBIT', 'CREDIT')),
    description TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_v3_ledger_postings_transaction
    ON v3_ledger_postings(transaction_id, posting_id);
