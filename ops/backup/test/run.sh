#!/usr/bin/env bash
# Tests of the backup scripts. They run inside the runner (ops/backup/test/compose.yml):
#   bash /work/ops/backup/test/run.sh            - all test_* functions
#   bash /work/ops/backup/test/run.sh test_a ... - only the listed ones
# Before each test: an empty bucket (all versions), no password file, empty state and hc log.
# Prints PASS/FAIL for each test; exit code 1 on any FAIL.
# SC2016: run_lib deliberately takes bash code in single quotes (expanded in the child bash).
# shellcheck disable=SC2016
set -euo pipefail

BACKUP_DIR=/work/ops/backup
TEST_DIR=$BACKUP_DIR/test
HC_URL=http://hc:8080
HC_LOG=/tmp/hc/requests.jsonl
BUCKET=aichat-test
OUT=/tmp/test-out   # output of the command under test (see run_script / run_lib)

# mc talks to MinIO with the keys from the test env file.
# shellcheck source=/dev/null
MC_HOST_t=$(set -a; . "$AICHAT_BACKUP_ENV"; printf 'http://%s:%s@minio:9000' "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY")
export MC_HOST_t

# --- helpers -------------------------------------------------------------

fail() { echo "  assert: $*" >&2; return 1; }

reset_env() {
    mc rm --recursive --versions --force "t/$BUCKET" >/dev/null 2>&1 || true
    [[ -z "$(mc ls --versions --recursive "t/$BUCKET")" ]] || fail "bucket not emptied"
    rm -f "$RESTIC_PASSWORD_FILE" "$RESTIC_PASSWORD_FILE.tmp"
    rm -rf "$AICHAT_STATE_DIR" "${HOME:-/root}/.cache/restic" "$OUT"
    curl -fsS -X POST "$HC_URL/reset" >/dev/null
}

# Repository password with a known value (for tests that do not care who created it).
use_test_password() { install -m 600 "$TEST_DIR/test-password" "$RESTIC_PASSWORD_FILE"; }

# run_script <script> [args] - runs ops/backup/<script>; stdout+stderr -> $OUT; returns the exit code.
run_script() {
    local script=$1 rc=0
    shift
    timeout 300 bash "$BACKUP_DIR/$script" "$@" >"$OUT" 2>&1 || rc=$?
    return "$rc"
}

# run_lib '<code>' - runs the code in a separate bash with lib.sh sourced; stdout+stderr -> $OUT.
run_lib() {
    local rc=0
    timeout 300 bash -c ". '$BACKUP_DIR/lib.sh'; $1" >"$OUT" 2>&1 || rc=$?
    return "$rc"
}

# rc_of <command...> - prints the command's exit code (without aborting the test).
rc_of() { local rc=0; "$@" || rc=$?; echo "$rc"; }

hc_paths() { jq -r .path "$HC_LOG"; }

# assert_no_secrets <file> - the file contains neither the real AWS key nor the wrong one substituted in tests,
# nor the repository password (if the password file exists), nor the backup and restore-test ping URLs.
assert_no_secrets() {
    local file=$1 secret hc_url restore_ping pw=""
    # shellcheck source=/dev/null
    secret=$(set -a; . "$AICHAT_BACKUP_ENV"; printf '%s' "$AWS_SECRET_ACCESS_KEY")
    # shellcheck source=/dev/null
    hc_url=$(set -a; . "$AICHAT_BACKUP_ENV"; printf '%s' "$HC_BACKUP_URL")
    # shellcheck source=/dev/null
    restore_ping=$(set -a; . "$AICHAT_BACKUP_ENV"; printf '%s' "$HC_RESTORE_URL")
    [[ -s "$RESTIC_PASSWORD_FILE" ]] && pw=$(cat "$RESTIC_PASSWORD_FILE")
    ! grep -qF "$secret" "$file" || fail "AWS key in the output"
    ! grep -qF wrong-secret "$file" || fail "wrong AWS key in the output"
    ! grep -qF "$hc_url" "$file" || fail "ping URL in the output"
    ! grep -qF "$restore_ping" "$file" || fail "restore-test ping URL in the output"
    [[ -z "$pw" ]] || ! grep -qF "$pw" "$file" || fail "repository password in the output"
}

# restic_t <args> - restic with the test env file (without lib.sh).
restic_t() (
    set -a
    # shellcheck source=/dev/null
    . "$AICHAT_BACKUP_ENV"
    set +a
    exec restic "$@"
)

# Repository with the test password (as after init-repo.sh).
init_test_repo() {
    use_test_password
    restic_t init >/dev/null 2>&1 || fail "restic init"
}

# /tmp/leaky-restic - restic that prints secrets to stderr on every call and fails on forget.
make_leaky_restic() {
    cat >/tmp/leaky-restic <<'EOF'
#!/bin/sh
echo "leak: $AWS_SECRET_ACCESS_KEY $AWS_ACCESS_KEY_ID $HC_BACKUP_URL $(cat "$RESTIC_PASSWORD_FILE")" >&2
if [ "$1" = forget ]; then
    echo "Fatal: forget $AWS_SECRET_ACCESS_KEY $(cat "$RESTIC_PASSWORD_FILE")" >&2
    exit 1
fi
exec restic "$@"
EOF
    chmod +x /tmp/leaky-restic
}

# assert_no_key_id <file> - the file has no AWS key ID.
assert_no_key_id() {
    local key_id
    # shellcheck source=/dev/null
    key_id=$(set -a; . "$AICHAT_BACKUP_ENV"; printf '%s' "$AWS_ACCESS_KEY_ID")
    ! grep -qF "$key_id" "$1" || fail "AWS key ID in the output"
}

snapshots_json() { restic_t snapshots --tag aichat-db --json; }
snapshot_count() { snapshots_json | jq length; }
STATUS=$AICHAT_STATE_DIR/status.json
RESTORE_STATUS=$AICHAT_STATE_DIR/restore-test.json

# db_exists <name> - 0 if the DB exists on the server.
db_exists() { [[ -n "$(psql -h db -U aichat -d aichat -tAc "SELECT 1 FROM pg_database WHERE datname='$1'")" ]]; }
# Backup time in the restore-test tests (epoch, UTC).
RT_BASE=$(date -u -d '2026-01-01 03:30:00' +%s)

# --- tests: init-repo.sh and lib.sh ------------------------------------------

test_init_creates_repo() {
    run_script init-repo.sh || { cat "$OUT"; fail "init-repo.sh returned an error"; }
    [[ -s "$RESTIC_PASSWORD_FILE" ]] || fail "password file not created"
    [[ "$(stat -c %a "$RESTIC_PASSWORD_FILE")" == 600 ]] || fail "password file mode is not 600"
    grep -q "Save the password in a password manager: cat $RESTIC_PASSWORD_FILE" "$OUT" \
        || fail "no instruction to save the password"
    ! grep -qF "$(cat "$RESTIC_PASSWORD_FILE")" "$OUT" || fail "password printed"
    [[ "$(rc_of run_lib 'load_env; repo_exists')" == 0 ]] || fail "repo_exists after init != 0"
}

