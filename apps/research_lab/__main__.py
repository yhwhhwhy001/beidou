"""离线研究实验入口 (Research Lab)。Clock Domain: OFFLINE。

BF-10: Research Lab 不得启动 Autopilot。
使用独立的离线研究环境，不依赖实时行情和三层时钟域。
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    proj_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, proj_root)
    os.chdir(proj_root)
    os.environ.setdefault("BEIDOU_ENV", "research")

    print("=" * 60)
    print("北斗 Research Lab — 离线因子研究环境")
    print("=" * 60)
    print()
    print("可用功能:")
    print("  1. 因子挖掘流水线:  python -m apps.factor_miner run --policy config/factor_mining_policy.yaml")
    print("  2. 合成数据验证:    python -m pytest tests/unit/test_fw03_e2e_mining.py -v -s")
    print("  3. 表达式测试:      python -m pytest tests/unit/test_bf03_ast.py -v")
    print("  4. 因子评估测试:    python -m pytest tests/unit/test_bf02_metrics.py -v")
    print()
    print("⚠️  注意: Research Lab 不启动 Autopilot (三层时钟域)。")
    print("    如需 Paper 运行，请使用: python -m apps.autopilot --mode paper")
    print("    离线研究请使用 apps.factor_miner CLI。")
    print()

    # M00-F06: --run 分支退役 —— 旧实现只构造 PipelineConfig 不执行任何
    # 挖掘，属误导性占位。真实挖掘唯一入口是 factor_miner CLI。
    if "--run" in sys.argv:
        print(
            "[research_lab] --run is retired (placeholder that never executed a mining run); "
            "use `python -m apps.factor_miner run --policy config/factor_mining_policy.yaml`."
        )
        sys.exit(2)
    else:
        print("[research_lab] 就绪。使用 --run 参数启动因子挖掘。")


if __name__ == "__main__":
    main()
