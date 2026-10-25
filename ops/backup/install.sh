#!/usr/bin/env bash
# Установка бэкапов на сервер (docs/backups.md, раздел 4). Запуск: sudo /opt/aichat/ops/backup/install.sh
# Повторный запуск безопасен. Первый проход создаёт /etc/aichat/restic.env из шаблона и просит его
# заполнить; второй — скачивает образ restic, создаёт репозиторий (если его нет) и включает таймеры.
set -euo pipefail
# shellcheck source=ops/backup/lib.sh
. "$(dirname "$0")/lib.sh"

BACKUP_DIR=$AICHAT_LIB_DIR
UNITS=(aichat-backup.service aichat-backup.timer aichat-restore-test.service aichat-restore-test.timer)

((EUID == 0)) || die "Запустите от root: sudo $0"
# Юниты systemd запускают скрипты по этому пути.
[[ "$BACKUP_DIR" == /opt/aichat/ops/backup ]] \
    || die "Репозиторий должен лежать в /opt/aichat (сейчас скрипт в $BACKUP_DIR)"

# jq нужен скриптам бэкапа; в Ubuntu Server его нет по умолчанию. curl и openssl есть.
if ! command -v jq >/dev/null; then
    apt-get update -q
    apt-get install -y -q jq
fi
command -v docker >/dev/null || die "Не найден docker: сначала установите его (docs/deploy.md, раздел 3)"

install -d -m 700 -o root -g root /etc/aichat /var/lib/aichat-backup /var/cache/aichat-restic

if [[ ! -e "$AICHAT_BACKUP_ENV" ]]; then
    install -m 600 -o root -g root "$BACKUP_DIR/restic.env.example" "$AICHAT_BACKUP_ENV"
    echo "Создан $AICHAT_BACKUP_ENV из шаблона."
    echo "Заполните его (sudo nano $AICHAT_BACKUP_ENV) и запустите установку ещё раз:"
    echo "  sudo $0"
    exit 0
fi
chown root:root "$AICHAT_BACKUP_ENV"
chmod 600 "$AICHAT_BACKUP_ENV"

set -a
# shellcheck source=/dev/null
. "$AICHAT_BACKUP_ENV" || die "Не удалось прочитать $AICHAT_BACKUP_ENV"
set +a
missing=()
for var in RESTIC_REPOSITORY AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION; do
    [[ -n "${!var:-}" ]] || missing+=("$var")
done
((${#missing[@]} == 0)) || die "В $AICHAT_BACKUP_ENV не заполнено: ${missing[*]}"
for var in HC_BACKUP_URL HC_RESTORE_URL; do
    [[ -n "${!var:-}" ]] || log "Внимание: $var не задан — пинги healthchecks.io отправляться не будут"
done

docker pull "restic/restic:$(<"$BACKUP_DIR/RESTIC_VERSION")"
"$BACKUP_DIR/init-repo.sh"

for unit in "${UNITS[@]}"; do
    install -m 644 -o root -g root "$BACKUP_DIR/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now aichat-backup.timer aichat-restore-test.timer
systemctl list-timers --no-pager 'aichat-*'
echo "Готово: таймеры бэкапа включены."