test_init_twice_is_noop() {
    run_script init-repo.sh || { cat "$OUT"; fail "first init failed"; }
    local pw
    pw=$(cat "$RESTIC_PASSWORD_FILE")
    run_script init-repo.sh || { cat "$OUT"; fail "second init returned an error"; }
    grep -q "Repository already exists" "$OUT" || { cat "$OUT"; fail "no 'already exists' message"; }
    ! grep -q "Save the password" "$OUT" || fail "password generated again"
    [[ "$(cat "$RESTIC_PASSWORD_FILE")" == "$pw" ]] || fail "password file changed"
}

test_init_fails_on_access_error() {
    use_test_password
    local env=/tmp/bad-key.env
    sed 's/^AWS_SECRET_ACCESS_KEY=.*/AWS_SECRET_ACCESS_KEY=wrong-secret/' "$AICHAT_BACKUP_ENV" >"$env"
    [[ "$(rc_of env AICHAT_BACKUP_ENV="$env" bash -c "bash '$BACKUP_DIR/init-repo.sh' >'$OUT' 2>&1")" == 1 ]] \
        || { cat "$OUT"; fail "init with a wrong key did not fail"; }
    grep -q "Cannot access the repository" "$OUT" || { cat "$OUT"; fail "no access error text"; }
    [[ -z "$(mc ls --recursive "t/$BUCKET")" ]] || fail "init wrote something to the bucket"
    assert_no_secrets "$OUT"
}

test_init_bad_env_leaves_no_password() {
    local rc
    rc=$(rc_of env AICHAT_BACKUP_ENV=/nonexistent bash -c "bash '$BACKUP_DIR/init-repo.sh' >'$OUT' 2>&1")
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "init without env file: code $rc"; }
    grep -q "Missing /nonexistent" "$OUT" || { cat "$OUT"; fail "no missing env file text"; }

    local env=/tmp/no-repo.env
    grep -v '^RESTIC_REPOSITORY=' "$AICHAT_BACKUP_ENV" >"$env"
    rc=$(rc_of env AICHAT_BACKUP_ENV="$env" bash -c "bash '$BACKUP_DIR/init-repo.sh' >>'$OUT' 2>&1")
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "init without RESTIC_REPOSITORY: code $rc"; }
    grep -q "RESTIC_REPOSITORY is not set" "$OUT" || { cat "$OUT"; fail "no RESTIC_REPOSITORY text"; }

    [[ ! -e "$RESTIC_PASSWORD_FILE" && ! -e "$RESTIC_PASSWORD_FILE.tmp" ]] || fail "password file left behind"
    ! grep -q "Save the password" "$OUT" || fail "request to save a nonexistent password"
}

# Reinstall: the repository is in the bucket but there is no password file - the new (wrong) password does not stay.
test_init_existing_repo_without_password() {
    init_test_repo
    rm -f "$RESTIC_PASSWORD_FILE"
    local rc
    rc=$(rc_of run_script init-repo.sh)
    [[ "$rc" != 0 ]] || { cat "$OUT"; fail "init without a password and with an existing repository did not fail"; }
    [[ ! -e "$RESTIC_PASSWORD_FILE" && ! -e "$RESTIC_PASSWORD_FILE.tmp" ]] || fail "new password file left behind"
    ! grep -q "Save the password" "$OUT" || { cat "$OUT"; fail "request to save a wrong password"; }
    grep -q "The repository seems to exist already (or access is denied). If this is a reinstall, put the previous password from your password manager into /etc/aichat/restic-password (docs/backups.md, section 6.2) and run again." "$OUT" \
        || { cat "$OUT"; fail "no hint about the previous password"; }
    # With the previous password - "already exists".
    use_test_password
    run_script init-repo.sh || { cat "$OUT"; fail "init with the previous password failed"; }
    grep -q "Repository already exists" "$OUT" || { cat "$OUT"; fail "no 'already exists' message"; }
}

# A new password but restic init failed - the password is removed, there is no request to save it.
test_init_failure_removes_new_password() {
    cat >/tmp/noinit-restic <<'EOF'
#!/bin/sh
if [ "$1" = init ]; then echo "Fatal: create repository failed" >&2; exit 1; fi
exec restic "$@"
EOF
    chmod +x /tmp/noinit-restic
    local rc
    rc=$(RESTIC_CMD=/tmp/noinit-restic rc_of run_script init-repo.sh)
    [[ "$rc" != 0 ]] || { cat "$OUT"; fail "failed restic init gave no error"; }
    [[ ! -e "$RESTIC_PASSWORD_FILE" && ! -e "$RESTIC_PASSWORD_FILE.tmp" ]] || fail "new password file left behind"
    ! grep -q "Save the password" "$OUT" || { cat "$OUT"; fail "request to save a password without a repository"; }
}

test_init_ignores_stale_tmp() {
    printf 'stale\n' >"$RESTIC_PASSWORD_FILE.tmp"
    run_script init-repo.sh || { cat "$OUT"; fail "init-repo.sh with a stale .tmp failed"; }
    [[ "$(cat "$RESTIC_PASSWORD_FILE")" != stale ]] || fail "content of the stale .tmp was used"
    [[ ! -e "$RESTIC_PASSWORD_FILE.tmp" ]] || fail ".tmp not removed"
    [[ "$(stat -c %a "$RESTIC_PASSWORD_FILE")" == 600 ]] || fail "password file mode is not 600"
}

test_repo_exists_distinguishes_network_error() {
    use_test_password
    local rc
    rc=$(rc_of run_lib 'load_env; repo_exists')
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "empty bucket: expected code 1, got $rc"; }
    [[ ! -s "$OUT" ]] || { cat "$OUT"; fail "repo_exists printed restic output"; }

    rc=$(rc_of run_lib 'load_env; RESTIC_REPOSITORY=s3:http://no-such-host.invalid:9000/aichat-test/aichat repo_exists')
    [[ "$rc" == 2 ]] || { cat "$OUT"; fail "nonexistent host: expected code 2, got $rc"; }
    [[ ! -s "$OUT" ]] || { cat "$OUT"; fail "repo_exists printed restic output"; }

    run_lib 'load_env; AWS_SECRET_ACCESS_KEY=wrong-secret repo_exists || echo "rc=$? $REPO_EXISTS_ERROR"' || true
    grep -q '^rc=2 restic did not answer within 15 s (network or S3 unavailable?): Stat: The request signature' "$OUT" \
        || { cat "$OUT"; fail "wrong key: expected code 2 and the S3 error text"; }
    assert_no_secrets "$OUT"
}

