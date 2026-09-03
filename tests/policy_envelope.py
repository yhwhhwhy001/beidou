"""签名授权信封的存在性守卫。

BD-AF-P3-T07 的 Metric Owner 策略与 BD-AF-P3-T08 的 Portfolio Owner 策略
是本人签名、带有效期的授权信封,按设计存放在仓库之外(见
artifacts/analysis/alpha-return-2026-08-01_2026-08-31-v1/governance/inputs/
metric-owner-verification.json:scope=LOCAL_IMPLEMENTATION_ONLY)。

测试固定校验信封的 SHA256,所以替身或合成 fixture 一律无效 —— 拿不到原件
时只能跳过,不能伪造。环境变量可以把路径指向原件的实际存放位置。
"""

from __future__ import annotations

from pathlib import Path

import pytest


def require_policy_envelope(path: Path, env_var: str) -> None:
    """Skip when the signed policy envelope is not reachable at ``path``."""

    if not path.is_file():
        pytest.skip(f"signed policy envelope absent at {path}; point {env_var} at the original to run this")
