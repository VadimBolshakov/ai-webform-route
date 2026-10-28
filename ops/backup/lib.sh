# shellcheck shell=bash
# Shared functions of the backup scripts. Usage: . "$(dirname "$0")/lib.sh"
# The variables below can be overridden by the environment (tests: ops/backup/test/compose.yml);
# without an override the server values apply.

AICHAT_LIB_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

: "${AICHAT_BACKUP_ENV:=/etc/aichat/restic.env}"
: "${RESTIC_PASSWORD_FILE:=/etc/aichat/restic-password}"
: "${AICHAT_STATE_DIR:=/var/lib/aichat-backup}"
if [[ -z "${RESTIC_CMD:-}" ]]; then
    RESTIC_CMD="docker run --rm -i --hostname aichat --env-file $(printf %q "$AICHAT_BACKUP_ENV")"
    RESTIC_CMD+=" -e RESTIC_PASSWORD_FILE=/run/restic-password"
    RESTIC_CMD+=" -v $(printf %q "$RESTIC_PASSWORD_FILE:/run/restic-password:ro")"
    RESTIC_CMD+=" -v /var/cache/aichat-restic:/root/.cache/restic"
    RESTIC_CMD+=" restic/restic:$(<"$AICHAT_LIB_DIR/RESTIC_VERSION")"
fi
: "${PG_DUMP_CMD:=docker compose --project-directory /opt/aichat exec -T db pg_dump -U aichat -Fc aichat}"
: "${PSQL_CMD:=docker compose --project-directory /opt/aichat exec -T db psql -U aichat -v ON_ERROR_STOP=1}"
: "${PG_RESTORE_CMD:=docker compose --project-directory /opt/aichat exec -T db pg_restore -U aichat}"
# For restic run without docker (tests), the password is taken from this file.
export RESTIC_PASSWORD_FILE

log() { printf 'aichat-backup: %s\n' "$*" >&2; }

die() {
    log "$*"
    exit 1
}

# Loads /etc/aichat/restic.env (KEY=VALUE without quotes) and exports the variables.
load_env_file() {
    [[ -f "$AICHAT_BACKUP_ENV" ]] || die "Missing $AICHAT_BACKUP_ENV"
    set -a
    # shellcheck source=/dev/null
    . "$AICHAT_BACKUP_ENV" || die "Could not read $AICHAT_BACKUP_ENV"
    set +a
    [[ -n "${RESTIC_REPOSITORY:-}" ]] || die "RESTIC_REPOSITORY is not set in $AICHAT_BACKUP_ENV"
}

# load_env_file + check that the repository password file exists and is not empty.
load_env() {
    load_env_file
    [[ -s "$RESTIC_PASSWORD_FILE" ]] || die "Repository password file $RESTIC_PASSWORD_FILE is missing (or empty)"
}

# Commands from variables run via eval: they may contain quotes (overrides in tests).
restic_cmd() { eval "$RESTIC_CMD"' "$@"'; }
db_dump() { eval "$PG_DUMP_CMD"; }
db_psql() { eval "$PSQL_CMD"' "$@"'; }
db_restore() { eval "$PG_RESTORE_CMD"' "$@"'; }

# repo_exists: 0 - the repository exists; 1 - restic reported there is no repository (code 10 or the text
# "repository does not exist"); 2 - another error
# (network, keys, password). It does not print restic output; for 1 and 2 the error text is in REPO_EXISTS_ERROR.
# On network errors and wrong keys restic retries the request for up to 15 minutes, so the check
# is limited to AICHAT_REPO_CHECK_TIMEOUT seconds (default 120); expiry gives code 2.
: "${AICHAT_REPO_CHECK_TIMEOUT:=120}"
# shellcheck disable=SC2034  # REPO_EXISTS_ERROR is read by the calling scripts
REPO_EXISTS_ERROR=""
# shellcheck disable=SC2034  # REPO_EXISTS_ERROR is assigned inside the function
repo_exists() {
    local rc=0 out retry
    [[ "$AICHAT_REPO_CHECK_TIMEOUT" =~ ^[0-9]+$ ]] \
        || die "AICHAT_REPO_CHECK_TIMEOUT must be an integer number of seconds"
    out=$(eval "timeout $AICHAT_REPO_CHECK_TIMEOUT $RESTIC_CMD cat config" 2>&1 >/dev/null </dev/null) || rc=$?
    REPO_EXISTS_ERROR=$out
    if ((rc == 0)); then
        REPO_EXISTS_ERROR=""
        return 0
    fi
    if ((rc == 124)); then
        retry=$(printf '%s\n' "$out" | grep -m 1 'returned error, retrying' | sed 's/^.*retrying after [^:]*: //') || true
        REPO_EXISTS_ERROR="restic did not answer within $AICHAT_REPO_CHECK_TIMEOUT s (network or S3 unavailable?)${retry:+: $retry}"
        return 2
    fi
    if ((rc == 10)) || [[ "$out" == *"repository does not exist"* ]]; then
        return 1
    fi
    return 2
}

