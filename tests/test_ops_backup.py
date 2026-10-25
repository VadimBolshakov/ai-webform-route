"""Bash-тесты скриптов бэкапа (ops/backup/test/run.sh) на стенде Docker Compose: MinIO, Postgres, фейковый
healthchecks. Без Docker тест пропускается. Отдельные bash-тесты: AICHAT_BACKUP_TESTS="test_a test_b".
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = ROOT / "ops" / "backup"
COMPOSE = BACKUP_DIR / "test" / "compose.yml"
PROJECT = "aichat-backup-test"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.docker
def test_backup_scripts():
    if not _docker_available():
        pytest.skip("Docker недоступен")
    env = {
        **os.environ,
        "RESTIC_VERSION": (BACKUP_DIR / "RESTIC_VERSION").read_text().strip(),
        "MSYS_NO_PATHCONV": "1",
    }
    compose = ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE)]
    selected = os.environ.get("AICHAT_BACKUP_TESTS", "").split()
    try:
        subprocess.run(
            [*compose, "up", "-d", "--build", "--wait"], env=env, check=True, timeout=900
        )
        result = subprocess.run(
            [*compose, "run", "--rm", "runner", "bash", "/work/ops/backup/test/run.sh", *selected],
            env=env,
            timeout=1800,
        )
        assert result.returncode == 0, "run.sh: есть упавшие тесты (FAIL в выводе выше)"
    finally:
        subprocess.run([*compose, "down", "-v", "--remove-orphans"], env=env, timeout=300)
