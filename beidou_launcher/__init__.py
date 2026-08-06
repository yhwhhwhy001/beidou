"""北斗唯一权威的一键启动、自检与运行监督模块。"""

from .models import CheckResult, CheckSeverity, CheckStatus, StartupReport
from .supervisor import BeidouSupervisor

__all__ = ["BeidouSupervisor", "CheckResult", "CheckSeverity", "CheckStatus", "StartupReport"]
__version__ = "2.1.0"
