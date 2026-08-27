"""LOCKED 时必须落完整未截断的阻断快照 (74% 根因不可恢复的修复)。"""

import json
import os

from beidou_launcher.models import CheckResult, CheckSeverity, CheckStatus


def test_locked_snapshot_written_with_full_blockers(tmp_path):
    from beidou_launcher.supervisor import _write_locked_snapshot

    blockers = [
        CheckResult(
            check_id="runtime.safety.reconciliation",
            name="深度对账",
            status=CheckStatus.FAIL,
            severity=CheckSeverity.P0,
            message="Balance mismatch: system=1 exchange=10736.5 diff=10735.5 tolerance=107.36",
        )
    ]
    path = _write_locked_snapshot(blockers, base_dir=tmp_path, now=1700000000.0)
    assert os.path.exists(path)
    payload = json.loads(path.read_text())
    assert "Balance mismatch" in json.dumps(payload["blockers"], ensure_ascii=False)
    assert "10736.5" in json.dumps(payload["blockers"], ensure_ascii=False)  # 未被截断
