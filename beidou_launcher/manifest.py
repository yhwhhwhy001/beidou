"""Authoritative startup manifest for the Beidou runtime."""

from __future__ import annotations

SUPPORTED_MODES = ("research", "paper", "shadow", "testnet", "safety_only")
DEFAULT_MODE = "paper"
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT")
HEALTH_PORT = 9090
EXPECTED_FACTOR_COUNT = 8

REQUIRED_PACKAGES = (
    "beidou_shared",
    "beidou_safety",
    "beidou_strategy",
    "beidou_research",
    "beidou_policy",
    "beidou_security",
    "beidou_observability",
    "beidou_exchange",
    "beidou_lifecycle",
    "beidou_delivery",
    "beidou_autonomy",
    "beidou_infra",
    "beidou_data",
    "beidou_control",
    "beidou_chaos",
    "beidou_production",
    "beidou_reporting",
    "beidou_certification",
    "beidou_core",
)

CRITICAL_COMPONENTS = {
    "autonomous_engine": "beidou_core.engine:AutonomousEngine",
    "market_data_feed": "beidou_core.feed:MarketDataFeed",
    "persistent_store": "beidou_core.store:PersistentStore",
    "health_server": "beidou_core.health:HealthServer",
    "exchange_adapter": "beidou_exchange.binance_usdm.adapter:BinanceUsdmAdapter",
    "pre_risk": "beidou_safety.risk.engine:PreRiskCheckerImpl",
    "risk_engine": "beidou_safety.risk.engine:RiskEngineImpl",
    "risk_approval_signer": "beidou_safety.risk.engine:RiskApprovalSignerImpl",
    "risk_approval_state_machine": "beidou_safety.risk.engine:RiskApprovalStateMachine",
    "intent_outbox": "beidou_safety.execution.intent:IntentOutbox",
    "order_state_tracker": "beidou_safety.execution.order_state:OrderStateTracker",
    "immutable_ledger": "beidou_safety.execution.ledger:ImmutableLedger",
    "reconciliation": "beidou_safety.execution.reconciliation:ReconciliationEngine",
    "protection_manager": "beidou_safety.protection.engine:ProtectionManager",
    "alpha_graph": "beidou_strategy.alpha:AlphaGraph",
    "signal_fuser": "beidou_strategy.alpha.signal_fusion:SignalFuser",
    "portfolio_optimizer": "beidou_strategy.portfolio.optimizer:PortfolioOptimizerImpl",
    "strategy_risk": "beidou_strategy.risk.manager:StrategyRiskManager",
    "factor_registry": "beidou_research.factors.factor:FactorRegistry",
    "factor_evaluator": "beidou_research.factors.factor:FactorEvaluator",
    "control_plane": "beidou_control.plane:ControlPlane",
    "module_lifecycle": "beidou_lifecycle.lifecycle:ModuleLifecycle",
    "mapek_controller": "beidou_autonomy.mapek:MAPEKController",
}

EXPECTED_ALPHA_COMPONENTS = frozenset(
    {
        "meanrev_entry_v1",
        "trend_entry_v1",
        "breakout_entry_v1",
        "momentum_filter_v1",
        "volatility_filter_v1",
        "volume_filter_v1",
        "trailing_exit_v1",
        "time_exit_v1",
    }
)

ENGINE_REQUIRED_ATTRIBUTES = (
    "_exchange",
    "_adapter",
    "_store",
    "_feed",
    "_health",
    "_control",
    "_lifecycle",
    "_protection",
    "_outbox",
    "_ledger",
    "_recon",
    "_pre_risk",
    "_risk_engine",
    "_approval",
    "_risk_sm",
    "_post_risk",
    "_cost_model",
    "_optimizer",
    "_fuser",
    "_model_registry",
    "_drift_detector",
    "_mapek",
    "_trading_pool",
    "_strategy_risk",
    "_factor_registry",
    "_factor_evaluator",
    "_alpha_graph",
    "_strategy_kernel",
)
