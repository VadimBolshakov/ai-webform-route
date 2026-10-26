#!/usr/bin/env bash
# Ручная инициализация репозитория restic (один раз при установке; ночной бэкап init не делает).
# Если пароля репозитория нет — создаёт его. Если репозиторий уже есть — ничего не делает.
# Новый пароль остаётся на диске (и печатается просьба его сохранить) только после успешного
# restic init; при любой другой развязке он удаляется: иначе при переустановке остался бы
# неверный пароль к существующему репозиторию.
set -euo pipefail
# shellcheck source=ops/backup/lib.sh
. "$(dirname "$0")/lib.sh"

# Сначала env-файл: при его ошибке не должно остаться нового пароля и просьбы его сохранить.
load_env_file

PW_CREATED=0 # файл пароля создан этим запуском
PW_VALID=0   # пароль подтверждён: restic init с ним прошёл (или он открывает репозиторий)

on_exit() {
    if ((PW_CREATED && !PW_VALID)); then rm -f "$RESTIC_PASSWORD_FILE"; fi
}

announce_password() {
    ((PW_CREATED)) || return 0
    echo "Создан пароль репозитория: $RESTIC_PASSWORD_FILE"
    echo "Без него восстановить бэкапы невозможно."
    echo "Сохраните пароль в менеджере паролей: cat $RESTIC_PASSWORD_FILE"
}
trap on_exit EXIT

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
    PW_CREATED=1
    rm -f "$tmp"
fi

load_env

rc=0
repo_exists || rc=$?
case "$rc" in
    0)
        PW_VALID=1
        echo "Репозиторий уже существует, инициализация не нужна."
        announce_password
        ;;
    1)
        restic_cmd init
        PW_VALID=1
        echo "Репозиторий создан."
        announce_password
        ;;
    *)
        if ((PW_CREATED)); then
            log "Ответ restic: $(printf '%s\n' "$REPO_EXISTS_ERROR" | head -n 1)"
            die "Похоже, репозиторий уже существует (или нет доступа). Если это переустановка — положите прежний пароль из менеджера паролей в /etc/aichat/restic-password (docs/backups.md, раздел 6.2) и запустите снова."
        fi
        die "Нет доступа к репозиторию: $(printf '%s\n' "$REPO_EXISTS_ERROR" | head -n 1)"
        ;;
esac
