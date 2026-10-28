#!/usr/bin/env bash
# Nightly backup (spec 5.4): pg_dump | restic backup -> forget --prune -> restic check on Sundays (UTC).
# The result goes to the journal (stderr), $AICHAT_STATE_DIR/status.json and the HC_BACKUP_URL ping.
# Exit code 1 on any error. It does not create the repository: its absence is an error (init is init-repo.sh only).
# restic output reaches the journal, status and ping only with secrets stripped (redact in lib.sh).
# A snapshot not accepted as the result (incomplete dump, interruption by a signal) is removed (forget).
set -euo pipefail
# shellcheck source=ops/backup/lib.sh
. "$(dirname "$0")/lib.sh"

STATUS_FILE=status.json
PING_URL_VAR=HC_BACKUP_URL
ACCEPTED=0        # this run's snapshot was accepted as the backup result
PARTIAL_HANDLED=0 # removal of the unaccepted snapshot has already been attempted
FORGET_ERR=""     # error text of that removal (no secrets)
snapshot_id=""
size_bytes=""

# status_json <result> <snapshot> <size_bytes> <error> - empty values become null.
status_json() {
    jq -nc --arg time "$(now_iso)" --arg result "$1" \
        --arg snapshot "$2" --arg size "$3" --argjson duration "$SECONDS" --arg error "$4" \
        '{time: $time, result: $result,
          snapshot: (if $snapshot == "" then null else $snapshot end),
          size_bytes: (if $size == "" then null else ($size | tonumber) end),
          duration_s: $duration,
          error: (if $error == "" then null else $error end)}'
}

status_error() { status_json error "" "" "$1"; }

load_env_or_report

# Directory for step output (mktemp -d - mode 700); removed on any exit.
WORK=$(mktemp -d)

# parse_summary - snapshot_id and size_bytes from the final JSON line of restic backup (if there is one).
parse_summary() {
    local summary=""
    if [[ -s "$WORK/backup.out" ]]; then
        summary=$(jq -Rc 'fromjson? | select(.message_type? == "summary")' "$WORK/backup.out" | tail -n 1)
    fi
    snapshot_id=$(jq -r '.snapshot_id // empty' <<<"$summary")
    size_bytes=$(jq -r '.total_bytes_processed // empty' <<<"$summary")
}

# forget_partial - removes the unaccepted snapshot $snapshot_id; on error the text goes to FORGET_ERR.
forget_partial() {
    local rc=0 err
    PARTIAL_HANDLED=1
    restic_cmd forget "$snapshot_id" >/dev/null 2>"$WORK/forget.err" </dev/null || rc=$?
    emit "$WORK/forget.err" >&2
    if ((rc == 0)); then
        log "Snapshot ${snapshot_id:0:8} from the incomplete dump removed"
    else
        err=$(error_text last "$(<"$WORK/forget.err")")
        FORGET_ERR="could not remove snapshot ${snapshot_id:0:8} from the incomplete dump: ${err:-exit code $rc}"
    fi
}

INTERRUPTED="Backup interrupted by a signal (timeout/stop)"

on_exit() {
    local rc=$?
    ignore_signals
    # Safety net: this run's snapshot, if not accepted as the result, must not become latest.
    if ((!ACCEPTED && !PARTIAL_HANDLED)); then
        parse_summary || true
        if [[ -n "$snapshot_id" ]]; then forget_partial || true; fi
    fi
    rm -rf "$WORK"
    if ((rc != 0 && !REPORTED)); then
        report_error "Unexpected backup.sh error (code $rc), see the journal for details${FORGET_ERR:+; $FORGET_ERR}"
    fi
}
trap on_exit EXIT
# A signal is only noted: the current step finishes, then the incomplete snapshot is removed and the error reported.
trap_signals

hc_ping "${HC_BACKUP_URL:-}" start

rc=0
repo_exists || rc=$?
check_signal
case $rc in
    0) ;;
    1) fail "Repository not found - init is not performed, check the bucket (possible attack)" ;;
    *) fail "Cannot access the repository: $(error_text first "$REPO_EXISTS_ERROR")" ;;
esac

step "restic unlock" restic_cmd unlock

# The dump goes only through a pipe into restic's stdin; it is never written to disk.
# --retry-lock: wait (up to 30 minutes) for the lock to clear if another run's prune is still going, instead of failing.
backup_args=(backup --retry-lock 30m --stdin --stdin-filename aichat.dump --tag aichat-db --host aichat --json)
if [[ -n "${AICHAT_NOW:-}" ]]; then
    # Only for rotation tests: snapshot time = AICHAT_NOW (UTC).
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
        msg+="${msg:+; }restic backup: ${err:-exit code ${pipe_rc[1]}}"
    fi
    # SIGPIPE (141) in pg_dump after restic fails is a consequence, not the cause.
    if ((pipe_rc[0] != 0 && !(pipe_rc[1] != 0 && pipe_rc[0] == 141))); then
        err=$(error_text last "$(<"$WORK/dump.err")")
        msg+="${msg:+; }pg_dump: ${err:-exit code ${pipe_rc[0]}}"
    fi
    # restic may have saved a snapshot from an incomplete dump - such a snapshot is removed.
    if [[ -n "$snapshot_id" ]]; then
        forget_partial
        msg+="${FORGET_ERR:+; $FORGET_ERR}"
    fi
    fail "$msg"
fi
[[ -n "$snapshot_id" ]] || fail "restic backup: no final summary line in the output"
ACCEPTED=1
log "Snapshot ${snapshot_id:0:8} created, ${size_bytes:-?} bytes"

step "restic forget" restic_cmd forget --retry-lock 30m --tag aichat-db --keep-daily 7 --keep-weekly 4 --keep-monthly 3 --prune

if [[ "$(date -u -d "@$(now_epoch)" +%u)" == 7 ]]; then
    step "restic check" restic_cmd check
fi

check_signal
write_status status.json "$(status_json ok "${snapshot_id:0:8}" "$size_bytes" "")"
REPORTED=1
hc_ping "${HC_BACKUP_URL:-}" ok
log "Backup completed"
