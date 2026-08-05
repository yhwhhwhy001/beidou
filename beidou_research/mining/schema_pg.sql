-- BF-07: 因子研究 PostgreSQL Schema (Section 12.1)
-- Forward-only migration. 适用于 PostgreSQL 14+

BEGIN;

-- ================================================================
-- 数据集与特征清单
-- ================================================================

CREATE TABLE IF NOT EXISTS research_dataset_manifest (
    manifest_id     TEXT PRIMARY KEY,
    manifest_hash   TEXT NOT NULL,
    venue           TEXT NOT NULL,
    symbols         TEXT[] NOT NULL,
    timeframe       TEXT NOT NULL,
    start_time      TIMESTAMPTZ NOT NULL,
    end_time        TIMESTAMPTZ NOT NULL,
    feature_names   TEXT[] NOT NULL,
    feature_versions TEXT[] NOT NULL,
    source_description TEXT DEFAULT '',
    is_frozen       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS research_feature_manifest (
    feature_manifest_hash TEXT PRIMARY KEY,
    feature_name    TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    source          TEXT DEFAULT '',
    dtype           TEXT DEFAULT 'float64',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ================================================================
-- 标签规格
-- ================================================================

CREATE TABLE IF NOT EXISTS research_label_spec (
    label_id            TEXT PRIMARY KEY,
    horizon_bars        INT NOT NULL,
    horizon_unit        TEXT DEFAULT 'bar',
    price_type          TEXT DEFAULT 'close',
    return_type         TEXT DEFAULT 'log',
    cost_adjusted       BOOLEAN DEFAULT TRUE,
    neutralization      TEXT[] DEFAULT '{}',
    embargo_bars        INT DEFAULT 0,
    min_data_quality    TEXT DEFAULT 'PASS',
    spec_hash           TEXT NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ================================================================
-- 挖掘运行
-- ================================================================

CREATE TABLE IF NOT EXISTS research_mining_run (
    run_id              TEXT PRIMARY KEY,
    status              TEXT DEFAULT 'CREATED',
    dataset_manifest_hash TEXT DEFAULT '',
    label_spec_hash     TEXT DEFAULT '',
    max_candidates      INT DEFAULT 10000,
    random_seed         INT DEFAULT 42,
    generators          TEXT[] DEFAULT '{}',
    candidates_generated INT DEFAULT 0,
    candidates_screened INT DEFAULT 0,
    candidates_evaluated INT DEFAULT 0,
    candidates_passed   INT DEFAULT 0,
    failure_taxonomy    JSONB DEFAULT '{}',
    evidence_bundle_hash TEXT DEFAULT '',
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ================================================================
-- 因子候选与表达式
-- ================================================================

CREATE TABLE IF NOT EXISTS research_factor_expression (
    expression_id       TEXT PRIMARY KEY,
    ast_json            JSONB NOT NULL,
    output_type         TEXT NOT NULL,
    required_features   TEXT[] DEFAULT '{}',
    max_lookback_bars   INT DEFAULT 0,
    complexity_score    FLOAT DEFAULT 0.0,
    canonical_hash      TEXT NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS research_factor_candidate (
    candidate_id        TEXT PRIMARY KEY,
    expression_id       TEXT REFERENCES research_factor_expression(expression_id),
    parameter_set       JSONB DEFAULT '{}',
    label_spec_id       TEXT DEFAULT '',
    universe_spec_id    TEXT DEFAULT '',
    generator_type      TEXT NOT NULL,
    generator_version   TEXT DEFAULT '2.0.0',
    random_seed         INT DEFAULT 42,
    parent_candidate_ids TEXT[] DEFAULT '{}',
    run_id              TEXT REFERENCES research_mining_run(run_id),
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ================================================================
-- 因子评估运行
-- ================================================================

CREATE TABLE IF NOT EXISTS research_factor_evaluation_run (
    eval_run_id         TEXT PRIMARY KEY,
    candidate_id        TEXT NOT NULL,
    run_id              TEXT NOT NULL,
    fold_id             INT DEFAULT 0,
    fold_type           TEXT DEFAULT 'wfo',  -- wfo / cpcv
    train_samples       INT DEFAULT 0,
    test_samples        INT DEFAULT 0,
    ic_mean             FLOAT DEFAULT 0.0,
    ic_std              FLOAT DEFAULT 0.0,
    icir                FLOAT DEFAULT 0.0,
    rank_ic_mean        FLOAT DEFAULT 0.0,
    sharpe              FLOAT DEFAULT 0.0,
    hit_rate            FLOAT DEFAULT 0.0,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ================================================================
-- Fold 指标
-- ================================================================

CREATE TABLE IF NOT EXISTS research_factor_fold_metric (
    metric_id           TEXT PRIMARY KEY,
    eval_run_id         TEXT NOT NULL,
    fold_id             INT NOT NULL,
    metric_name         TEXT NOT NULL,
    metric_value        FLOAT NOT NULL,
    sample_count        INT DEFAULT 0,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ================================================================
-- 稳健性指标
-- ================================================================

CREATE TABLE IF NOT EXISTS research_factor_stability_metric (
    stability_id        TEXT PRIMARY KEY,
    candidate_id        TEXT NOT NULL,
    dimension           TEXT NOT NULL,
    full_sample_value   FLOAT DEFAULT 0.0,
    sub_sample_value    FLOAT DEFAULT 0.0,
    degradation_pct     FLOAT DEFAULT 0.0,
    is_stable           BOOLEAN DEFAULT FALSE,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ================================================================
-- 因子溯源
-- ================================================================

CREATE TABLE IF NOT EXISTS research_factor_lineage (
    lineage_id          TEXT PRIMARY KEY,
    candidate_id        TEXT NOT NULL,
    parent_candidate_id TEXT,
    operation           TEXT NOT NULL,
    expression_hash     TEXT NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ================================================================
-- Gate 决策
-- ================================================================

CREATE TABLE IF NOT EXISTS research_factor_gate_decision (
    gate_id             TEXT PRIMARY KEY,
    factor_id           TEXT NOT NULL,
    from_state          TEXT NOT NULL,
    to_state            TEXT NOT NULL,
    evidence_bundle_hash TEXT NOT NULL,
    decision            TEXT NOT NULL,
    reason              TEXT DEFAULT '',
    operator_id         TEXT DEFAULT 'system',
    decided_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ================================================================
-- 因子版本（不可变）
-- ================================================================

CREATE TABLE IF NOT EXISTS research_factor_version (
    factor_version_id   TEXT PRIMARY KEY,
    factor_id           TEXT NOT NULL,
    version             TEXT NOT NULL,
    lifecycle           TEXT DEFAULT 'IDEA',
    evidence_bundle_hash TEXT DEFAULT '',
    artifact_ref        TEXT DEFAULT '',
    strategy_binding    TEXT DEFAULT '',
    code_hash           TEXT DEFAULT '',
    parameter_hash      TEXT DEFAULT '',
    is_immutable        BOOLEAN DEFAULT TRUE,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (factor_id, version)
);

CREATE INDEX IF NOT EXISTS idx_factor_version_lifecycle
    ON research_factor_version(lifecycle);

CREATE INDEX IF NOT EXISTS idx_gate_decision_factor
    ON research_factor_gate_decision(factor_id, decided_at);

-- ================================================================
-- 策略
-- ================================================================

CREATE TABLE IF NOT EXISTS strategy_definition (
    strategy_id         TEXT PRIMARY KEY,
    version             TEXT NOT NULL,
    factor_versions     TEXT[] DEFAULT '{}',
    entry_policy        JSONB DEFAULT '{}',
    filter_chain        JSONB DEFAULT '[]',
    fusion_policy       JSONB DEFAULT '{}',
    sizing_policy       JSONB DEFAULT '{}',
    exit_policy         JSONB DEFAULT '{}',
    risk_budget_policy  TEXT DEFAULT '',
    eligible_regimes    TEXT[] DEFAULT '{}',
    evidence_bundle_hash TEXT DEFAULT '',
    code_hash           TEXT DEFAULT '',
    parameter_hash      TEXT DEFAULT '',
    is_active           BOOLEAN DEFAULT FALSE,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (strategy_id, version)
);

CREATE TABLE IF NOT EXISTS strategy_factor_binding (
    binding_id          TEXT PRIMARY KEY,
    strategy_id         TEXT NOT NULL,
    strategy_version    TEXT NOT NULL,
    factor_version_id   TEXT NOT NULL,
    role                TEXT DEFAULT 'entry',
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS strategy_gate_decision (
    gate_id             TEXT PRIMARY KEY,
    strategy_id         TEXT NOT NULL,
    from_state          TEXT NOT NULL,
    to_state            TEXT NOT NULL,
    evidence_bundle_hash TEXT NOT NULL,
    decision            TEXT NOT NULL,
    reason              TEXT DEFAULT '',
    decided_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS strategy_runtime_attribution (
    attribution_id      TEXT PRIMARY KEY,
    strategy_id         TEXT NOT NULL,
    strategy_version    TEXT NOT NULL,
    factor_id           TEXT NOT NULL,
    factor_version      TEXT NOT NULL,
    contribution_pct    FLOAT DEFAULT 0.0,
    ic_rolling          FLOAT DEFAULT 0.0,
    timestamp           TIMESTAMPTZ DEFAULT NOW()
);

COMMIT;
