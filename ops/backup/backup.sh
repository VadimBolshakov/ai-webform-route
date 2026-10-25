#!/usr/bin/env bash
# Ночной бэкап (спека 5.4): pg_dump | restic backup → forget --prune → по воскресеньям (UTC) restic check.
# Результат — в журнал (stderr), $AICHAT_STATE_DIR/status.json и пингом HC_BACKUP_URL.
# Код выхода 1 при любой ошибке. Репозиторий не создаёт: его отсутствие — ошибка (init — только init-repo.sh).
# Вывод restic попадает в журнал, статус и пинг только с вырезанными секретами (redact в lib.sh).
# Снапшот, не принятый как результат (неполный дамп, прерывание сигналом), удаляется (forget).
set -euo pipefail
# shellcheck source=ops/backup/lib.sh
. "$(dirname "$0")/lib.sh"

REPORTED=0        # итог (status.json + пинг) уже отправлен
SIG_RC=0          # пришёл SIGTERM/SIGINT (143/130); проверяется после каждого шага
ACCEPTED=0        # снапшот этого запуска принят как результат бэкапа
PARTIAL_HANDLED=0 # удаление непринятого снапшота уже выполнялось
FORGET_ERR=""     # текст ошибки этого удаления (без секретов)
snapshot_id=""
size_bytes=""

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

# Ошибка env-файла или пароля: URL пинга неизвестен — только журнал и status.json (старый «ok» не остаётся).
if ! env_err=$( (load_env) 2>&1 >/dev/null); then
    printf '%s\n' "$env_err" >&2
    env_err=$(error_text last "$env_err")
    write_status status.json "$(status_json error "" "" "${env_err#aichat-backup: }")" \
        || log "Не удалось записать status.json"
    exit 1
fi
load_env

# Каталог для вывода шагов (mktemp -d — права 700); удаляется при любом выходе.
WORK=$(mktemp -d)

# report_error <текст без секретов> — status.json с ошибкой и пинг /fail.
report_error() {
    REPORTED=1
    write_status status.json "$(status_json error "" "" "$1")" || log "Не удалось записать status.json"
    hc_ping "${HC_BACKUP_URL:-}" fail "$1"
}

# fail <текст без секретов> — ошибка запуска: журнал, статус, пинг, выход 1 (текст ≤ 1024 байт).
fail() {
    local LC_ALL=C
    local msg=${1:0:1024}
    log "Ошибка: $msg"
    report_error "$msg"
    exit 1
}

# emit <file> — содержимое файла без секретов.
emit() {
    local text
    text=$(<"$1")
    [[ -z "$text" ]] || printf '%s\n' "$(redact "$text")"
}

# parse_summary — snapshot_id и size_bytes из итоговой JSON-строки restic backup (если она есть).
parse_summary() {
    local summary=""
    if [[ -s "$WORK/backup.out" ]]; then
        summary=$(jq -Rc 'fromjson? | select(.message_type? == "summary")' "$WORK/backup.out" | tail -n 1)
    fi
    snapshot_id=$(jq -r '.snapshot_id // empty' <<<"$summary")
    size_bytes=$(jq -r '.total_bytes_processed // empty' <<<"$summary")
}

# forget_partial — удаляет непринятый снапшот $snapshot_id; при ошибке текст — в FORGET_ERR.
forget_partial() {
    local rc=0 err
    PARTIAL_HANDLED=1
    restic_cmd forget "$snapshot_id" >/dev/null 2>"$WORK/forget.err" </dev/null || rc=$?
    emit "$WORK/forget.err" >&2
    if ((rc == 0)); then
        log "Снапшот ${snapshot_id:0:8} из неполного дампа удалён"
    else
        err=$(error_text last "$(<"$WORK/forget.err")")
        FORGET_ERR="не удалось удалить снапшот ${snapshot_id:0:8} из неполного дампа: ${err:-код выхода $rc}"
    fi
}

INTERRUPTED="Бэкап прерван сигналом (таймаут/остановка)"
check_signal() {
    ((SIG_RC == 0)) || fail "$INTERRUPTED"
}

on_exit() {
    local rc=$?
    # Повторный сигнал не должен прервать запись статуса и пинг /fail.
    trap '' TERM INT
    # Страховка: снапшот этого запуска, не принятый как результат, не должен стать latest.
    if ((!ACCEPTED && !PARTIAL_HANDLED)); then
        parse_summary || true
        if [[ -n "$snapshot_id" ]]; then forget_partial || true; fi
    fi
    rm -rf "$WORK"
    if ((rc != 0 && !REPORTED)); then
        report_error "Непредвиденная ошибка backup.sh (код $rc), подробности в журнале${FORGET_ERR:+; $FORGET_ERR}"
    fi
}
trap on_exit EXIT
# Сигнал только отмечается: текущий шаг завершается, затем — удаление неполного снапшота и ошибка.
trap 'SIG_RC=143' TERM
trap 'SIG_RC=130' INT

# step <название> <команда...> — stdout и stderr команды — в журнал без секретов;
# при ошибке — fail с последней содержательной строкой stderr.
step() {
    local name=$1 rc=0 msg
    shift
    "$@" >"$WORK/out" 2>"$WORK/err" </dev/null || rc=$?
    emit "$WORK/out"
    emit "$WORK/err" >&2
    check_signal
    if ((rc != 0)); then
        msg=$(error_text last "$(<"$WORK/err")")
        fail "$name: ${msg:-код выхода $rc}"
    fi
}

hc_ping "${HC_BACKUP_URL:-}" start

rc=0
repo_exists || rc=$?
check_signal
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
parse_summary

if ((SIG_RC != 0 || pipe_rc[0] != 0 || pipe_rc[1] != 0)); then
    msg=""
    if ((SIG_RC != 0)); then msg=$INTERRUPTED; fi
    if ((pipe_rc[1] != 0)); then
        err=$(error_text last "$(<"$WORK/backup.err")")
        msg+="${msg:+; }restic backup: ${err:-код выхода ${pipe_rc[1]}}"
    fi
    # SIGPIPE (141) у pg_dump после падения restic — следствие, не причина.
    if ((pipe_rc[0] != 0 && !(pipe_rc[1] != 0 && pipe_rc[0] == 141))); then
        err=$(error_text last "$(<"$WORK/dump.err")")
        msg+="${msg:+; }pg_dump: ${err:-код выхода ${pipe_rc[0]}}"
    fi
    # restic мог сохранить снапшот из неполного дампа — такой снапшот удаляется.
    if [[ -n "$snapshot_id" ]]; then
        forget_partial
        msg+="${FORGET_ERR:+; $FORGET_ERR}"
    fi
    fail "$msg"
fi
[[ -n "$snapshot_id" ]] || fail "restic backup: в выводе нет итоговой строки summary"
ACCEPTED=1
log "Снапшот ${snapshot_id:0:8} создан, ${size_bytes:-?} байт"

step "restic forget" restic_cmd forget --tag aichat-db --keep-daily 7 --keep-weekly 4 --keep-monthly 3 --prune

if [[ "$(date -u -d "@$(now_epoch)" +%u)" == 7 ]]; then
    step "restic check" restic_cmd check
fi

check_signal
write_status status.json "$(status_json ok "${snapshot_id:0:8}" "$size_bytes" "")"
REPORTED=1
hc_ping "${HC_BACKUP_URL:-}" ok
log "Бэкап завершён"