# hc_ping <url> <start|ok|fail> [body] - healthchecks.io ping; a ping failure does not change the exit code.
# The URL is passed to curl via a config on a descriptor, not as an argument (not visible in ps); it is not written to the journal.
hc_ping() {
    local url=${1:-} suffix=${2:-} body=${3-} target rc=0
    if [[ -z "$url" ]]; then
        log "healthchecks: URL not set, ping skipped"
        return 0
    fi
    # The URL goes into the curl config in quotes: a quote, backslash and newline are not allowed.
    if [[ "$url" == *[\"\\]* || "$url" == *$'\n'* ]]; then
        log "healthchecks: invalid characters in URL, ping skipped"
        return 0
    fi
    url=${url%/}
    case "$suffix" in
        start) target="$url/start" ;;
        ok) target="$url" ;;
        fail) target="$url/fail" ;;
        *)
            log "healthchecks: unknown ping type '$suffix', ping skipped"
            return 0
            ;;
    esac
    local LC_ALL=C
    body=${body:0:1024}
    printf '%s' "$body" | curl -fsS -m 10 --retry 3 -o /dev/null --data-binary @- \
        -K <(printf 'url = "%s"\n' "$target") 2>/dev/null || rc=$?
    if ((rc != 0)); then
        log "healthchecks: ping '$suffix' not delivered (curl, code $rc)"
    fi
    return 0
}

