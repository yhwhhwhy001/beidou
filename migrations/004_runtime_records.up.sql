-- Runtime compatibility records for the PostgreSQL engine adapter.
--
-- This table is intentionally an explicit compatibility layer while the
-- domain projections are migrated one by one.  It is durable and append
-- audited, but it does not by itself certify production readiness: the
-- runtime must still pass replay, crash, PITR and venue reconciliation gates.

CREATE TABLE IF NOT EXISTS v3_runtime_records (
    record_type TEXT NOT NULL,
    record_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (record_type, record_id)
);

CREATE INDEX IF NOT EXISTS idx_v3_runtime_records_type_updated
    ON v3_runtime_records(record_type, updated_at, record_id);

CREATE TABLE IF NOT EXISTS v3_runtime_events (
    event_id TEXT PRIMARY KEY,
    record_type TEXT NOT NULL,
    record_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_v3_runtime_events_record
    ON v3_runtime_events(record_type, record_id, occurred_at, event_id);
