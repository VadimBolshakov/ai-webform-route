#!/usr/bin/env bash
# restic с настройками сервера (контейнер restic/restic, /etc/aichat/restic.env, пароль из файла) —
# для ручных команд из docs/backups.md. Пример: sudo /opt/aichat/ops/backup/restic.sh snapshots
set -euo pipefail
# shellcheck source=ops/backup/lib.sh
. "$(dirname "$0")/lib.sh"

((EUID == 0)) || die "Запустите от root: sudo $0 $*"
load_env
restic_cmd "$@"
