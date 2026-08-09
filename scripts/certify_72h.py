#!/usr/bin/env python3
"""PKG-MON-11: 72h Testnet 认证运行器 (INV-012: 真实经过时间)。

用法:
  python scripts/certify_72h.py start    # 启动 72h 认证
  python scripts/certify_72h.py status   # 查看当前进度
  python scripts/certify_72h.py finalize # 结束并生成最终报告
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CERT_DIR = Path("evidence/certification")
LEGACY_CERTIFIER_MESSAGE = (
    "LEGACY_72H_CERTIFIER_DISABLED: use the governed G7 runtime producer with "
    "authoritative cost_and_pnl_reporting, protection, reconciliation, and incident evidence"
)


def verify_prerequisites() -> bool:
    """The old timer-only certifier cannot establish a production gate."""
    raise RuntimeError(LEGACY_CERTIFIER_MESSAGE)


def start_certification():
    """拒绝只靠计时器生成无人值守证书。"""
    raise RuntimeError(LEGACY_CERTIFIER_MESSAGE)


def finalize_certification(cert_id: str | None = None):
    """拒绝把计时器文件转换为 G7 证书。"""
    raise RuntimeError(LEGACY_CERTIFIER_MESSAGE)


def show_status():
    """只读显示旧目录状态；不把它解释为 G7 证书。"""
    manifest_path = CERT_DIR / "manifest.json"
    if not manifest_path.exists():
        print(json.dumps({"state": "NOT_VERIFIABLE", "reason": "NO_LEGACY_MANIFEST"}, ensure_ascii=False))
        return

    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        raise RuntimeError("LEGACY_CERTIFICATION_MANIFEST_CORRUPT") from exc
    print(json.dumps({"state": "NOT_VERIFIABLE", "legacy_manifest": manifest}, ensure_ascii=False))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "start":
        start_certification()
    elif cmd == "finalize":
        finalize_certification()
    elif cmd == "status":
        show_status()
    else:
        raise SystemExit("usage: certify_72h.py {start|status|finalize}")