test_repo_exists_classification() {
    use_test_password
    # "Is there a repository..." without code 10 and without "repository does not exist" - not "no repository".
    local rc
    rc=$(rc_of run_lib "load_env; RESTIC_CMD=\"sh -c 'echo \\\"Is there a repository at the following location?\\\" >&2; exit 1' x\" repo_exists")
    [[ "$rc" == 2 ]] || { cat "$OUT"; fail "'Is there a repository' without code 10: expected 2, got $rc"; }
    rc=$(rc_of run_lib "load_env; RESTIC_CMD=\"sh -c 'echo \\\"Fatal: repository does not exist: x\\\" >&2; exit 1' x\" repo_exists")
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "'repository does not exist': expected 1, got $rc"; }
    rc=$(rc_of run_lib "load_env; RESTIC_CMD=\"sh -c 'exit 10' x\" repo_exists")
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "code 10: expected 1, got $rc"; }

    # The timeout is validated before substitution into eval.
    rc=$(rc_of run_lib "load_env; AICHAT_REPO_CHECK_TIMEOUT='1; touch /tmp/pwned;' repo_exists")
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "non-numeric timeout accepted"; }
    grep -q "AICHAT_REPO_CHECK_TIMEOUT must be an integer" "$OUT" || { cat "$OUT"; fail "no timeout text"; }
    [[ ! -e /tmp/pwned ]] || fail "timeout executed as code"
}

