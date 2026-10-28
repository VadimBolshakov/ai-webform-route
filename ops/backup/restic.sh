#!/usr/bin/env bash
# restic with the server settings (restic/restic container, /etc/aichat/restic.env, password from a file) -
# for the manual commands in docs/backups.md. Example: sudo /opt/aichat/ops/backup/restic.sh snapshots
set -euo pipefail
# shellcheck source=ops/backup/lib.sh
. "$(dirname "$0")/lib.sh"

((EUID == 0)) || die "Run as root: sudo $0 $*"
load_env
restic_cmd "$@"
