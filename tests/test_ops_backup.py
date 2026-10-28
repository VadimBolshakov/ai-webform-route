"""Bash tests of the backup scripts (ops/backup/test/run.sh) on a Docker Compose bench: MinIO, Postgres, fake
healthchecks. Skipped without Docker. Individual bash tests: AICHAT_BACKUP_TESTS="test_a test_b".
"""

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = ROOT / "ops" / "backup"
COMPOSE = BACKUP_DIR / "test" / "compose.yml"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def test_entry_point_scripts_are_executable():
    """systemd runs the scripts directly (ExecStart): in git they must have mode 100755."""
    names = ["backup.sh", "restore-test.sh", "init-repo.sh", "install.sh", "restic.sh"]
    out = subprocess.run(
        ["git", "ls-files", "-s", "--", *[f"ops/backup/{n}" for n in names]],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    modes = {line.split("\t")[1].rsplit("/", 1)[1]: line.split()[0] for line in out.splitlines()}
    assert {"backup.sh", "restore-test.sh", "init-repo.sh"} <= modes.keys()
    assert {n: m for n, m in modes.items() if m != "100755"} == {}


@pytest.mark.docker
def test_backup_scripts():
    if not _docker_available():
        pytest.skip("Docker is unavailable")
    env = {
        **os.environ,
        "RESTIC_VERSION": (BACKUP_DIR / "RESTIC_VERSION").read_text().strip(),
        "MSYS_NO_PATHCONV": "1",
    }
    # Unique project: parallel or leftover runs do not interfere with each other, down -v removes only its own.
    project = f"aichat-backup-test-{uuid.uuid4().hex[:8]}"
    compose = ["docker", "compose", "-p", project, "-f", str(COMPOSE)]
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
        assert result.returncode == 0, "run.sh: some tests failed (FAIL in the output above)"
    finally:
        subprocess.run([*compose, "down", "-v", "--remove-orphans"], env=env, timeout=300)


def test_runbook_lifecycle_matches_file():
    """The lifecycle JSON embedded in docs/backups.md matches ops/backup/aws/lifecycle.json."""
    doc = (ROOT / "docs" / "backups.md").read_text(encoding="utf-8")
    blocks = [json.loads(b) for b in re.findall(r"```json\n(.*?)```", doc, re.S)]
    lifecycle = json.loads((BACKUP_DIR / "aws" / "lifecycle.json").read_text(encoding="utf-8"))
    assert lifecycle in blocks
