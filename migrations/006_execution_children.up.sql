-- Durable parent/child execution commands.
--
-- Every child command is inserted before the first venue write.  Economic
-- fields are immutable through command_hash/payload; later rows only advance
-- state and append events.  UNKNOWN is never converted to a retry by DDL or a
-- timer and must be resolved by an identity-bound venue query.

CREATE TABLE IF NOT EXISTS v3_execution_commands (
    parent_intent_id TEXT NOT NULL REFERENCES v3_order_intents(intent_id),
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    client_order_id TEXT NOT NULL UNIQUE,
    command_hash TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity TEXT NOT NULL,
    order_type TEXT NOT NULL,
    time_in_force TEXT NOT NULL,
    limit_price TEXT,
    reduce_only BOOLEAN NOT NULL DEFAULT FALSE,
    rule_snapshot_hash TEXT NOT NULL,
    payload JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'PLANNED'
        CHECK (state IN (
            'PLANNED', 'SENDING', 'ACKED', 'PARTIALLY_FILLED',
            'FILLED', 'CANCELED', 'REJECTED', 'UNKNOWN'
        )),
    exchange_order_id TEXT,
    filled_quantity TEXT NOT NULL DEFAULT '0',
    lease_owner TEXT,
    fencing_token BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (parent_intent_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_v3_execution_commands_parent_state
    ON v3_execution_commands(parent_intent_id, state, sequence);
CREATE INDEX IF NOT EXISTS idx_v3_execution_commands_exchange_order
    ON v3_execution_commands(exchange_order_id)
    WHERE exchange_order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_v3_execution_commands_inflight
    ON v3_execution_commands(state, lease_owner, fencing_token, updated_at)
    WHERE state IN ('SENDING', 'ACKED', 'PARTIALLY_FILLED', 'UNKNOWN');

CREATE TABLE IF NOT EXISTS v3_execution_command_events (
    event_id TEXT PRIMARY KEY,
    parent_intent_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    lease_owner TEXT,
    fencing_token BIGINT NOT NULL DEFAULT 0,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (parent_intent_id, sequence)
        REFERENCES v3_execution_commands(parent_intent_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_v3_execution_command_events_parent
    ON v3_execution_command_events(parent_intent_id, sequence, occurred_at, event_id);
