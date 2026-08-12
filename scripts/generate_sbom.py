#!/usr/bin/env python3
"""BD-CV03: 生成 SBOM (Software Bill of Materials)。

输出 CycloneDX 兼容的依赖清单，绑定 repo SHA 和 lockfile hash。
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def get_git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def get_installed_packages() -> list[dict]:
    try:
        packages = []
        for distribution in importlib.metadata.distributions():
            name = distribution.metadata.get("Name", "").strip()
            version = distribution.version.strip()
            if name:
                packages.append({"name": name, "version": version, "purl": f"pkg:pypi/{name}@{version}"})
        return sorted(packages, key=lambda item: item["name"].lower())
    except Exception as exc:
        print(f"pip freeze failed: {exc}", file=sys.stderr)
        return []


def generate_sbom(project_root: Path, output_path: Path) -> dict:
    packages = get_installed_packages()

    lockfile_path = project_root / "requirements_lock.txt"
    lockfile_hash = ""
    if lockfile_path.exists():
        lockfile_hash = hashlib.sha256(lockfile_path.read_bytes()).hexdigest()

    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:beidou-sbom-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tools": [{"name": "beidou-sbom-generator", "vendor": "beidou"}],
            "component": {
                "name": "beidou",
                "type": "application",
                "version": "2.0.0",
                "description": "北斗 - 加密合约量化交易系统",
            },
            "properties": [
                {"name": "beidou:git_sha", "value": get_git_sha()},
                {"name": "beidou:lockfile_hash", "value": lockfile_hash},
                {"name": "beidou:baseline", "value": "aeaa44700956eccba6e8fc5be7646ca42ce395d5"},
            ],
        },
        "components": [
            {
                "type": "library",
                "name": pkg["name"],
                "version": pkg["version"],
                "purl": pkg["purl"],
            }
            for pkg in packages
        ],
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(sbom, indent=2, ensure_ascii=False))
    print(f"SBOM written to {output_path} ({len(packages)} packages)")
    return sbom


if __name__ == "__main__":
    project_root = Path(__file__).resolve().parent.parent
    output = project_root / "artifacts" / "sbom" / "sbom.json"
    generate_sbom(project_root, output)
