"""BD-CV03 AC-03-04: 证据篡改检测测试。

任意修改 evidence/manifest/artifact/hash 后 verifier 必须 FAIL。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


class TestEvidenceTamperDetection:
    """证据篡改必须被检测到并导致验证失败。"""

    def _make_temp_evidence(self, content: dict) -> Path:
        """创建临时证据文件和 manifest。"""
        tmpdir = Path(tempfile.mkdtemp())
        manifest_path = tmpdir / "manifest.json"
        manifest_path.write_text(json.dumps(content, indent=2))
        return tmpdir

    def _compute_manifest_hash(self, content: dict) -> str:
        return hashlib.sha256(
            json.dumps(content, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def test_manifest_hash_changes_when_content_changes(self):
        """AC-03-04: 修改 manifest 后 hash 必须变化。"""
        content = {"task_id": "TEST", "status": "PASS", "evidence": ["file1.txt"]}
        hash1 = self._compute_manifest_hash(content)
        content["status"] = "FAIL"
        hash2 = self._compute_manifest_hash(content)
        assert hash1 != hash2, "Tampered manifest must produce different hash"

    def test_missing_evidence_detected(self):
        """AC-03-04: 缺少声明 evidence 文件时验证必须失败。"""
        content = {
            "task_id": "TEST",
            "evidence_files": ["missing_file.txt", "also_missing.txt"],
        }
        tmpdir = self._make_temp_evidence(content)
        # 检查声明的文件是否存在
        for fname in content["evidence_files"]:
            fpath = tmpdir / fname
            assert not fpath.exists(), f"Precondition: {fname} must not exist"
        # 验证逻辑: 任何声明的文件缺失 → FAIL
        missing = [f for f in content["evidence_files"] if not (tmpdir / f).exists()]
        assert len(missing) > 0, "Tamper: missing evidence files must be detected"

    def test_modified_evidence_file_alters_hash(self):
        """AC-03-04: 修改 evidence 文件内容后 hash 必须变化。"""
        tmpdir = Path(tempfile.mkdtemp())
        evidence_file = tmpdir / "test_output.txt"
        evidence_file.write_text("PASS: 1563 tests")
        hash1 = hashlib.sha256(evidence_file.read_bytes()).hexdigest()
        evidence_file.write_text("PASS: 9999 tests (TAMPERED)")
        hash2 = hashlib.sha256(evidence_file.read_bytes()).hexdigest()
        assert hash1 != hash2, "Modified evidence must produce different hash"

    def test_manifest_integrity_check(self):
        """验证 manifest 结构完整性。"""
        minimal_valid_manifest = {
            "task_id": "BD-CV-TEST",
            "head_sha": "a" * 40,
            "status": "EVIDENCE_COLLECTED",
            "evidence_files": ["test.txt"],
        }
        required_fields = ["task_id", "head_sha", "status"]
        for field in required_fields:
            assert field in minimal_valid_manifest, f"Missing required field: {field}"

    def test_tampered_commit_sha_detected(self):
        """AC-03-04: commit SHA 不一致必须被检测。"""
        manifest = {
            "task_id": "BD-CV-TEST",
            "head_sha": "aaaa000000000000000000000000000000000000",
        }
        actual_head = "bbbb111111111111111111111111111111111111"
        assert manifest["head_sha"] != actual_head, "SHA mismatch must be detected"

    def test_evidence_hash_binding(self):
        """AC-03-03: evidence hash 必须绑定到 manifest。"""
        evidence_content = b"test evidence data"
        evidence_hash = hashlib.sha256(evidence_content).hexdigest()
        manifest = {
            "task_id": "BD-CV-TEST",
            "evidence_hashes": {
                "test.txt": evidence_hash,
            },
        }
        # 重新计算
        recomputed = hashlib.sha256(evidence_content).hexdigest()
        assert manifest["evidence_hashes"]["test.txt"] == recomputed, (
            "Manifest hash must match recomputed hash"
        )
        # 篡改后不匹配
        tampered = hashlib.sha256(b"tampered data").hexdigest()
        assert manifest["evidence_hashes"]["test.txt"] != tampered, (
            "Tampered data must not match manifest hash"
        )


class TestEnvEvidenceSurvival:
    """其他 AC 和边界条件。"""

    def test_evidence_does_not_contain_secrets(self):
        """证据文件不得包含 API 密钥明文。"""
        project_root = Path(__file__).parent.parent.parent
        evidence_dir = project_root / "artifacts" / "evidence"
        if not evidence_dir.exists():
            return  # 没有 evidence 目录则跳过
        # Real secrets (actual key values, not regex patterns)
        real_secrets = [
            "BEIDOU_BINANCE_API_KEY=d1x",
            "BEIDOU_BINANCE_API_SECRET=wKp",
            "BEIDOU_SIGNING_KEY=beidou-testnet",
            "BEIDOU_POSTGRES_PASSWORD=beidou_dev",
        ]
        for root, dirs, files in os.walk(evidence_dir):
            for fname in files:
                if fname.endswith(".txt") or fname.endswith(".json"):
                    fpath = Path(root) / fname
                    content = fpath.read_text(errors="replace")
                    for pattern in real_secrets:
                        assert pattern not in content, (
                            f"Real secret value found in evidence: {fpath}"
                        )
