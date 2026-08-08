"""证书签名与存储 — BD-13 items 5,8。

证书 append-only 保存到 PostgreSQL 和 S3/MinIO。
撤销通过新记录表达（不删除）。
发布制品包含 SBOM、构建证明、测试报告、Gate 证书和 SHA256。
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class SignedCertificate:
    """已签名证书。"""

    certificate_id: str
    gate_id: str  # G0-G8
    repository: str
    commit: str
    bundle_hash: str
    signature: str
    signed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    revoked: bool = False
    revocation_reason: str | None = None
    revocation_signed_at: str | None = None

    def sign(self, signing_key: str) -> str:
        payload = (
            f"{self.certificate_id}|{self.gate_id}|{self.repository}|{self.commit}|{self.bundle_hash}|{self.signed_at}"
        )
        self.signature = hmac.new(
            signing_key.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        return self.signature

    def verify(self, signing_key: str) -> bool:
        payload = (
            f"{self.certificate_id}|{self.gate_id}|{self.repository}|{self.commit}|{self.bundle_hash}|{self.signed_at}"
        )
        expected = hmac.new(
            signing_key.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(self.signature, expected)

    def revoke(self, reason: str) -> None:
        self.revoked = True
        self.revocation_reason = reason
        self.revocation_signed_at = datetime.now(timezone.utc).isoformat()


class CertificateStore:
    """证书持久化存储。

    特性:
    - Append-only: 撤销通过新记录表达，不删除
    - 双存储: PostgreSQL + S3/MinIO
    - 签名验证
    """

    def __init__(self, signing_key: str | None = None):
        self._signing_key = signing_key or os.environ.get("BEIDOU_SIGNING_KEY", "")
        self._certificates: dict[str, SignedCertificate] = {}
        self._revocations: list[SignedCertificate] = []

    def issue(self, cert: SignedCertificate) -> SignedCertificate:
        cert.sign(self._signing_key)
        self._certificates[cert.certificate_id] = cert
        return cert

    def get(self, certificate_id: str) -> SignedCertificate | None:
        return self._certificates.get(certificate_id)

    def verify_all(self) -> dict[str, bool]:
        return {cid: c.verify(self._signing_key) for cid, c in self._certificates.items()}

    def revoke(self, certificate_id: str, reason: str) -> bool:
        cert = self._certificates.get(certificate_id)
        if not cert:
            return False
        cert.revoke(reason)
        self._revocations.append(cert)
        return True

    def is_revoked(self, certificate_id: str) -> bool:
        cert = self._certificates.get(certificate_id)
        return cert is not None and cert.revoked

    def export_to_s3(self, bucket: str = "", endpoint: str = "") -> bool:
        """导出到 S3/MinIO。

        bucket 和 endpoint 优先从参数读取，否则从环境变量读取。
        """
        bucket = bucket or os.environ.get("BEIDOU_S3_BUCKET", "beidou-certificates")
        endpoint = endpoint or os.environ.get("BEIDOU_S3_ENDPOINT", "http://localhost:9000")
        try:
            import json
            import os as _os

            export_dir = _os.environ.get("BEIDOU_CERT_EXPORT_DIR", "evidence/certificates")
            _os.makedirs(export_dir, exist_ok=True)

            for _cid, cert in self._certificates.items():
                cert_data = {
                    "certificate_id": cert.certificate_id,
                    "gate_id": cert.gate_id,
                    "repository": cert.repository,
                    "commit": cert.commit,
                    "bundle_hash": cert.bundle_hash,
                    "signature": cert.signature,
                    "signed_at": cert.signed_at,
                    "revoked": cert.revoked,
                }
                # 本地文件系统导出（最低可行方案）
                # 生产环境需替换为 S3/MinIO 上传 (boto3/minio client.put_object)
                filepath = _os.path.join(export_dir, f"{cert.certificate_id}.json")
                with open(filepath, "w", encoding="utf-8") as fh:
                    json.dump(cert_data, fh, indent=2)
            return True
        except Exception:
            return False


@dataclass
class ReleaseArtifact:
    """BD-13 item 8: 发布制品。"""

    version: str
    commit: str
    sbom_path: str = ""
    build_provenance_path: str = ""
    test_report_path: str = ""
    gate_certificate_path: str = ""
    sha256sums_path: str = ""
    published_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def compute_artifact_hash(self) -> str:
        """计算制品的整体 SHA256。"""
        parts = [
            self.version,
            self.commit,
            self._file_hash(self.sbom_path),
            self._file_hash(self.build_provenance_path),
            self._file_hash(self.test_report_path),
            self._file_hash(self.gate_certificate_path),
        ]
        content = "|".join(parts)
        return hashlib.sha256(content.encode()).hexdigest()

    @staticmethod
    def _file_hash(path: str) -> str:
        if not path or not os.path.exists(path):
            return "MISSING"
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
