#!/usr/bin/env bash
# Ночной бэкап (спека 5.4): pg_dump | restic backup → forget --prune → по воскресеньям (UTC) restic check.
# Результат — в журнал (stderr), $AICHAT_STATE_DIR/status.json и пингом HC_BACKUP_URL.
# Код выхода 1 при любой ошибке. Репозиторий не создаёт: его отсутствие — ошибка (init — только init-repo.sh).
# Вывод restic попадает в журнал, статус и пинг только с вырезанными секретами (redact в lib.sh).
set -euo pipefail
# shellcheck source=ops/backup/lib.sh
. "$(dirname "$0")/lib.sh"

load_env

# Каталог для вывода шагов (mktemp -d — права 700); удаляется при любом выходе.
WORK=$(mktemp -d)
REPORTED=0

# report_error <текст без секретов> — status.json с ошибкой и пинг /fail.
report_error() {
    REPORTED=1
    write_status status.json "$(status_json error "" "" "$1")" || log "Не удалось записать status.json"
    hc_ping "${HC_BACKUP_URL:-}" fail "$1"
}

# status_json <result> <snapshot> <size_bytes> <error> — пустые значения → null.
status_json() {
    jq -nc --arg time "$(date -u -d "@$(now_epoch)" +%Y-%m-%dT%H:%M:%SZ)" --arg result "$1" \
        --arg snapshot "$2" --arg size "$3" --argjson duration "$SECONDS" --arg error "$4" \
        '{time: $time, result: $result,
          snapshot: (if $snapshot == "" then null else $snapshot end),
          size_bytes: (if $size == "" then null else ($size | tonumber) end),
          duration_s: $duration,
          error: (if $error == "" then null else $error end)}'
}

# fail <текст без секретов> — ошибка запуска: журнал, статус, пинг, выход 1 (текст ≤ 1024 байт).
fail() {
    local LC_ALL=C
    local msg=${1:0:1024}
    log "Ошибка: $msg"
    report_error "$msg"
    exit 1
}

on_exit() {
    local rc=$?
    rm -rf "$WORK"
    if ((rc != 0 && !REPORTED)); then
        report_error "Непредвиденная ошибка backup.sh (код $rc), подробности в журнале"
    fi
}
trap on_exit EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

# emit <file> — содержимое файла без секретов.
emit() {
    local text
    text=$(<"$1")
    [[ -z "$text" ]] || printf '%s\n' "$(redact "$text")"
}

# step <название> <команда...> — stdout и stderr команды — в журнал без секретов;
# при ошибке — fail с последней содержательной строкой stderr.
step() {
    local name=$1 rc=0 msg
    shift
    "$@" >"$WORK/out" 2>"$WORK/err" </dev/null || rc=$?
    emit "$WORK/out"
    emit "$WORK/err" >&2
    if ((rc != 0)); then
        msg=$(error_text last "$(<"$WORK/err")")
        fail "$name: ${msg:-код выхода $rc}"
    fi
}

hc_ping "${HC_BACKUP_URL:-}" start

rc=0
repo_exists || rc=$?
case $rc in
    0) ;;
    1) fail "Репозиторий не найден — init не выполняется, проверьте бакет (возможна атака)" ;;
    *) fail "Нет доступа к репозиторию: $(error_text first "$REPO_EXISTS_ERROR")" ;;
esac

step "restic unlock" restic_cmd unlock

# Дамп идёт только через канал в stdin restic, на диск не пишется.
backup_args=(backup --stdin --stdin-filename aichat.dump --tag aichat-db --host aichat --json)
if [[ -n "${AICHAT_NOW:-}" ]]; then
    # Только для тестов ротации: время снапшота = AICHAT_NOW (UTC).
    backup_args+=(--time "$(date -u -d "@$AICHAT_NOW" '+%Y-%m-%d %H:%M:%S')")
fi
set +e
db_dump 2>"$WORK/dump.err" | restic_cmd "${backup_args[@]}" >"$WORK/backup.out" 2>"$WORK/backup.err"
pipe_rc=("${PIPESTATUS[@]}")
set -e
emit "$WORK/dump.err" >&2
emit "$WORK/backup.err" >&2
summary=$(jq -Rc 'fromjson? | select(.message_type? == "summary")' "$WORK/backup.out" | tail -n 1)
snapshot_id=$(jq -r '.snapshot_id // empty' <<<"$summary")
size_bytes=$(jq -r '.total_bytes_processed // empty' <<<"$summary")

if ((pipe_rc[0] != 0 || pipe_rc[1] != 0)); then
    msg=""
    if ((pipe_rc[1] != 0)); then
        err=$(error_text last "$(<"$WORK/backup.err")")
        msg="restic backup: ${err:-код выхода ${pipe_rc[1]}}"
    fi
    if ((pipe_rc[0] != 0)); then
        err=$(error_text last "$(<"$WORK/dump.err")")
        msg+="${msg:+; }pg_dump: ${err:-код выхода ${pipe_rc[0]}}"
    fi
    # restic мог сохранить снапшот из неполного дампа — такой снапшот удаляется.
    if [[ -n "$snapshot_id" ]]; then
        rc=0
        restic_cmd forget "$snapshot_id" >/dev/null 2>"$WORK/err" </dev/null || rc=$?
        if ((rc == 0)); then
            log "Снапшот ${snapshot_id:0:8} из неполного дампа удалён"
        else
            err=$(error_text last "$(<"$WORK/err")")
            msg+="; не удалось удалить снапшот ${snapshot_id:0:8} из неполного дампа: ${err:-код выхода $rc}"
        fi
    fi
    fail "$msg"
fi
[[ -n "$snapshot_id" ]] || fail "restic backup: в выводе нет итоговой строки summary"
log "Снапшот ${snapshot_id:0:8} создан, ${size_bytes:-?} байт"

step "restic forget" restic_cmd forget --tag aichat-db --keep-daily 7 --keep-weekly 4 --keep-monthly 3 --prune

if [[ "$(date -u -d "@$(now_epoch)" +%u)" == 7 ]]; then
    step "restic check" restic_cmd check
fi

write_status status.json "$(status_json ok "${snapshot_id:0:8}" "$size_bytes" "")"
REPORTED=1
hc_ping "${HC_BACKUP_URL:-}" ok
log "Бэкап завершён"
