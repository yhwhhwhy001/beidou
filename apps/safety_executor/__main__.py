"""Retired legacy safety-executor entry (Clock Domain: REALTIME).

M00-F06: 该入口直接实例化 AutonomousEngine（safety_only 硬编码）构成
第二引擎启动路径，与唯一生产主链冲突。保留 fail-closed 退役入口防止
误执行，迁移目标：``beidou start --mode <mode>``（唯一主链）。
"""

from __future__ import annotations


def main() -> int:
    print(
        "apps.safety_executor is retired: a second engine entry must not exist; "
        "use the single production chain: `beidou start --mode testnet`."
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
