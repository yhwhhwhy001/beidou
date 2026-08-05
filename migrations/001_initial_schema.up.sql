-- 北斗 V2.0 初始数据库 Schema
-- PostgreSQL 事件存储、事务 Outbox、不可变账本

-- 事件存储表 (append-only)
CREATE TABLE IF NOT EXISTS event_store (
    id BIGSERIAL,
    stream_id VARCHAR(255) NOT NULL,
    aggregate_type VARCHAR(100) NOT NULL,
    sequence BIGINT NOT NULL,
    event_type VARCHAR(100) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    metadata JSONB NOT NULL DEFAULT '{}',
    event_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ingest_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    schema_version VARCHAR(20) NOT NULL DEFAULT '2.0.0',
    correlation_id VARCHAR(255),
    causation_id VARCHAR(255),
    checksum VARCHAR(64) NOT NULL,
    -- 每个 stream 的 sequence 唯一
    CONSTRAINT uq_stream_sequence UNIQUE (stream_id, sequence)
);

CREATE INDEX idx_event_store_stream ON event_store(stream_id, sequence);
CREATE INDEX idx_event_store_correlation ON event_store(correlation_id);
CREATE INDEX idx_event_store_type ON event_store(aggregate_type, event_type);
CREATE INDEX idx_event_store_time ON event_store(ingest_time);

-- 事务 Outbox 表
CREATE TABLE IF NOT EXISTS transactional_outbox (
    id BIGSERIAL PRIMARY KEY,
    aggregate_type VARCHAR(100) NOT NULL,
    aggregate_id VARCHAR(255) NOT NULL,
    event_type VARCHAR(100) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at TIMESTAMPTZ,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    retry_count INT NOT NULL DEFAULT 0,
    max_retries INT NOT NULL DEFAULT 10,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dead_letter_reason TEXT,
    -- 幂等键
    idempotency_key VARCHAR(255),
    client_order_id VARCHAR(255),
    exchange_event_id VARCHAR(255),
    CONSTRAINT uq_idempotency_key UNIQUE (idempotency_key),
    CONSTRAINT uq_client_order_id UNIQUE (client_order_id),
    CONSTRAINT uq_exchange_event_id UNIQUE (exchange_event_id)
);

CREATE INDEX idx_outbox_status ON transactional_outbox(status, next_attempt_at);
CREATE INDEX idx_outbox_aggregate ON transactional_outbox(aggregate_type, aggregate_id);

-- 不可变账本 (Journal) 表
CREATE TABLE IF NOT EXISTS immutable_ledger (
    id BIGSERIAL,
    entry_id VARCHAR(255) NOT NULL PRIMARY KEY,
    account_id VARCHAR(100) NOT NULL,
    venue_id VARCHAR(50) NOT NULL,
    instrument_id VARCHAR(50),
    debit NUMERIC(30,8) NOT NULL DEFAULT 0,
    credit NUMERIC(30,8) NOT NULL DEFAULT 0,
    description TEXT,
    correlation_id VARCHAR(255),
    causation_id VARCHAR(255),
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    checksum VARCHAR(64) NOT NULL,
    -- 借贷平衡约束
    CONSTRAINT ck_ledger_balance CHECK (ABS(debit - credit) < 0.00000001)
);

CREATE INDEX idx_ledger_account ON immutable_ledger(account_id, venue_id);
CREATE INDEX idx_ledger_correlation ON immutable_ledger(correlation_id);
CREATE INDEX idx_ledger_timestamp ON immutable_ledger(timestamp);

-- 保护单表
CREATE TABLE IF NOT EXISTS protections (
    protection_id VARCHAR(255) PRIMARY KEY,
    position_id VARCHAR(255) NOT NULL,
    instrument_id VARCHAR(50) NOT NULL,
    venue_id VARCHAR(50) NOT NULL,
    side VARCHAR(10) NOT NULL,
    trigger_price NUMERIC(30,8),
    order_price NUMERIC(30,8),
    quantity NUMERIC(30,8) NOT NULL,
    order_type VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    executed_at TIMESTAMPTZ,
    correlation_id VARCHAR(255)
);

CREATE INDEX idx_protections_position ON protections(position_id);
CREATE INDEX idx_protections_status ON protections(status);

-- 订单状态跟踪表
CREATE TABLE IF NOT EXISTS order_states (
    order_id VARCHAR(255) PRIMARY KEY,
    instrument_id VARCHAR(50) NOT NULL,
    venue_id VARCHAR(50) NOT NULL,
    side VARCHAR(10),
    order_type VARCHAR(20),
    original_quantity NUMERIC(30,8),
    executed_quantity NUMERIC(30,8) DEFAULT 0,
    price NUMERIC(30,8),
    avg_fill_price NUMERIC(30,8),
    status VARCHAR(20) NOT NULL DEFAULT 'NEW',
    client_order_id VARCHAR(255),
    correlation_id VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    version INT NOT NULL DEFAULT 1
);

CREATE INDEX idx_order_states_status ON order_states(status);
CREATE INDEX idx_order_states_client ON order_states(client_order_id);

-- Schema 版本追踪
CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(50) PRIMARY KEY,
    checksum VARCHAR(64) NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    description TEXT
);
