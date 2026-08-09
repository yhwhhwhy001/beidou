"""Crash-consistent local backup and restore verification.

This module is deliberately storage-provider neutral: it can produce and
verify a SQLite backup, but it does not claim that a local file is an
external object-store/PITR backup.  Encryption requires an explicitly
injected 256-bit key; there is no generated or hard-coded fallback key.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DEFAULT_REQUIRED_TABLES = frozenset(
    {
        "ledger_transactions",
        "ledger_postings",
        "fill_events",
        "user_stream_events",
        "user_stream_projections",
        "reconciliation_snapshots",
        "reconciliation_results",
        "position_projection",
        "account_opening_projections",
    }
)
_ENCRYPTED_HEADER = b"BEIDOU-BACKUP-V1\x00"


@dataclass(frozen=True, slots=True)
class BackupManifest:
    backup_id: str
    source_path: str
    backup_path: str
    created_at: datetime
    sha256: str
    size_bytes: int
    integrity_check: bool
    required_tables: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BackupVerificationResult:
    backup_path: str
    sha256: str
    integrity_check: bool
    foreign_key_check: bool
    required_tables: tuple[str, ...]
    row_counts: dict[str, int] = field(default_factory=dict)
    errors: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.integrity_check and self.foreign_key_check and not self.errors


@dataclass(frozen=True, slots=True)
class EncryptedBackupManifest:
    source_path: str
    encrypted_path: str
    created_at: datetime
    ciphertext_sha256: str
    plaintext_size_bytes: int


class SQLiteBackupManager:
    """Create and verify a consistent SQLite backup without exchange access."""

    def __init__(self, *, required_tables: frozenset[str] = DEFAULT_REQUIRED_TABLES) -> None:
        self._required_tables = frozenset(required_tables)

    def create(self, source_path: str | Path, backup_path: str | Path, *, backup_id: str) -> BackupManifest:
        source = self._require_regular_file(source_path, "source")
        destination = Path(backup_path).expanduser()
        if destination.exists():
            raise FileExistsError(f"backup destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with sqlite3.connect(str(source)) as source_conn:
                with tempfile.NamedTemporaryFile(
                    dir=str(destination.parent),
                    prefix=f".{destination.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                with sqlite3.connect(str(temporary_path)) as backup_conn:
                    source_conn.backup(backup_conn)
                    backup_conn.commit()
            os.replace(temporary_path, destination)
            temporary_path = None
            verification = self.verify(destination)
            if not verification.passed:
                raise RuntimeError("backup verification failed: " + "; ".join(verification.errors))
            return BackupManifest(
                backup_id=str(backup_id),
                source_path=str(source),
                backup_path=str(destination),
                created_at=datetime.now(timezone.utc),
                sha256=verification.sha256,
                size_bytes=destination.stat().st_size,
                integrity_check=verification.integrity_check,
                required_tables=tuple(sorted(self._required_tables)),
            )
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def verify(self, backup_path: str | Path) -> BackupVerificationResult:
        backup = self._require_regular_file(backup_path, "backup")
        errors: list[str] = []
        row_counts: dict[str, int] = {}
        integrity_ok = False
        foreign_keys_ok = False
        try:
            uri = f"file:{quote(str(backup), safe='/')}?mode=ro"
            with sqlite3.connect(uri, uri=True) as conn:
                integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
                integrity_ok = integrity == "ok"
                if not integrity_ok:
                    errors.append(f"integrity_check={integrity}")
                foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
                foreign_keys_ok = not foreign_keys
                if not foreign_keys_ok:
                    errors.append(f"foreign_key_check_rows={len(foreign_keys)}")
                tables = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                }
                missing = sorted(self._required_tables - tables)
                if missing:
                    errors.append("missing_required_tables=" + ",".join(missing))
                for table in sorted(self._required_tables & tables):
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
                        errors.append(f"invalid_required_table_name={table}")
                        continue
                    quoted_table = '"' + table.replace('"', '""') + '"'
                    row_counts[table] = int(
                        conn.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0]  # noqa: S608
                    )
        except (OSError, sqlite3.Error) as exc:
            errors.append(f"sqlite_verification_error={type(exc).__name__}: {exc}")
        return BackupVerificationResult(
            backup_path=str(backup),
            sha256=self._sha256(backup),
            integrity_check=integrity_ok,
            foreign_key_check=foreign_keys_ok,
            required_tables=tuple(sorted(self._required_tables)),
            row_counts=row_counts,
            errors=tuple(errors),
        )

    @staticmethod
    def encrypt(plaintext_path: str | Path, encrypted_path: str | Path, *, key: bytes) -> EncryptedBackupManifest:
        plaintext = SQLiteBackupManager._require_regular_file(plaintext_path, "plaintext backup")
        if len(key) != 32:
            raise ValueError("backup encryption requires exactly 32 key bytes")
        destination = Path(encrypted_path).expanduser()
        if destination.exists():
            raise FileExistsError(f"encrypted backup destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = plaintext.read_bytes()
        nonce = os.urandom(12)
        ciphertext = AESGCM(key).encrypt(nonce, data, None)
        payload = _ENCRYPTED_HEADER + nonce + ciphertext
        SQLiteBackupManager._atomic_write(destination, payload)
        return EncryptedBackupManifest(
            source_path=str(plaintext),
            encrypted_path=str(destination),
            created_at=datetime.now(timezone.utc),
            ciphertext_sha256=hashlib.sha256(payload).hexdigest(),
            plaintext_size_bytes=len(data),
        )

    @staticmethod
    def decrypt(encrypted_path: str | Path, plaintext_path: str | Path, *, key: bytes) -> Path:
        encrypted = SQLiteBackupManager._require_regular_file(encrypted_path, "encrypted backup")
        if len(key) != 32:
            raise ValueError("backup encryption requires exactly 32 key bytes")
        destination = Path(plaintext_path).expanduser()
        if destination.exists():
            raise FileExistsError(f"decrypted backup destination already exists: {destination}")
        payload = encrypted.read_bytes()
        if not payload.startswith(_ENCRYPTED_HEADER) or len(payload) <= len(_ENCRYPTED_HEADER) + 12:
            raise ValueError("invalid encrypted backup envelope")
        offset = len(_ENCRYPTED_HEADER)
        plaintext = AESGCM(key).decrypt(payload[offset : offset + 12], payload[offset + 12 :], None)
        SQLiteBackupManager._atomic_write(destination, plaintext)
        return destination

    @staticmethod
    def _require_regular_file(path: str | Path, label: str) -> Path:
        resolved = Path(path).expanduser()
        if not resolved.exists() or not resolved.is_file():
            raise FileNotFoundError(f"{label} is not a regular file: {resolved}")
        return resolved

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _atomic_write(destination: Path, payload: bytes) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=str(destination.parent),
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, destination)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


__all__ = [
    "BackupManifest",
    "BackupVerificationResult",
    "EncryptedBackupManifest",
    "SQLiteBackupManager",
]