# write_status <file> <json> - atomic write; file is a name in $AICHAT_STATE_DIR or a full path.
write_status() {
    local file=$1 json=$2 tmp
    [[ "$file" == */* ]] || file="$AICHAT_STATE_DIR/$file"
    [[ -d "$AICHAT_STATE_DIR" ]] || install -d -m 700 "$AICHAT_STATE_DIR"
    tmp=$(mktemp "$file.XXXXXX")
    printf '%s\n' "$json" >"$tmp"
    mv -f "$tmp" "$file"
}

now_epoch() { printf '%s\n' "${AICHAT_NOW:-$(date +%s)}"; }

# redact <text> - prints text (without a trailing newline), replacing secret values with "***":
# the repository password, AWS keys, healthchecks.io ping URLs.
redact() {
    local text=$1 secret pw="" hb=${HC_BACKUP_URL:-} hr=${HC_RESTORE_URL:-}
    if [[ -r "$RESTIC_PASSWORD_FILE" ]]; then pw=$(<"$RESTIC_PASSWORD_FILE"); fi
    for secret in "$pw" "${AWS_SECRET_ACCESS_KEY:-}" "$hb" "${hb%/}" "$hr" "${hr%/}" "${AWS_ACCESS_KEY_ID:-}"; do
        if [[ -n "$secret" ]]; then text=${text//"$secret"/"***"}; fi
    done
    printf '%s' "$text"
}

# error_text <first|last> <text> - the first or last non-empty line of text (for a JSON error
# from restic --json - its message field) without secrets, truncated to 1024 bytes; empty if there are no lines.
error_text() {
    local which=$1 text=$2 line msg
    if [[ "$which" == first ]]; then
        line=$(awk 'NF {print; exit}' <<<"$text")
    else
        line=$(awk 'NF {l = $0} END {printf "%s", l}' <<<"$text")
    fi
    if [[ "$line" == "{"* ]] \
        && msg=$(jq -er '(.message // .error.message) | strings' <<<"$line" 2>/dev/null); then
        line=${msg//$'\n'/ }
    fi
    line=$(redact "$line")
    local LC_ALL=C
    printf '%s' "${line:0:1024}"
}

# iso_to_epoch <RFC3339 time> - epoch; fractional seconds (restic writes nanoseconds) and a timezone offset are allowed.
iso_to_epoch() {
    local t=$1
    if [[ "$t" =~ ^(.*:[0-9]{2})\.[0-9]+(.*)$ ]]; then t=${BASH_REMATCH[1]}${BASH_REMATCH[2]}; fi
    date -u -d "$t" +%s 2>/dev/null
}

# now_iso - now_epoch as a UTC time like 2026-01-01T03:30:00Z.
now_iso() { date -u -d "@$(now_epoch)" +%Y-%m-%dT%H:%M:%SZ; }

# --- reporting script skeleton (backup.sh, restore-test.sh) -------------------------------------
# Before calling these functions the script sets: STATUS_FILE (name in $AICHAT_STATE_DIR), PING_URL_VAR (name of the
# variable holding the ping URL, e.g. HC_BACKUP_URL), INTERRUPTED (error text on a signal),
# the function status_error <text> (prints the status JSON with an error) and, after load_env_or_report,
# the WORK directory for step output (mktemp -d - mode 700). The functions strip secrets (redact).
# shellcheck disable=SC2034  # REPORTED and SIG_RC are read and changed by the calling scripts
REPORTED=0 # the result (status + ping) has already been sent
# shellcheck disable=SC2034
SIG_RC=0 # SIGTERM/SIGINT arrived (143/130); checked after each step

# Env-file or password error: the ping URL is unknown - only the journal and status (a stale "ok" does not remain).
# Otherwise loads the environment into the current shell.
load_env_or_report() {
    local env_err
    if ! env_err=$( (load_env) 2>&1 >/dev/null); then
        printf '%s\n' "$env_err" >&2
        env_err=$(error_text last "$env_err")
        write_status "$STATUS_FILE" "$(status_error "${env_err#aichat-backup: }")" \
            || log "Could not write $STATUS_FILE"
        exit 1
    fi
    load_env
}

# report_error <text without secrets> - status with an error and a /fail ping.
report_error() {
    # shellcheck disable=SC2034  # REPORTED and LAST_ERROR are read by the caller's trap EXIT
    REPORTED=1
    # shellcheck disable=SC2034
    LAST_ERROR=$1
    write_status "$STATUS_FILE" "$(status_error "$1")" || log "Could not write $STATUS_FILE"
    hc_ping "${!PING_URL_VAR:-}" fail "$1"
}

# fail <text without secrets> - startup error: journal, status, ping, exit 1 (text <= 1024 bytes).
fail() {
    local LC_ALL=C
    local msg=${1:0:1024}
    log "Error: $msg"
    report_error "$msg"
    exit 1
}

# A signal is only noted: the current step finishes, then the script reports the error (check_signal).
trap_signals() {
    trap 'SIG_RC=143' TERM
    trap 'SIG_RC=130' INT
}
# For trap EXIT: a repeated signal must not interrupt writing the status and the /fail ping.
ignore_signals() { trap '' TERM INT; }

check_signal() {
    ((SIG_RC == 0)) || fail "$INTERRUPTED"
}

# emit <file> - file contents without secrets.
emit() {
    local text
    text=$(<"$1")
    [[ -z "$text" ]] || printf '%s\n' "$(redact "$text")"
}

# step <name> <command...> - the command's stdout and stderr (in $WORK/out and $WORK/err) go to the journal without
# secrets (stdout unless STEP_SHOW_OUT=0); on error - fail with the last meaningful stderr line.
step() {
    local name=$1 rc=0 msg
    shift
    "$@" >"$WORK/out" 2>"$WORK/err" </dev/null || rc=$?
    if [[ "${STEP_SHOW_OUT:-1}" != 0 ]]; then emit "$WORK/out"; fi
    emit "$WORK/err" >&2
    check_signal
    if ((rc != 0)); then
        msg=$(error_text last "$(<"$WORK/err")")
        fail "$name: ${msg:-exit code $rc}"
    fi
}
