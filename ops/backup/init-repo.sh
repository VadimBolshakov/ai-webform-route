#!/usr/bin/env bash
# Ручная инициализация репозитория restic (один раз при установке; ночной бэкап init не делает).
# Если пароля репозитория нет — создаёт его. Если репозиторий уже есть — ничего не делает.
set -euo pipefail
# shellcheck source=ops/backup/lib.sh
. "$(dirname "$0")/lib.sh"

if [[ ! -e "$RESTIC_PASSWORD_FILE" ]]; then
    (
        umask 077
        mkdir -p "$(dirname "$RESTIC_PASSWORD_FILE")"
        openssl rand -base64 48 >"$RESTIC_PASSWORD_FILE.tmp"
    )
    chmod 600 "$RESTIC_PASSWORD_FILE.tmp"
    mv -f "$RESTIC_PASSWORD_FILE.tmp" "$RESTIC_PASSWORD_FILE"
    echo "Создан пароль репозитория: $RESTIC_PASSWORD_FILE"
    echo "Без него восстановить бэкапы невозможно."
    echo "Сохраните пароль в менеджере паролей: cat $RESTIC_PASSWORD_FILE"
fi

load_env

rc=0
repo_exists || rc=$?
case "$rc" in
    0)
        echo "Репозиторий уже существует, инициализация не нужна."
        ;;
    1)
        restic_cmd init
        echo "Репозиторий создан."
        ;;
    *)
        die "Нет доступа к репозиторию: $(printf '%s\n' "$REPO_EXISTS_ERROR" | head -n 1)"
        ;;
esac
