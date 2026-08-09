#!/usr/bin/env python3
"""Forward-only migration runner — BD-03。

验证 schema checksum，按序执行迁移。
禁止跳过或回滚已执行的迁移。
"""

from __future__ import annotations

import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def compute_checksum(filepath: str) -> str:
    with open(filepath, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def run_migrations(migrations_dir: str = "migrations", db_url: str | None = None) -> dict:
    """按序执行 forward-only 迁移。

    返回: {"applied": [...], "skipped": [...], "errors": [...]}
    """
    db_url = db_url or os.environ.get(
        "DATABASE_URL",
        "postgresql://beidou_app@localhost:5432/beidou_testnet",
    )

    result = {"applied": [], "skipped": [], "errors": []}
    migrations_path = Path(migrations_dir)

    if not migrations_path.exists():
        result["errors"].append(f"Migrations directory not found: {migrations_dir}")
        return result

    # 查找所有 .up.sql 文件
    up_files = sorted(migrations_path.glob("*.up.sql"))
    if not up_files:
        result["errors"].append("No .up.sql migration files found")
        return result

    try:
        import psycopg

        with psycopg.connect(db_url) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version VARCHAR(255) PRIMARY KEY,
                   checksum VARCHAR(64) NOT NULL,
                   applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                   description TEXT
                )"""
            )
            conn.commit()
    except Exception as exc:
        result["errors"].append(f"schema_migrations bootstrap: {type(exc).__name__}: {exc}")
        return result

    for sql_file in up_files:
        checksum = compute_checksum(str(sql_file))

        try:
            with psycopg.connect(db_url) as conn, conn.cursor() as cur:
                # 检查是否已执行
                cur.execute(
                    "SELECT checksum FROM schema_migrations WHERE version = %s",
                    (sql_file.stem,),
                )
                existing = cur.fetchone()
                if existing and str(existing[0]) != checksum:
                    result["errors"].append(
                        f"{sql_file.name}: checksum mismatch for applied migration {sql_file.stem}"
                    )
                    break
                if existing:
                    result["skipped"].append(str(sql_file.name))
                    continue

                # 执行迁移
                sql = sql_file.read_text()
                cur.execute(sql)

                # 记录迁移
                cur.execute(
                    "INSERT INTO schema_migrations (version, checksum, description) VALUES (%s, %s, %s)",
                    (sql_file.stem, checksum, f"Applied at {datetime.now(timezone.utc).isoformat()}"),
                )
                conn.commit()

            result["applied"].append(str(sql_file.name))

        except Exception as e:
            result["errors"].append(f"{sql_file.name}: {e}")
            break  # Forward-only: stop on first error

    return result


if __name__ == "__main__":
    migrations_dir = sys.argv[1] if len(sys.argv) > 1 else "migrations"
    db_url = sys.argv[2] if len(sys.argv) > 2 else None
    result = run_migrations(migrations_dir, db_url)
    sys.exit(1 if result["errors"] else 0)
