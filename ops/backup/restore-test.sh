#!/usr/bin/env bash
# Monthly restore check (spec 5.5): the latest aichat-db snapshot is restored
# into a temporary database restore_test, users and alembic_version are checked in it, and the database is dropped (always).
# The result goes to the journal (stderr), $AICHAT_STATE_DIR/restore-test.json and the HC_RESTORE_URL ping.
# Exit code 1 on any error, including when the latest snapshot is older than 48 hours.
# The dump goes only through the restic dump | pg_restore pipe; it is never written to disk.
# restic and psql output reaches the journal, status and ping only with secrets stripped (redact in lib.sh).
set -euo pipefail
# shellcheck source=ops/backup/lib.sh
. "$(dirname "$0")/lib.sh"

STATUS_FILE=restore-test.json
PING_URL_VAR=HC_RESTORE_URL
MAX_AGE_S=$((48 * 3600))
DB_TOUCHED=0 # the temporary DB was created (or might have been): trap EXIT drops it
snapshot=""  # short snapshot id
age_h=""     # snapshot age in hours
users=""     # number of users rows in the restored DB
DROP_ERR=""  # error text of dropping restore_test (no secrets)
LAST_ERROR="" # last reported error (report_error)
DROP_SQL='DROP DATABASE IF EXISTS restore_test WITH (FORCE)' # FORCE: terminates other connections

# status_json <result> <error> - empty values become null.
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

# Directory for step output (mktemp -d - mode 700); removed on any exit.
WORK=$(mktemp -d)

INTERRUPTED="Restore check interrupted by a signal (timeout/stop)"

# Drops the temporary DB and checks that it is gone; on failure the error text goes to DROP_ERR.
drop_db() {
    local rc=0 left
    db_psql -c "$DROP_SQL" >"$WORK/drop.out" 2>"$WORK/drop.err" </dev/null || rc=$?
    emit "$WORK/drop.err" >&2
    if ((rc == 0)); then
        left=$(db_psql -tAc "SELECT 1 FROM pg_database WHERE datname = 'restore_test'" 2>"$WORK/drop.err" </dev/null) || rc=$?
        [[ -z "$left" ]] || rc=1
    fi
    if ((rc != 0)); then
        DROP_ERR="could not drop the temporary DB restore_test, a copy of the data remains on the server: $(error_text last "$(<"$WORK/drop.err")")"
    fi
}

on_exit() {
    local rc=$?
    ignore_signals
    if ((DB_TOUCHED)); then drop_db || true; fi
    rm -rf "$WORK"
    if [[ -n "$DROP_ERR" ]]; then
        # Even if the check passed: leftover data is an error, the status and the /fail ping are overwritten.
        log "Error: $DROP_ERR"
        report_error "${LAST_ERROR:+$LAST_ERROR; }$DROP_ERR"
        exit 1
    fi
    if ((rc != 0 && !REPORTED)); then
        report_error "Unexpected restore-test.sh error (code $rc), see the journal for details"
    fi
}
trap on_exit EXIT
trap_signals

# count_rows <table> - number of rows of a restored DB table into ROWS; an empty table is an error.
ROWS=""
count_rows() {
    step "psql: $1" db_psql -d restore_test -tAc "SELECT count(*) FROM $1"
    ROWS=$(tr -d '[:space:]' <"$WORK/out")
    [[ "$ROWS" =~ ^[0-9]+$ ]] || fail "psql: $1: unexpected response '$(error_text first "$ROWS")'"
    ((ROWS >= 1)) || fail "Table $1 is empty in the restored DB"
}

hc_ping "${HC_RESTORE_URL:-}" start

# --retry-lock: a backup prune running at the time holds the lock - wait for it (up to 30 minutes) instead of reporting a failure.
STEP_SHOW_OUT=0 step "restic snapshots" restic_cmd snapshots --retry-lock 30m --tag aichat-db --latest 1 --json
latest=$(jq -c '.[0] // empty' "$WORK/out" 2>/dev/null) || fail "restic snapshots: response could not be parsed"
[[ -n "$latest" ]] || fail "No aichat-db snapshots in the repository: backups are not being created"
snapshot_id=$(jq -r .id <<<"$latest")
snapshot=${snapshot_id:0:8}

snap_epoch=$(iso_to_epoch "$(jq -r .time <<<"$latest")") || fail "Could not parse the snapshot time of $snapshot"
age_s=$(($(now_epoch) - snap_epoch))
age_h=$(jq -n --argjson s "$age_s" '($s / 360 | round) / 10')
log "Latest snapshot: $snapshot, age $age_h h"
((age_s >= -3600)) || fail "The latest snapshot time is more than an hour in the future: check the server clock (or the repository was tampered with)"
((age_s <= MAX_AGE_S)) || fail "The latest snapshot is older than 48 hours: backups are not being created"

DB_TOUCHED=1
step "psql" db_psql -c "$DROP_SQL" -c 'CREATE DATABASE restore_test'

# Restoration goes only through a pipe; the dump is never written to disk.
set +e
restic_cmd dump --retry-lock 30m "$snapshot_id" /aichat.dump 2>"$WORK/dump.err" </dev/null \
    | db_restore -d restore_test --no-owner >"$WORK/restore.out" 2>"$WORK/restore.err"
pipe_rc=("${PIPESTATUS[@]}")
set -e
emit "$WORK/dump.err" >&2
emit "$WORK/restore.out"
emit "$WORK/restore.err" >&2
check_signal
if ((pipe_rc[0] != 0 || pipe_rc[1] != 0)); then
    msg=""
    # SIGPIPE (141) in restic after pg_restore fails is a consequence, not the cause.
    if ((pipe_rc[0] != 0 && !(pipe_rc[0] == 141 && pipe_rc[1] != 0))); then
        err=$(error_text last "$(<"$WORK/dump.err")")
        msg="restic dump: ${err:-exit code ${pipe_rc[0]}}"
    fi
    if ((pipe_rc[1] != 0)); then
        err=$(error_text last "$(<"$WORK/restore.err")")
        msg+="${msg:+; }pg_restore: ${err:-exit code ${pipe_rc[1]}}"
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
log "Restore check passed: snapshot $snapshot, users: $users"
