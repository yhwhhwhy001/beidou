"""北斗模块生命周期管理。状态机、能力注册、健康证据与降级语义。"""

__version__ = "2.0.0"
from .capability import CapabilityNegotiation, CapabilityRegistry, CapabilityVersion, CompatibilityResult
from .lifecycle import DegradationLevel, HealthEvidence, ModuleLifecycle, ModuleState

__all__ = [
    "CapabilityNegotiation",
    "CapabilityRegistry",
    "CapabilityVersion",
    "CompatibilityResult",
    "DegradationLevel",
    "HealthEvidence",
    "ModuleLifecycle",
    "ModuleState",
]
