#!/usr/bin/env bash
# Ручная инициализация репозитория restic (один раз при установке; ночной бэкап init не делает).
# Если пароля репозитория нет — создаёт его. Если репозиторий уже есть — ничего не делает.
set -euo pipefail
# shellcheck source=ops/backup/lib.sh
. "$(dirname "$0")/lib.sh"

# Сначала env-файл: при его ошибке не должно остаться нового пароля и просьбы его сохранить.
load_env_file

if [[ ! -e "$RESTIC_PASSWORD_FILE" ]]; then
    tmp="$RESTIC_PASSWORD_FILE.tmp"
    rm -f "$tmp"
    (
        umask 077
        mkdir -p "$(dirname "$RESTIC_PASSWORD_FILE")"
        openssl rand -base64 48 >"$tmp"
    )
    chmod 600 "$tmp"
    # Публикация без перезаписи: если файл пароля успел появиться — не трогаем его.
    if ! ln "$tmp" "$RESTIC_PASSWORD_FILE" 2>/dev/null; then
        rm -f "$tmp"
        die "Файл пароля $RESTIC_PASSWORD_FILE появился во время работы — запустите init-repo.sh снова"
    fi
    rm -f "$tmp"
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