test_hc_ping_paths() {
    run_lib "hc_ping $HC_URL start; hc_ping $HC_URL ok; hc_ping $HC_URL fail boom" \
        || { cat "$OUT"; fail "hc_ping returned an error"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start / /fail" ]] || { cat "$HC_LOG"; fail "ping paths"; }
    [[ "$(jq -r 'select(.path == "/fail") | .body' "$HC_LOG")" == boom ]] || { cat "$HC_LOG"; fail "fail body"; }

    curl -fsS -X POST "$HC_URL/reset" >/dev/null
    run_lib "hc_ping $HC_URL/ fail \"\$(printf 'x%.0s' {1..3000})\"" || { cat "$OUT"; fail "long body"; }
    [[ "$(hc_paths)" == /fail ]] || { cat "$HC_LOG"; fail "path with a URL with a trailing slash"; }
    [[ "$(jq -r '.size' "$HC_LOG")" == 1024 ]] || { cat "$HC_LOG"; fail "body not truncated to 1024 bytes"; }

    # Multibyte body (2000 bytes of UTF-8) in a UTF-8 locale: no more than 1024 bytes sent.
    curl -fsS -X POST "$HC_URL/reset" >/dev/null
    run_lib "export LC_ALL=C.UTF-8; hc_ping $HC_URL fail \"\$(printf 'я%.0s' {1..1000})\"" \
        || { cat "$OUT"; fail "multibyte body"; }
    local size
    size=$(jq -r '.size' "$HC_LOG")
    [[ "$size" =~ ^[0-9]+$ ]] || { cat "$HC_LOG"; fail "no body size"; }
    ((size > 0 && size <= 1024)) || { cat "$HC_LOG"; fail "sent $size bytes"; }
}

test_hc_ping_rejects_bad_url() {
    run_lib "hc_ping 'http://hc:8080/x\"y' start; hc_ping 'http://hc:8080/x\\y' ok; echo rc=\$?" \
        || { cat "$OUT"; fail "hc_ping with a bad URL returned an error"; }
    [[ "$(grep -c '^aichat-backup: healthchecks: invalid characters in URL, ping skipped$' "$OUT")" == 2 ]] \
        || { cat "$OUT"; fail "no warnings"; }
    grep -q '^rc=0$' "$OUT" || fail "code is not 0"
    [[ ! -s "$HC_LOG" ]] || { cat "$HC_LOG"; fail "there was a request to hc"; }
}

test_hc_ping_unreachable_ok() {
    run_lib "hc_ping http://hc:9 start; echo rc=\$?" || { cat "$OUT"; fail "hc_ping to a closed port returned an error"; }
    grep -q '^rc=0$' "$OUT" || { cat "$OUT"; fail "code is not 0"; }
    grep -q '^aichat-backup: healthchecks:' "$OUT" || { cat "$OUT"; fail "no warning in stderr"; }
    ! grep -q 'hc:9' "$OUT" || { cat "$OUT"; fail "ping URL got into the journal"; }
}

test_hc_ping_empty_url_skipped() {
    run_lib 'hc_ping "" start; echo rc=$?' || { cat "$OUT"; fail "hc_ping with an empty URL returned an error"; }
    grep -q '^aichat-backup: healthchecks: URL not set, ping skipped$' "$OUT" || { cat "$OUT"; fail "no warning"; }
    grep -q '^rc=0$' "$OUT" || fail "code is not 0"
    [[ ! -s "$HC_LOG" ]] || fail "there was a request to hc"
}

test_lib_helpers() {
    # load_env: no env file -> die with a message
    [[ "$(rc_of run_lib 'AICHAT_BACKUP_ENV=/nonexistent load_env')" == 1 ]] || fail "load_env without a file did not fail"
    grep -q '^aichat-backup: Missing /nonexistent$' "$OUT" || { cat "$OUT"; fail "load_env error text"; }
    # load_env: no password -> die
    [[ "$(rc_of run_lib 'load_env')" == 1 ]] || fail "load_env without a password did not fail"
    use_test_password
    run_lib 'load_env; echo "$RESTIC_REPOSITORY"; bash -c "echo \$AWS_ACCESS_KEY_ID"' || { cat "$OUT"; fail "load_env"; }
    [[ "$(paste -sd ' ' "$OUT")" == "s3:http://minio:9000/aichat-test/aichat aichat-test-key" ]] \
        || { cat "$OUT"; fail "load_env did not export the variables"; }

    # write_status: directory 700, atomic write (name in the state directory or a full path), no temp files
    run_lib 'write_status status.json "{\"result\":\"ok\"}"; write_status "$AICHAT_STATE_DIR/r.json" "{}"' \
        || { cat "$OUT"; fail "write_status"; }
    [[ "$(stat -c %a "$AICHAT_STATE_DIR")" == 700 ]] || fail "state directory mode is not 700"
    [[ "$(jq -r .result "$AICHAT_STATE_DIR/status.json")" == ok ]] || fail "status.json"
    local files=("$AICHAT_STATE_DIR"/*)
    [[ "${files[*]}" == "$AICHAT_STATE_DIR/r.json $AICHAT_STATE_DIR/status.json" ]] || fail "extra files: ${files[*]}"

    # now_epoch
    run_lib 'AICHAT_NOW=1700000000 now_epoch' || fail "now_epoch"
    [[ "$(cat "$OUT")" == 1700000000 ]] || fail "now_epoch with AICHAT_NOW"
    run_lib 'now_epoch' || fail "now_epoch"
    [[ "$(cat "$OUT")" =~ ^[0-9]{10}$ ]] || fail "now_epoch without AICHAT_NOW"

    # db_psql / db_dump / db_restore / restic_cmd via overridden commands (stdin is passed through)
    run_lib 'db_psql -tAc "SELECT count(*) FROM users"' || { cat "$OUT"; fail "db_psql"; }
    [[ "$(cat "$OUT")" == 2 ]] || { cat "$OUT"; fail "db_psql: expected 2 users"; }
    run_lib 'db_dump | db_restore --list | grep -c "TABLE DATA public users"' || { cat "$OUT"; fail "db_dump | db_restore"; }
    [[ "$(cat "$OUT")" == 1 ]] || { cat "$OUT"; fail "no users data in the dump"; }
    run_lib 'load_env; restic_cmd init >/dev/null; printf "abc" | restic_cmd backup -q --stdin --stdin-filename x.txt; restic_cmd dump latest /x.txt' \
        || { cat "$OUT"; fail "restic_cmd"; }
    [[ "$(cat "$OUT")" == abc ]] || { cat "$OUT"; fail "restic_cmd: stdin not passed through"; }

    # default values (server)
    env -u AICHAT_BACKUP_ENV -u RESTIC_PASSWORD_FILE -u AICHAT_STATE_DIR -u RESTIC_CMD -u PG_DUMP_CMD -u PSQL_CMD -u PG_RESTORE_CMD \
        bash -c ". '$BACKUP_DIR/lib.sh'; printf '%s\n' \"\$AICHAT_BACKUP_ENV\" \"\$RESTIC_PASSWORD_FILE\" \"\$AICHAT_STATE_DIR\" \"\$RESTIC_CMD\" \"\$PG_DUMP_CMD\" \"\$PSQL_CMD\" \"\$PG_RESTORE_CMD\"" >"$OUT"
    diff -u - "$OUT" <<EOF || fail "default values"
/etc/aichat/restic.env
/etc/aichat/restic-password
/var/lib/aichat-backup
docker run --rm -i --hostname aichat --env-file /etc/aichat/restic.env -e RESTIC_PASSWORD_FILE=/run/restic-password -v /etc/aichat/restic-password:/run/restic-password:ro -v /var/cache/aichat-restic:/root/.cache/restic restic/restic:$(cat "$BACKUP_DIR/RESTIC_VERSION")
docker compose --project-directory /opt/aichat exec -T db pg_dump -U aichat -Fc aichat
docker compose --project-directory /opt/aichat exec -T db psql -U aichat -v ON_ERROR_STOP=1
docker compose --project-directory /opt/aichat exec -T db pg_restore -U aichat
EOF
}

test_error_text() {
    use_test_password
    run_lib 'load_env; redact "a $AWS_SECRET_ACCESS_KEY b $(cat "$RESTIC_PASSWORD_FILE") c $HC_BACKUP_URL/x d $AWS_ACCESS_KEY_ID"' \
        || { cat "$OUT"; fail "redact"; }
    [[ "$(cat "$OUT")" == 'a *** b *** c ***/x d ***' ]] || { cat "$OUT"; fail "redact did not strip the secrets"; }

    run_lib 'error_text last "$(printf "one\ntwo\n\n  \n")"; echo; error_text first "$(printf "\n one\ntwo\n")"' \
        || { cat "$OUT"; fail "error_text"; }
    [[ "$(paste -sd '|' "$OUT")" == 'two| one' ]] || { cat "$OUT"; fail "error_text: first/last line"; }

    # restic JSON error (--json) -> the message field; secrets are stripped in it too.
    run_lib 'load_env; error_text last "{\"message_type\":\"exit_error\",\"code\":1,\"message\":\"Fatal: $AWS_SECRET_ACCESS_KEY\"}"' \
        || { cat "$OUT"; fail "error_text json"; }
    [[ "$(cat "$OUT")" == 'Fatal: ***' ]] || { cat "$OUT"; fail "error_text: message from JSON"; }

    run_lib 'error_text last "$(printf "x%.0s" {1..3000})" | wc -c; error_text last "" | wc -c' \
        || { cat "$OUT"; fail "error_text long/empty"; }
    [[ "$(paste -sd ' ' "$OUT")" == '1024 0' ]] || { cat "$OUT"; fail "error_text: not truncated to 1024 bytes"; }
}

# --- tests: backup.sh ---------------------------------------------------------

test_backup_creates_snapshot() {
    init_test_repo
    run_script backup.sh || { cat "$OUT"; fail "backup.sh returned an error"; }
    [[ "$(snapshot_count)" == 1 ]] || { snapshots_json; fail "snapshots are not 1"; }
    snapshots_json | jq -e '.[0].hostname == "aichat" and .[0].paths == ["/aichat.dump"]' >/dev/null \
        || { snapshots_json; fail "snapshot host or path"; }
    # Without AICHAT_NOW the snapshot has the current time (--time is not passed).
    local age
    age=$(($(date +%s) - $(date -d "$(snapshots_json | jq -r '.[0].time')" +%s)))
    ((age >= 0 && age < 600)) || fail "snapshot time differs from the current by $age s"

    jq -e '.result == "ok" and (.snapshot | test("^[0-9a-f]{8}$")) and (.size_bytes > 0)
        and (.duration_s | type == "number") and .error == null
        and (.time | test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"))' "$STATUS" >/dev/null \
        || { cat "$STATUS"; fail "status.json"; }
    [[ "$(jq -r .snapshot "$STATUS")" == "$(snapshots_json | jq -r '.[0].short_id')" ]] \
        || { cat "$STATUS"; fail "snapshot in status.json does not match the snapshot"; }
    local size
    size=$(restic_t dump latest /aichat.dump | wc -c)
    [[ "$(jq -r .size_bytes "$STATUS")" == "$size" ]] || { cat "$STATUS"; fail "size_bytes != dump size ($size)"; }
    restic_t dump latest /aichat.dump | pg_restore --list | grep -q "TABLE DATA public users" \
        || fail "no dump with users data in the snapshot"
    [[ "$(hc_paths | paste -sd ' ')" == "/start /" ]] || { cat "$HC_LOG"; fail "pings are not /start, /"; }
}

test_backup_never_inits() {
    use_test_password
    local rc
    rc=$(rc_of run_script backup.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "no repository: code $rc"; }
    local msg="Repository not found - init is not performed, check the bucket (possible attack)"
    grep -qF "$msg" "$OUT" || { cat "$OUT"; fail "no repository-not-found message"; }
    [[ "$(rc_of restic_t cat config 2>/dev/null)" != 0 ]] || fail "repository created"
    [[ -z "$(mc ls --recursive "t/$BUCKET")" ]] || fail "backup.sh wrote something to the bucket"
    jq -e --arg msg "$msg" '.result == "error" and .snapshot == null and .size_bytes == null and .error == $msg' \
        "$STATUS" >/dev/null || { cat "$STATUS"; fail "status.json"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "pings are not /start, /fail"; }
    [[ "$(jq -r 'select(.path == "/fail") | .body' "$HC_LOG")" == "$msg" ]] || { cat "$HC_LOG"; fail "/fail body"; }
}

test_pg_dump_failure_no_snapshot() {
    init_test_repo
    export PG_DUMP_CMD="sh -c 'echo partial; exit 3'"
    local rc
    rc=$(rc_of run_script backup.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "pg_dump failure: code $rc"; }
    # restic managed to write a snapshot from the incomplete dump - backup.sh must remove it.
    grep -q "from the incomplete dump removed" "$OUT" || { cat "$OUT"; fail "the incomplete dump snapshot was not removed"; }
    [[ "$(snapshot_count)" == 0 ]] || { snapshots_json; fail "snapshot left behind"; }
    jq -e '.result == "error" and .snapshot == null and (.error | startswith("pg_dump: "))' "$STATUS" >/dev/null \
        || { cat "$STATUS"; fail "status.json"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "pings"; }
    [[ -n "$(jq -r 'select(.path == "/fail") | .body' "$HC_LOG")" ]] || { cat "$HC_LOG"; fail "empty /fail body"; }

    # The last meaningful stderr line of pg_dump is in the error text.
    export PG_DUMP_CMD="sh -c 'echo partial; echo \"pg_dump: error: connection refused\" >&2; echo >&2; exit 1'"
    rc=$(rc_of run_script backup.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "pg_dump failure (2): code $rc"; }
    [[ "$(jq -r .error "$STATUS")" == "pg_dump: pg_dump: error: connection refused" ]] \
        || { cat "$STATUS"; fail "pg_dump error text"; }
    [[ "$(snapshot_count)" == 0 ]] || { snapshots_json; fail "snapshot left behind (2)"; }
}

test_restic_backup_failure() {
    init_test_repo
    # Real restic, but `backup` with a wrong password: a JSON error (exit_error) on stderr.
    printf 'wrong-password\n' >/tmp/wrong-password
    cat >/tmp/badpw-restic <<'EOF'
#!/bin/sh
if [ "$1" = backup ]; then RESTIC_PASSWORD_FILE=/tmp/wrong-password exec restic "$@"; fi
exec restic "$@"
EOF
    chmod +x /tmp/badpw-restic
    local rc
    rc=$(RESTIC_CMD=/tmp/badpw-restic rc_of run_script backup.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "restic backup failure: code $rc"; }
    [[ "$(jq -r .error "$STATUS")" == "restic backup: Fatal: wrong password or no key found" ]] \
        || { cat "$STATUS"; fail "restic backup error text"; }
    [[ "$(snapshot_count)" == 0 ]] || { snapshots_json; fail "there is a snapshot"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "pings"; }

    # The dump is larger than the pipe buffer: restic exited without reading - pg_dump gets SIGPIPE (141);
    # that is a consequence, not the cause, and it does not appear in the error text.
    export PG_DUMP_CMD="sh -c 'yes | head -c 10000000'"
    rc=$(RESTIC_CMD=/tmp/badpw-restic rc_of run_script backup.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "restic backup failure (SIGPIPE): code $rc"; }
    [[ "$(jq -r .error "$STATUS")" == "restic backup: Fatal: wrong password or no key found" ]] \
        || { cat "$STATUS"; fail "error text on pg_dump SIGPIPE"; }
}

test_backup_env_error() {
    # No repository password: load_env fails before the pings (the ping URL is not known yet),
    # but status.json gets the error - a stale "ok" does not remain.
    install -d -m 700 "$AICHAT_STATE_DIR"
    echo '{"result":"ok"}' >"$STATUS"
    local rc
    rc=$(rc_of run_script backup.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "code $rc"; }
    grep -q "Repository password file" "$OUT" || { cat "$OUT"; fail "no message"; }
    jq -e '.result == "error" and .snapshot == null and (.error | startswith("Repository password file"))' \
        "$STATUS" >/dev/null || { cat "$STATUS"; fail "status.json"; }
    [[ ! -s "$HC_LOG" ]] || { cat "$HC_LOG"; fail "there were pings"; }
}

test_cleanup_forget_failure_redacted() {
    init_test_repo
    make_leaky_restic
    export PG_DUMP_CMD="sh -c 'echo partial; exit 3'"
    local rc
    rc=$(RESTIC_CMD=/tmp/leaky-restic rc_of run_script backup.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "code $rc"; }
    jq -e '.error | test("^pg_dump: exit code 3; could not remove snapshot [0-9a-f]{8} from the incomplete dump: Fatal: forget \\*\\*\\* \\*\\*\\*$")' \
        "$STATUS" >/dev/null || { cat "$STATUS"; fail "snapshot removal error text"; }
    # stderr of the failed forget is in the journal, with secrets stripped.
    grep -qx 'Fatal: forget \*\*\* \*\*\*' "$OUT" || { cat "$OUT"; fail "forget stderr not in the journal"; }
    local all=/tmp/all-output
    cat "$OUT" "$STATUS" >"$all"
    jq -r .body "$HC_LOG" >>"$all"
    [[ "$(hc_paths | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "pings"; }
    assert_no_secrets "$all"
    assert_no_key_id "$all"
}

test_sigterm_mid_dump_no_snapshot() {
    init_test_repo
    # The dump "hangs" for 3 s; SIGTERM reaches backup.sh during the dump. The dump then finishes,
    # restic saves a snapshot - backup.sh must remove it and report the interruption.
    export PG_DUMP_CMD="sh -c 'touch /tmp/dump-started; echo partial; sleep 3; echo rest'"
    rm -f /tmp/dump-started
    bash "$BACKUP_DIR/backup.sh" >"$OUT" 2>&1 &
    local pid=$! i rc=0
    for i in $(seq 1 150); do
        [[ ! -e /tmp/dump-started ]] || break
        sleep 0.2
    done
    [[ -e /tmp/dump-started ]] || { kill "$pid"; cat "$OUT"; fail "the dump did not start"; }
    kill -TERM "$pid"
    wait "$pid" || rc=$?
    ((rc != 0)) || { cat "$OUT"; fail "interrupted backup returned 0"; }
    [[ "$(snapshot_count)" == 0 ]] || { cat "$OUT"; snapshots_json; fail "snapshot of the interrupted dump left behind"; }
    jq -e '.result == "error" and .snapshot == null and (.error | startswith("Backup interrupted by a signal (timeout/stop)"))' \
        "$STATUS" >/dev/null || { cat "$OUT" "$STATUS"; fail "status.json"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "pings"; }
    [[ -n "$(jq -r 'select(.path == "/fail") | .body' "$HC_LOG")" ]] || fail "empty /fail body"
}

test_unexpected_error_reported() {
    init_test_repo
    # die inside lib.sh (not via fail) - the status and /fail are still sent from trap EXIT.
    local rc
    rc=$(AICHAT_REPO_CHECK_TIMEOUT=abc rc_of run_script backup.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "code $rc"; }
    jq -e '.result == "error" and (.error | startswith("Unexpected backup.sh error (code 1)"))' "$STATUS" >/dev/null \
        || { cat "$STATUS"; fail "status.json"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "pings"; }
    [[ -z "$(find /tmp -maxdepth 1 -name 'tmp.*')" ]] || fail "temporary directory not removed"
}

test_rotation_applied() {
    init_test_repo
    local base i
    base=$(date -u -d '2026-01-01 03:30:00' +%s)
    for i in $(seq 0 9); do
        export AICHAT_NOW=$((base + i * 86400))
        run_script backup.sh || { cat "$OUT"; fail "backup of day $i failed"; }
    done
    local count
    count=$(snapshot_count)
    ((count < 10)) || { snapshots_json; fail "rotation not applied: $count snapshots"; }
    local days
    days=$(snapshots_json | jq -r '.[].time[:10]')
    for i in $(seq 3 9); do
        grep -qx "$(date -u -d "@$((base + i * 86400))" +%F)" <<<"$days" \
            || { echo "$days"; fail "no snapshot of day $i"; }
    done
}

test_check_on_sunday_only() {
    init_test_repo
    local sunday
    sunday=$(date -u -d '2026-01-04 03:30:00' +%s)
    [[ "$(date -u -d "@$sunday" +%u)" == 7 ]] || fail "2026-01-04 is not a Sunday"
    export AICHAT_NOW=$sunday
    run_script backup.sh || { cat "$OUT"; fail "Sunday backup failed"; }
    grep -q "no errors were found" "$OUT" || { cat "$OUT"; fail "no restic check on Sunday"; }
    export AICHAT_NOW=$((sunday + 86400))
    run_script backup.sh || { cat "$OUT"; fail "Monday backup failed"; }
    ! grep -q "no errors were found" "$OUT" || { cat "$OUT"; fail "restic check not on Sunday"; }
}

test_no_secrets_in_output() {
    init_test_repo
    local all=/tmp/all-output
    collect() {
        cat "$OUT" "$STATUS" >>"$all"
        jq -r .body "$HC_LOG" >>"$all"
    }
    run_script backup.sh || { cat "$OUT"; fail "backup.sh returned an error"; }
    collect

    # Wrong AWS key.
    local env=/tmp/bad-key.env rc
    sed 's/^AWS_SECRET_ACCESS_KEY=.*/AWS_SECRET_ACCESS_KEY=wrong-secret/' "$AICHAT_BACKUP_ENV" >"$env"
    rc=$(AICHAT_BACKUP_ENV=$env rc_of run_script backup.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "wrong key: code $rc"; }
    grep -q "Cannot access the repository: " "$OUT" || { cat "$OUT"; fail "no access error text"; }
    collect

    make_leaky_restic
    curl -fsS -X POST "$HC_URL/reset" >/dev/null
    rc=$(RESTIC_CMD=/tmp/leaky-restic rc_of run_script backup.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "forget failure: code $rc"; }
    grep -qF 'leak: *** *** *** ***' "$OUT" || { cat "$OUT"; fail "restic stderr did not reach the journal with secrets stripped"; }
    [[ "$(jq -r .error "$STATUS")" == 'restic forget: Fatal: forget *** ***' ]] || { cat "$STATUS"; fail "forget error text"; }
    collect

    [[ "$(jq -r .path "$HC_LOG" | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "pings"; }
    assert_no_secrets "$all"
    assert_no_key_id "$all"
}

test_dump_never_touches_disk() {
    init_test_repo
    touch /tmp/marker
    sleep 1
    run_script backup.sh || { cat "$OUT"; fail "backup.sh returned an error"; }
    local found
    found=$(find / -xdev -name '*.dump' -newer /tmp/marker 2>/dev/null)
    [[ -z "$found" ]] || fail "dump files on disk: $found"
    # The temporary directory for step stderr is removed.
    found=$(find /tmp -maxdepth 1 -name 'tmp.*' -newer /tmp/marker)
    [[ -z "$found" ]] || fail "temporary files left behind: $found"
}

test_stale_lock_removed() {
    init_test_repo
    # The background subshell itself becomes restic (exec): $! is the PID of the restic process.
    (
        set -a
        # shellcheck source=/dev/null
        . "$AICHAT_BACKUP_ENV"
        set +a
        exec restic backup --stdin --stdin-filename stale.bin </dev/zero >/dev/null 2>&1
    ) &
    local pid=$! i locks=""
    for i in $(seq 1 60); do
        locks=$(restic_t list locks --no-lock 2>/dev/null) || true
        [[ -z "$locks" ]] || break
        sleep 0.5
    done
    [[ -n "$locks" ]] || fail "lock did not appear"
    kill -9 "$pid"
    wait "$pid" 2>/dev/null || true
    ! kill -0 "$pid" 2>/dev/null || fail "restic process is alive"
    restic_t cat lock "$locks" | jq -e --arg h "$(hostname)" --argjson pid "$pid" \
        '.hostname == $h and .pid == $pid' >/dev/null || { restic_t cat lock "$locks"; fail "lock of the wrong process"; }

    run_script backup.sh || { cat "$OUT"; fail "backup.sh with a stale lock failed"; }
    [[ -z "$(restic_t list locks --no-lock)" ]] || fail "lock not removed"
    [[ "$(snapshot_count)" == 1 ]] || { snapshots_json; fail "snapshots are not 1"; }
}

# Commands that may run into the lock of a still-running prune wait for it (--retry-lock 30m).
test_retry_lock_used() {
    init_test_repo
    cat >/tmp/argrec-restic <<'EOF'
#!/bin/sh
echo "$*" >>/tmp/restic-args
exec restic "$@"
EOF
    chmod +x /tmp/argrec-restic
    rm -f /tmp/restic-args
    RESTIC_CMD=/tmp/argrec-restic run_script backup.sh || { cat "$OUT"; fail "backup.sh returned an error"; }
    RESTIC_CMD=/tmp/argrec-restic run_script restore-test.sh || { cat "$OUT"; fail "restore-test.sh returned an error"; }
    local cmd
    for cmd in backup forget snapshots dump; do
        grep -q "^$cmd --retry-lock 30m " /tmp/restic-args \
            || { cat /tmp/restic-args; fail "restic $cmd without --retry-lock 30m"; }
    done
}

# --- tests: restore-test.sh ---------------------------------------------------

test_restore_ok() {
    init_test_repo
    run_script backup.sh || { cat "$OUT"; fail "backup.sh returned an error"; }
    curl -fsS -X POST "$HC_URL/reset" >/dev/null
    run_script restore-test.sh || { cat "$OUT"; fail "restore-test.sh returned an error"; }
    jq -e --arg id "$(snapshots_json | jq -r '.[0].short_id')" \
        '.result == "ok" and .snapshot == $id and .users == 2 and .error == null
        and (.snapshot_age_h | type == "number" and . >= 0 and . < 1)
        and (.time | test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"))' "$RESTORE_STATUS" >/dev/null \
        || { cat "$RESTORE_STATUS"; fail "restore-test.json"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start /" ]] || { cat "$HC_LOG"; fail "pings are not /start, /"; }
    ! db_exists restore_test || fail "restore_test not dropped"
}

test_restore_fails_on_stale_snapshot() {
    init_test_repo
    export AICHAT_NOW=$RT_BASE
    run_script backup.sh || { cat "$OUT"; fail "backup.sh returned an error"; }
    curl -fsS -X POST "$HC_URL/reset" >/dev/null

    # 47 hours - still fine.
    AICHAT_NOW=$((RT_BASE + 47 * 3600)) run_script restore-test.sh || { cat "$OUT"; fail "47 h snapshot: error"; }
    jq -e '.result == "ok" and .snapshot_age_h == 47' "$RESTORE_STATUS" >/dev/null \
        || { cat "$RESTORE_STATUS"; fail "snapshot_age_h at 47 h"; }

    # 49 hours - an error, the DB was not created.
    curl -fsS -X POST "$HC_URL/reset" >/dev/null
    local rc msg="The latest snapshot is older than 48 hours: backups are not being created"
    rc=$(AICHAT_NOW=$((RT_BASE + 49 * 3600)) rc_of run_script restore-test.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "49 h snapshot: code $rc"; }
    grep -qF "$msg" "$OUT" || { cat "$OUT"; fail "no 48 hours message"; }
    jq -e --arg msg "$msg" '.result == "error" and .error == $msg and .users == null and .snapshot_age_h == 49
        and (.snapshot | test("^[0-9a-f]{8}$"))' "$RESTORE_STATUS" >/dev/null \
        || { cat "$RESTORE_STATUS"; fail "restore-test.json"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "pings are not /start, /fail"; }
    [[ "$(jq -r 'select(.path == "/fail") | .body' "$HC_LOG")" == "$msg" ]] || { cat "$HC_LOG"; fail "/fail body"; }
    ! db_exists restore_test || fail "restore_test left behind"
}

test_restore_db_dropped_on_failure() {
    init_test_repo
    run_script backup.sh || { cat "$OUT"; fail "backup.sh returned an error"; }
    curl -fsS -X POST "$HC_URL/reset" >/dev/null
    local rc
    rc=$(PG_RESTORE_CMD="sh -c 'cat >/dev/null; exit 1'" rc_of run_script restore-test.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "code $rc"; }
    ! db_exists restore_test || fail "restore_test left behind after the failure"
    jq -e '.result == "error" and .users == null and (.error | startswith("pg_restore: "))' "$RESTORE_STATUS" >/dev/null \
        || { cat "$RESTORE_STATUS"; fail "restore-test.json"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "pings"; }

    # A content check failure (the users table is empty) is also an error, the DB is dropped.
    curl -fsS -X POST "$HC_URL/reset" >/dev/null
    rc=$(PG_RESTORE_CMD="sh -c 'cat >/dev/null; psql -h db -U aichat -d restore_test -c \"CREATE TABLE users (id int); CREATE TABLE alembic_version (v text)\"'" \
        rc_of run_script restore-test.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "empty users: code $rc"; }
    jq -e '.result == "error" and (.error | test("Table users is empty"))' "$RESTORE_STATUS" >/dev/null \
        || { cat "$RESTORE_STATUS"; fail "empty users error"; }
    ! db_exists restore_test || fail "restore_test left behind after the empty users"
}

test_restore_fails_without_snapshots() {
    init_test_repo
    local rc msg="No aichat-db snapshots in the repository: backups are not being created"
    rc=$(rc_of run_script restore-test.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "code $rc"; }
    grep -qF "$msg" "$OUT" || { cat "$OUT"; fail "no no-snapshots message"; }
    jq -e --arg msg "$msg" '.result == "error" and .error == $msg and .snapshot == null and .snapshot_age_h == null' \
        "$RESTORE_STATUS" >/dev/null || { cat "$RESTORE_STATUS"; fail "restore-test.json"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "pings"; }
    ! db_exists restore_test || fail "restore_test created"

    # There is no repository at all - a restic error in the status.
    reset_env
    use_test_password
    rc=$(rc_of run_script restore-test.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "no repository: code $rc"; }
    jq -e '.result == "error" and (.error | startswith("restic snapshots: "))' "$RESTORE_STATUS" >/dev/null \
        || { cat "$RESTORE_STATUS"; fail "restore-test.json without a repository"; }
}

test_restore_does_not_touch_live_db() {
    init_test_repo
    run_script backup.sh || { cat "$OUT"; fail "backup.sh returned an error"; }
    # Change the working DB after the backup: the restored copy (2 users) must not touch it.
    local extra
    extra=$(psql -h db -U aichat -d aichat -tAq -c 'INSERT INTO users DEFAULT VALUES RETURNING id') || fail "INSERT"
    # shellcheck disable=SC2064  # the id is known now; the test runs in a subshell, the trap does not leak out of it
    trap "psql -h db -U aichat -d aichat -qc 'DELETE FROM users WHERE id = $extra'" EXIT
    run_script restore-test.sh || { cat "$OUT"; fail "restore-test.sh returned an error"; }
    [[ "$(psql -h db -U aichat -d aichat -tAc 'SELECT count(*) FROM users')" == 3 ]] || fail "working DB changed"
    [[ "$(psql -h db -U aichat -d aichat -tAc 'SELECT count(*) FROM alembic_version')" == 1 ]] || fail "alembic_version changed"
    jq -e '.users == 2' "$RESTORE_STATUS" >/dev/null || { cat "$RESTORE_STATUS"; fail "the copy does not have 2 users"; }
}

test_restore_env_error() {
    install -d -m 700 "$AICHAT_STATE_DIR"
    echo '{"result":"ok"}' >"$RESTORE_STATUS"
    local rc
    rc=$(rc_of run_script restore-test.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "code $rc"; }
    grep -q "Repository password file" "$OUT" || { cat "$OUT"; fail "no message"; }
    jq -e '.result == "error" and .snapshot == null and (.error | startswith("Repository password file"))' \
        "$RESTORE_STATUS" >/dev/null || { cat "$RESTORE_STATUS"; fail "restore-test.json"; }
    [[ ! -s "$HC_LOG" ]] || { cat "$HC_LOG"; fail "there were pings"; }
}

test_restore_sigterm_drops_db() {
    init_test_repo
    run_script backup.sh || { cat "$OUT"; fail "backup.sh returned an error"; }
    curl -fsS -X POST "$HC_URL/reset" >/dev/null
    rm -f /tmp/restore-started
    export PG_RESTORE_CMD="sh -c 'touch /tmp/restore-started; sleep 3; cat >/dev/null'"
    bash "$BACKUP_DIR/restore-test.sh" >"$OUT" 2>&1 &
    local pid=$! i rc=0
    for i in $(seq 1 150); do
        [[ ! -e /tmp/restore-started ]] || break
        sleep 0.2
    done
    [[ -e /tmp/restore-started ]] || { kill "$pid"; cat "$OUT"; fail "the restore did not start"; }
    kill -TERM "$pid"
    wait "$pid" || rc=$?
    ((rc != 0)) || { cat "$OUT"; fail "interrupted check returned 0"; }
    jq -e '.result == "error" and (.error | startswith("Restore check interrupted by a signal"))' "$RESTORE_STATUS" >/dev/null \
        || { cat "$OUT" "$RESTORE_STATUS"; fail "restore-test.json"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "pings"; }
    ! db_exists restore_test || fail "restore_test left behind after the signal"
}

test_restore_no_secrets_on_failure() {
    init_test_repo
    run_script backup.sh || { cat "$OUT"; fail "backup.sh returned an error"; }
    curl -fsS -X POST "$HC_URL/reset" >/dev/null
    # restic that prints secrets on every call and fails on dump with secrets in the error text.
    cat >/tmp/leaky-dump-restic <<'EOS'
#!/bin/sh
echo "leak: $AWS_SECRET_ACCESS_KEY $AWS_ACCESS_KEY_ID $HC_BACKUP_URL $HC_RESTORE_URL $(cat "$RESTIC_PASSWORD_FILE")" >&2
if [ "$1" = dump ]; then
    echo "Fatal: dump $AWS_SECRET_ACCESS_KEY $HC_RESTORE_URL $(cat "$RESTIC_PASSWORD_FILE")" >&2
    exit 1
fi
exec restic "$@"
EOS
    chmod +x /tmp/leaky-dump-restic
    local rc
    rc=$(RESTIC_CMD=/tmp/leaky-dump-restic rc_of run_script restore-test.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "code $rc"; }
    grep -qF 'leak: *** *** *** *** ***' "$OUT" || { cat "$OUT"; fail "restic stderr did not reach the journal with secrets stripped"; }
    jq -e '.error | startswith("restic dump: Fatal: dump *** *** ***")' "$RESTORE_STATUS" >/dev/null \
        || { cat "$RESTORE_STATUS"; fail "restic dump error text"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "pings"; }
    local all=/tmp/all-output
    cat "$OUT" "$RESTORE_STATUS" >"$all"
    jq -r .body "$HC_LOG" >>"$all"
    assert_no_secrets "$all"
    assert_no_key_id "$all"
    ! db_exists restore_test || fail "restore_test left behind"
}

test_restore_drops_db_with_open_connection() {
    init_test_repo
    run_script backup.sh || { cat "$OUT"; fail "backup.sh returned an error"; }
    curl -fsS -X POST "$HC_URL/reset" >/dev/null
    # "pg_restore" creates the tables and leaves an open connection to restore_test (as after a client SIGTERM).
    export PG_RESTORE_CMD="sh -c 'cat >/dev/null; psql -h db -U aichat -d restore_test -qc \"CREATE TABLE users (id int); INSERT INTO users VALUES (1); CREATE TABLE alembic_version (v text); INSERT INTO alembic_version VALUES (1)\"; psql -h db -U aichat -d restore_test -c \"select pg_sleep(60)\" >/dev/null 2>&1 &'"
    run_script restore-test.sh || { cat "$OUT"; fail "restore-test.sh returned an error"; }
    ! db_exists restore_test || fail "restore_test left behind because of an open connection"
    jq -e '.result == "ok" and .users == 1' "$RESTORE_STATUS" >/dev/null || { cat "$RESTORE_STATUS"; fail "restore-test.json"; }
}

test_restore_drop_failure_is_error() {
    init_test_repo
    run_script backup.sh || { cat "$OUT"; fail "backup.sh returned an error"; }
    curl -fsS -X POST "$HC_URL/reset" >/dev/null
    # psql wrapper: after the restore it refuses to drop restore_test.
    cat >/tmp/nodrop-psql <<'EOS'
#!/bin/sh
case "$*" in *"DROP DATABASE"*) [ -e /tmp/restore-done ] && { echo "ERROR: cannot drop restore_test" >&2; exit 1; } ;; esac
exec psql -h db -U aichat -v ON_ERROR_STOP=1 "$@"
EOS
    chmod +x /tmp/nodrop-psql
    rm -f /tmp/restore-done
    export PSQL_CMD=/tmp/nodrop-psql
    export PG_RESTORE_CMD="sh -c 'cat >/dev/null; psql -h db -U aichat -d restore_test -qc \"CREATE TABLE users (id int); INSERT INTO users VALUES (1); CREATE TABLE alembic_version (v text); INSERT INTO alembic_version VALUES (1)\"; touch /tmp/restore-done'"
    local rc
    rc=$(rc_of run_script restore-test.sh)
    # Clean up the leftover so as not to disturb the next tests (before the checks: when a test fails the DB does not remain).
    local left
    left=$(db_exists restore_test && echo yes || echo no)
    psql -h db -U aichat -d aichat -qc 'DROP DATABASE IF EXISTS restore_test WITH (FORCE)'
    [[ "$left" == yes ]] || fail "restore_test should have remained (simulated drop failure)"
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "code $rc"; }
    jq -e '.result == "error" and .users == 1 and (.error | test("could not drop the temporary DB restore_test"))' "$RESTORE_STATUS" >/dev/null \
        || { cat "$RESTORE_STATUS"; fail "restore-test.json"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start / /fail" ]] || { cat "$HC_LOG"; fail "pings are not /start, /, /fail"; }
    jq -r 'select(.path == "/fail") | .body' "$HC_LOG" | grep -q "restore_test" || fail "/fail body"
}

test_restore_fails_on_future_snapshot() {
    init_test_repo
    export AICHAT_NOW=$RT_BASE
    run_script backup.sh || { cat "$OUT"; fail "backup.sh returned an error"; }
    local rc
    rc=$(AICHAT_NOW=$((RT_BASE - 2 * 3600)) rc_of run_script restore-test.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "code $rc"; }
    grep -q "in the future" "$OUT" || { cat "$OUT"; fail "no message"; }
    ! db_exists restore_test || fail "restore_test created"
    AICHAT_NOW=$((RT_BASE - 1800)) run_script restore-test.sh || { cat "$OUT"; fail "a 30 min shift must pass"; }
}

test_iso_to_epoch() {
    run_lib 'iso_to_epoch 2026-01-01T03:30:00Z; iso_to_epoch 2026-01-01T03:30:00.123456789Z;
        iso_to_epoch 2026-01-01T05:30:00.5+02:00; iso_to_epoch 2026-01-01T03:30:00-00:00; iso_to_epoch 2025-12-31T22:30:00.25-05:00' \
        || { cat "$OUT"; fail "iso_to_epoch"; }
    [[ "$(sort -u "$OUT" | paste -sd ' ')" == "$RT_BASE" ]] || { cat "$OUT"; fail "iso_to_epoch: different epochs, expected $RT_BASE"; }
    [[ "$(rc_of run_lib 'iso_to_epoch not-a-time')" != 0 ]] || fail "iso_to_epoch accepted garbage"
}

test_shellcheck() {
    shellcheck -x "$BACKUP_DIR"/*.sh "$TEST_DIR/run.sh" || fail "shellcheck found issues"
}

# --- run ------------------------------------------------------------------

main() {
    local tests=("$@") t rc failed=0
    if ((${#tests[@]} == 0)); then
        mapfile -t tests < <(declare -F | awk '$3 ~ /^test_/ {print $3}')
    fi
    for t in "${tests[@]}"; do
        if ! declare -F "$t" >/dev/null; then
            echo "FAIL $t (no such test)"
            failed=1
            continue
        fi
        reset_env
        set +e
        (
            set -euo pipefail
            "$t"
        ) >"/tmp/$t.log" 2>&1
        rc=$?
        set -e
        if ((rc == 0)); then
            echo "PASS $t"
        else
            echo "FAIL $t"
            sed 's/^/    /' "/tmp/$t.log"
            failed=1
        fi
    done
    return "$failed"
}

main "$@"
