#!/usr/bin/env bash
# Ежемесячная проверка восстановления (спека 5.5): последний снапшот aichat-db восстанавливается
# в временную БД restore_test, в ней проверяются users и alembic_version, БД удаляется (всегда).
# Результат — в журнал (stderr), $AICHAT_STATE_DIR/restore-test.json и пингом HC_RESTORE_URL.
# Код выхода 1 при любой ошибке, в том числе если последний снапшот старше 48 часов.
# Дамп идёт только через канал restic dump | pg_restore, на диск не пишется.
# Вывод restic и psql попадает в журнал, статус и пинг только с вырезанными секретами (redact в lib.sh).
set -euo pipefail
# shellcheck source=ops/backup/lib.sh
. "$(dirname "$0")/lib.sh"

STATUS_FILE=restore-test.json
PING_URL_VAR=HC_RESTORE_URL
MAX_AGE_S=$((48 * 3600))
DB_TOUCHED=0 # временная БД создавалась (или могла создаться): trap EXIT её удаляет
snapshot=""  # короткий id снапшота
age_h=""     # возраст снапшота в часах
users=""     # число строк users в восстановленной БД
DROP_ERR=""  # текст ошибки удаления restore_test (без секретов)
LAST_ERROR="" # последняя отправленная ошибка (report_error)
DROP_SQL='DROP DATABASE IF EXISTS restore_test WITH (FORCE)' # FORCE: разрывает чужие соединения

# status_json <result> <error> — пустые значения → null.
status_json() {
    jq -nc --arg time "$(now_iso)" --arg result "$1" --arg snapshot "$snapshot" \
        --arg age "$age_h" --arg users "$users" --arg error "$2" \
        '{time: $time, result: $result,
          snapshot: (if $snapshot == "" then null else $snapshot end),
          snapshot_age_h: (if $age == "" then null else ($age | tonumber) end),
          users: (if $users == "" then null else ($users | tonumber) end),
          error: (if $error == "" then null else $error end)}'
}

status_error() { status_json error "$1"; }

load_env_or_report

# Каталог для вывода шагов (mktemp -d — права 700); удаляется при любом выходе.
WORK=$(mktemp -d)

INTERRUPTED="Проверка восстановления прервана сигналом (таймаут/остановка)"

# Удаляет временную БД и проверяет, что её нет; при неудаче текст ошибки — в DROP_ERR.
drop_db() {
    local rc=0 left
    db_psql -c "$DROP_SQL" >"$WORK/drop.out" 2>"$WORK/drop.err" </dev/null || rc=$?
    emit "$WORK/drop.err" >&2
    if ((rc == 0)); then
        left=$(db_psql -tAc "SELECT 1 FROM pg_database WHERE datname = 'restore_test'" 2>"$WORK/drop.err" </dev/null) || rc=$?
        [[ -z "$left" ]] || rc=1
    fi
    if ((rc != 0)); then
        DROP_ERR="не удалось удалить временную БД restore_test, на сервере осталась копия данных: $(error_text last "$(<"$WORK/drop.err")")"
    fi
}

on_exit() {
    local rc=$?
    ignore_signals
    if ((DB_TOUCHED)); then drop_db || true; fi
    rm -rf "$WORK"
    if [[ -n "$DROP_ERR" ]]; then
        # Даже если проверка прошла: остаток данных — ошибка, статус и пинг /fail перезаписываются.
        log "Ошибка: $DROP_ERR"
        report_error "${LAST_ERROR:+$LAST_ERROR; }$DROP_ERR"
        exit 1
    fi
    if ((rc != 0 && !REPORTED)); then
        report_error "Непредвиденная ошибка restore-test.sh (код $rc), подробности в журнале"
    fi
}
trap on_exit EXIT
trap_signals

# count_rows <таблица> — число строк таблицы восстановленной БД в ROWS; пустая таблица — ошибка.
ROWS=""
count_rows() {
    step "psql: $1" db_psql -d restore_test -tAc "SELECT count(*) FROM $1"
    ROWS=$(tr -d '[:space:]' <"$WORK/out")
    [[ "$ROWS" =~ ^[0-9]+$ ]] || fail "psql: $1: неожиданный ответ «$(error_text first "$ROWS")»"
    ((ROWS >= 1)) || fail "В восстановленной БД таблица $1 пуста"
}

hc_ping "${HC_RESTORE_URL:-}" start

STEP_SHOW_OUT=0 step "restic snapshots" restic_cmd snapshots --tag aichat-db --latest 1 --json
latest=$(jq -c '.[0] // empty' "$WORK/out" 2>/dev/null) || fail "restic snapshots: ответ не разобран"
[[ -n "$latest" ]] || fail "В репозитории нет снапшотов aichat-db: бэкапы не создаются"
snapshot_id=$(jq -r .id <<<"$latest")
snapshot=${snapshot_id:0:8}

snap_epoch=$(iso_to_epoch "$(jq -r .time <<<"$latest")") || fail "Не удалось разобрать время снапшота $snapshot"
age_s=$(($(now_epoch) - snap_epoch))
age_h=$(jq -n --argjson s "$age_s" '($s / 360 | round) / 10')
log "Последний снапшот: $snapshot, возраст $age_h ч"
((age_s >= -3600)) || fail "Время последнего снапшота в будущем более чем на час: проверьте часы сервера (или репозиторий подменён)"
((age_s <= MAX_AGE_S)) || fail "Последний снапшот старше 48 часов: бэкапы не создаются"

DB_TOUCHED=1
step "psql" db_psql -c "$DROP_SQL" -c 'CREATE DATABASE restore_test'

# Восстановление идёт только через канал, на диск дамп не пишется.
set +e
restic_cmd dump "$snapshot_id" /aichat.dump 2>"$WORK/dump.err" </dev/null \
    | db_restore -d restore_test --no-owner >"$WORK/restore.out" 2>"$WORK/restore.err"
pipe_rc=("${PIPESTATUS[@]}")
set -e
emit "$WORK/dump.err" >&2
emit "$WORK/restore.out"
emit "$WORK/restore.err" >&2
check_signal
if ((pipe_rc[0] != 0 || pipe_rc[1] != 0)); then
    msg=""
    # SIGPIPE (141) у restic после падения pg_restore — следствие, не причина.
    if ((pipe_rc[0] != 0 && !(pipe_rc[0] == 141 && pipe_rc[1] != 0))); then
        err=$(error_text last "$(<"$WORK/dump.err")")
        msg="restic dump: ${err:-код выхода ${pipe_rc[0]}}"
    fi
    if ((pipe_rc[1] != 0)); then
        err=$(error_text last "$(<"$WORK/restore.err")")
        msg+="${msg:+; }pg_restore: ${err:-код выхода ${pipe_rc[1]}}"
    fi
    fail "$msg"
fi

count_rows users
users=$ROWS
count_rows alembic_version

check_signal
write_status "$STATUS_FILE" "$(status_json ok "")"
REPORTED=1
hc_ping "${HC_RESTORE_URL:-}" ok
log "Проверка восстановления пройдена: снапшот $snapshot, users: $users"
