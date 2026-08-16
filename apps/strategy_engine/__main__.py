"""Retired legacy strategy-engine entry (Clock Domain: NEARLINE).

M00-F06: 该入口直接实例化 AutonomousEngine（paper 模式硬编码）构成
第二引擎启动路径，与唯一生产主链（beidou_launcher → supervisor →
AutonomousEngine）冲突。保留 fail-closed 退役入口防止误执行，迁移
目标：``beidou start --mode <mode>``（唯一主链）。
"""

from __future__ import annotations


def main() -> int:
    print(
        "apps.strategy_engine is retired: a second engine entry must not exist; "
        "use the single production chain: `beidou start --mode paper`."
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
