#!/usr/bin/env python3
"""BD-CV03: 生成 SBOM (Software Bill of Materials)。

输出 CycloneDX 兼容的依赖清单，绑定 repo SHA 和 lockfile hash。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def get_git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def get_pip_freeze(venv_python: str) -> list[dict]:
    try:
        result = subprocess.run(
            [venv_python, "-m", "pip", "freeze", "--all"],
            capture_output=True, text=True, timeout=30,
        )
        packages = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-e"):
                continue
            if "==" in line:
                name, version = line.split("==", 1)
                packages.append({"name": name.strip(), "version": version.strip(), "purl": f"pkg:pypi/{name.strip()}@{version.strip()}"})
            elif " @ " in line:
                name = line.split(" @ ")[0].strip()
                packages.append({"name": name, "version": "unknown", "purl": f"pkg:pypi/{name}"})
        return packages
    except Exception as exc:
        print(f"pip freeze failed: {exc}", file=sys.stderr)
        return []


def generate_sbom(project_root: Path, output_path: Path) -> dict:
    venv_python = str(project_root / ".venv" / "bin" / "python3")
    packages = get_pip_freeze(venv_python)

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
