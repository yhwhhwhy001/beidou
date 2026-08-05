"""北斗模块生命周期管理。状态机、能力注册、健康证据与降级语义。"""
__version__ = "2.0.0"
from .lifecycle import ModuleLifecycle, ModuleState, DegradationLevel, HealthEvidence
from .capability import CapabilityRegistry, CapabilityVersion, CapabilityNegotiation, CompatibilityResult
__all__ = ["ModuleLifecycle", "ModuleState", "DegradationLevel", "HealthEvidence", "CapabilityRegistry", "CapabilityVersion", "CapabilityNegotiation", "CompatibilityResult"]
