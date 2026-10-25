#!/usr/bin/env bash
# Тесты скриптов бэкапа. Запускаются внутри runner (ops/backup/test/compose.yml):
#   bash /work/ops/backup/test/run.sh            — все функции test_*
#   bash /work/ops/backup/test/run.sh test_a ... — только перечисленные
# Перед каждым тестом: пустой бакет (все версии), нет пароль-файла, пустые state и журнал hc.
# Печатает PASS/FAIL по каждому тесту; код 1 при любом FAIL.
# SC2016: run_lib принимает bash-код в одинарных кавычках намеренно (раскрывается в дочернем bash).
# shellcheck disable=SC2016
set -euo pipefail

BACKUP_DIR=/work/ops/backup
TEST_DIR=$BACKUP_DIR/test
HC_URL=http://hc:8080
HC_LOG=/tmp/hc/requests.jsonl
BUCKET=aichat-test
OUT=/tmp/test-out   # вывод тестируемой команды (см. run_script / run_lib)

# mc ходит в MinIO с ключами из тестового env-файла.
# shellcheck source=/dev/null
MC_HOST_t=$(set -a; . "$AICHAT_BACKUP_ENV"; printf 'http://%s:%s@minio:9000' "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY")
export MC_HOST_t

# --- помощники -------------------------------------------------------------

fail() { echo "  assert: $*" >&2; return 1; }

reset_env() {
    mc rm --recursive --versions --force "t/$BUCKET" >/dev/null 2>&1 || true
    [[ -z "$(mc ls --versions --recursive "t/$BUCKET")" ]] || fail "бакет не очищен"
    rm -f "$RESTIC_PASSWORD_FILE" "$RESTIC_PASSWORD_FILE.tmp"
    rm -rf "$AICHAT_STATE_DIR" "${HOME:-/root}/.cache/restic" "$OUT"
    curl -fsS -X POST "$HC_URL/reset" >/dev/null
}

# Пароль репозитория с известным значением (для тестов, которым не важно, кто его создал).
use_test_password() { install -m 600 "$TEST_DIR/test-password" "$RESTIC_PASSWORD_FILE"; }

# run_script <script> [args] — запускает ops/backup/<script>; stdout+stderr → $OUT; возвращает код.
run_script() {
    local script=$1 rc=0
    shift
    timeout 300 bash "$BACKUP_DIR/$script" "$@" >"$OUT" 2>&1 || rc=$?
    return "$rc"
}

# run_lib '<код>' — выполняет код в отдельном bash с подключённым lib.sh; stdout+stderr → $OUT.
run_lib() {
    local rc=0
    timeout 300 bash -c ". '$BACKUP_DIR/lib.sh'; $1" >"$OUT" 2>&1 || rc=$?
    return "$rc"
}

# rc_of <команда...> — печатает код возврата команды (не прерывая тест).
rc_of() { local rc=0; "$@" || rc=$?; echo "$rc"; }

hc_paths() { jq -r .path "$HC_LOG"; }

# assert_no_secrets <file> — в файле нет ни настоящего, ни подставленного в тестах неверного ключа AWS,
# ни пароля репозитория (если файл пароля есть), ни URL пингов бэкапа и restore-test.
assert_no_secrets() {
    local file=$1 secret hc_url restore_ping pw=""
    # shellcheck source=/dev/null
    secret=$(set -a; . "$AICHAT_BACKUP_ENV"; printf '%s' "$AWS_SECRET_ACCESS_KEY")
    # shellcheck source=/dev/null
    hc_url=$(set -a; . "$AICHAT_BACKUP_ENV"; printf '%s' "$HC_BACKUP_URL")
    # shellcheck source=/dev/null
    restore_ping=$(set -a; . "$AICHAT_BACKUP_ENV"; printf '%s' "$HC_RESTORE_URL")
    [[ -s "$RESTIC_PASSWORD_FILE" ]] && pw=$(cat "$RESTIC_PASSWORD_FILE")
    ! grep -qF "$secret" "$file" || fail "в выводе ключ AWS"
    ! grep -qF wrong-secret "$file" || fail "в выводе неверный ключ AWS"
    ! grep -qF "$hc_url" "$file" || fail "в выводе URL пинга"
    ! grep -qF "$restore_ping" "$file" || fail "в выводе URL пинга restore-test"
    [[ -z "$pw" ]] || ! grep -qF "$pw" "$file" || fail "в выводе пароль репозитория"
}

# restic_t <аргументы> — restic с тестовым env-файлом (без lib.sh).
restic_t() (
    set -a
    # shellcheck source=/dev/null
    . "$AICHAT_BACKUP_ENV"
    set +a
    exec restic "$@"
)

# Репозиторий с тестовым паролем (как после init-repo.sh).
init_test_repo() {
    use_test_password
    restic_t init >/dev/null 2>&1 || fail "restic init"
}

# /tmp/leaky-restic — restic, печатающий секреты в stderr при каждом вызове и падающий на forget.
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

# assert_no_key_id <file> — в файле нет ID ключа AWS.
assert_no_key_id() {
    local key_id
    # shellcheck source=/dev/null
    key_id=$(set -a; . "$AICHAT_BACKUP_ENV"; printf '%s' "$AWS_ACCESS_KEY_ID")
    ! grep -qF "$key_id" "$1" || fail "в выводе ID ключа AWS"
}

snapshots_json() { restic_t snapshots --tag aichat-db --json; }
snapshot_count() { snapshots_json | jq length; }
STATUS=$AICHAT_STATE_DIR/status.json
RESTORE_STATUS=$AICHAT_STATE_DIR/restore-test.json

# db_exists <имя> — 0, если БД есть на сервере.
db_exists() { [[ -n "$(psql -h db -U aichat -d aichat -tAc "SELECT 1 FROM pg_database WHERE datname='$1'")" ]]; }
# Время бэкапа в тестах restore-test (эпоха, UTC).
RT_BASE=$(date -u -d '2026-01-01 03:30:00' +%s)

# --- тесты: init-repo.sh и lib.sh ------------------------------------------

test_init_creates_repo() {
    run_script init-repo.sh || { cat "$OUT"; fail "init-repo.sh вернул ошибку"; }
    [[ -s "$RESTIC_PASSWORD_FILE" ]] || fail "пароль-файл не создан"
    [[ "$(stat -c %a "$RESTIC_PASSWORD_FILE")" == 600 ]] || fail "права пароль-файла не 600"
    grep -q "Сохраните пароль в менеджере паролей: cat $RESTIC_PASSWORD_FILE" "$OUT" \
        || fail "нет инструкции сохранить пароль"
    ! grep -qF "$(cat "$RESTIC_PASSWORD_FILE")" "$OUT" || fail "пароль напечатан"
    [[ "$(rc_of run_lib 'load_env; repo_exists')" == 0 ]] || fail "repo_exists после init != 0"
}

test_init_twice_is_noop() {
    run_script init-repo.sh || { cat "$OUT"; fail "первый init упал"; }
    local pw
    pw=$(cat "$RESTIC_PASSWORD_FILE")
    run_script init-repo.sh || { cat "$OUT"; fail "второй init вернул ошибку"; }
    grep -q "Репозиторий уже существует" "$OUT" || { cat "$OUT"; fail "нет сообщения «уже существует»"; }
    ! grep -q "Сохраните пароль" "$OUT" || fail "пароль сгенерирован повторно"
    [[ "$(cat "$RESTIC_PASSWORD_FILE")" == "$pw" ]] || fail "пароль-файл изменился"
}

test_init_fails_on_access_error() {
    use_test_password
    local env=/tmp/bad-key.env
    sed 's/^AWS_SECRET_ACCESS_KEY=.*/AWS_SECRET_ACCESS_KEY=wrong-secret/' "$AICHAT_BACKUP_ENV" >"$env"
    [[ "$(rc_of env AICHAT_BACKUP_ENV="$env" bash -c "bash '$BACKUP_DIR/init-repo.sh' >'$OUT' 2>&1")" == 1 ]] \
        || { cat "$OUT"; fail "init с неверным ключом не упал"; }
    grep -q "Нет доступа к репозиторию" "$OUT" || { cat "$OUT"; fail "нет текста об ошибке доступа"; }
    [[ -z "$(mc ls --recursive "t/$BUCKET")" ]] || fail "init что-то записал в бакет"
    assert_no_secrets "$OUT"
}

test_init_bad_env_leaves_no_password() {
    local rc
    rc=$(rc_of env AICHAT_BACKUP_ENV=/nonexistent bash -c "bash '$BACKUP_DIR/init-repo.sh' >'$OUT' 2>&1")
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "init без env-файла: код $rc"; }
    grep -q "Нет /nonexistent" "$OUT" || { cat "$OUT"; fail "нет текста об отсутствии env-файла"; }

    local env=/tmp/no-repo.env
    grep -v '^RESTIC_REPOSITORY=' "$AICHAT_BACKUP_ENV" >"$env"
    rc=$(rc_of env AICHAT_BACKUP_ENV="$env" bash -c "bash '$BACKUP_DIR/init-repo.sh' >>'$OUT' 2>&1")
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "init без RESTIC_REPOSITORY: код $rc"; }
    grep -q "не задан RESTIC_REPOSITORY" "$OUT" || { cat "$OUT"; fail "нет текста о RESTIC_REPOSITORY"; }

    [[ ! -e "$RESTIC_PASSWORD_FILE" && ! -e "$RESTIC_PASSWORD_FILE.tmp" ]] || fail "остался файл пароля"
    ! grep -q "Сохраните пароль" "$OUT" || fail "просьба сохранить несуществующий пароль"
}

test_init_ignores_stale_tmp() {
    printf 'stale\n' >"$RESTIC_PASSWORD_FILE.tmp"
    run_script init-repo.sh || { cat "$OUT"; fail "init-repo.sh со старым .tmp упал"; }
    [[ "$(cat "$RESTIC_PASSWORD_FILE")" != stale ]] || fail "использовано содержимое старого .tmp"
    [[ ! -e "$RESTIC_PASSWORD_FILE.tmp" ]] || fail ".tmp не удалён"
    [[ "$(stat -c %a "$RESTIC_PASSWORD_FILE")" == 600 ]] || fail "права пароль-файла не 600"
}

test_repo_exists_distinguishes_network_error() {
    use_test_password
    local rc
    rc=$(rc_of run_lib 'load_env; repo_exists')
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "пустой бакет: ожидался код 1, получен $rc"; }
    [[ ! -s "$OUT" ]] || { cat "$OUT"; fail "repo_exists напечатал вывод restic"; }

    rc=$(rc_of run_lib 'load_env; RESTIC_REPOSITORY=s3:http://no-such-host.invalid:9000/aichat-test/aichat repo_exists')
    [[ "$rc" == 2 ]] || { cat "$OUT"; fail "несуществующий хост: ожидался код 2, получен $rc"; }
    [[ ! -s "$OUT" ]] || { cat "$OUT"; fail "repo_exists напечатал вывод restic"; }

    run_lib 'load_env; AWS_SECRET_ACCESS_KEY=wrong-secret repo_exists || echo "rc=$? $REPO_EXISTS_ERROR"' || true
    grep -q '^rc=2 restic не ответил за 15 с (сеть или S3 недоступны?): Stat: The request signature' "$OUT" \
        || { cat "$OUT"; fail "неверный ключ: ожидался код 2 и текст ошибки S3"; }
    assert_no_secrets "$OUT"
}

test_repo_exists_classification() {
    use_test_password
    # «Is there a repository…» без кода 10 и без «repository does not exist» — не «репозитория нет».
    local rc
    rc=$(rc_of run_lib "load_env; RESTIC_CMD=\"sh -c 'echo \\\"Is there a repository at the following location?\\\" >&2; exit 1' x\" repo_exists")
    [[ "$rc" == 2 ]] || { cat "$OUT"; fail "«Is there a repository» без кода 10: ожидался 2, получен $rc"; }
    rc=$(rc_of run_lib "load_env; RESTIC_CMD=\"sh -c 'echo \\\"Fatal: repository does not exist: x\\\" >&2; exit 1' x\" repo_exists")
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "«repository does not exist»: ожидался 1, получен $rc"; }
    rc=$(rc_of run_lib "load_env; RESTIC_CMD=\"sh -c 'exit 10' x\" repo_exists")
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "код 10: ожидался 1, получен $rc"; }

    # Таймаут проверяется до подстановки в eval.
    rc=$(rc_of run_lib "load_env; AICHAT_REPO_CHECK_TIMEOUT='1; touch /tmp/pwned;' repo_exists")
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "нечисловой таймаут принят"; }
    grep -q "AICHAT_REPO_CHECK_TIMEOUT должен быть целым числом" "$OUT" || { cat "$OUT"; fail "нет текста о таймауте"; }
    [[ ! -e /tmp/pwned ]] || fail "таймаут выполнен как код"
}

test_hc_ping_paths() {
    run_lib "hc_ping $HC_URL start; hc_ping $HC_URL ok; hc_ping $HC_URL fail boom" \
        || { cat "$OUT"; fail "hc_ping вернул ошибку"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start / /fail" ]] || { cat "$HC_LOG"; fail "пути пингов"; }
    [[ "$(jq -r 'select(.path == "/fail") | .body' "$HC_LOG")" == boom ]] || { cat "$HC_LOG"; fail "тело fail"; }

    curl -fsS -X POST "$HC_URL/reset" >/dev/null
    run_lib "hc_ping $HC_URL/ fail \"\$(printf 'x%.0s' {1..3000})\"" || { cat "$OUT"; fail "длинное тело"; }
    [[ "$(hc_paths)" == /fail ]] || { cat "$HC_LOG"; fail "путь при URL со слэшем"; }
    [[ "$(jq -r '.size' "$HC_LOG")" == 1024 ]] || { cat "$HC_LOG"; fail "тело не обрезано до 1024 байт"; }

    # Многобайтовое тело (2000 байт UTF-8) в UTF-8-локали: отправлено не больше 1024 байт.
    curl -fsS -X POST "$HC_URL/reset" >/dev/null
    run_lib "export LC_ALL=C.UTF-8; hc_ping $HC_URL fail \"\$(printf 'я%.0s' {1..1000})\"" \
        || { cat "$OUT"; fail "многобайтовое тело"; }
    local size
    size=$(jq -r '.size' "$HC_LOG")
    [[ "$size" =~ ^[0-9]+$ ]] || { cat "$HC_LOG"; fail "нет размера тела"; }
    ((size > 0 && size <= 1024)) || { cat "$HC_LOG"; fail "отправлено $size байт"; }
}

test_hc_ping_rejects_bad_url() {
    run_lib "hc_ping 'http://hc:8080/x\"y' start; hc_ping 'http://hc:8080/x\\y' ok; echo rc=\$?" \
        || { cat "$OUT"; fail "hc_ping с плохим URL вернул ошибку"; }
    [[ "$(grep -c '^aichat-backup: healthchecks: недопустимые символы в URL, пинг пропущен$' "$OUT")" == 2 ]] \
        || { cat "$OUT"; fail "нет предупреждений"; }
    grep -q '^rc=0$' "$OUT" || fail "код не 0"
    [[ ! -s "$HC_LOG" ]] || { cat "$HC_LOG"; fail "был запрос к hc"; }
}

test_hc_ping_unreachable_ok() {
    run_lib "hc_ping http://hc:9 start; echo rc=\$?" || { cat "$OUT"; fail "hc_ping на закрытый порт вернул ошибку"; }
    grep -q '^rc=0$' "$OUT" || { cat "$OUT"; fail "код не 0"; }
    grep -q '^aichat-backup: healthchecks:' "$OUT" || { cat "$OUT"; fail "нет предупреждения в stderr"; }
    ! grep -q 'hc:9' "$OUT" || { cat "$OUT"; fail "URL пинга попал в журнал"; }
}

test_hc_ping_empty_url_skipped() {
    run_lib 'hc_ping "" start; echo rc=$?' || { cat "$OUT"; fail "hc_ping с пустым URL вернул ошибку"; }
    grep -q '^aichat-backup: healthchecks: URL не задан, пинг пропущен$' "$OUT" || { cat "$OUT"; fail "нет предупреждения"; }
    grep -q '^rc=0$' "$OUT" || fail "код не 0"
    [[ ! -s "$HC_LOG" ]] || fail "был запрос к hc"
}

test_lib_helpers() {
    # load_env: нет env-файла → die с текстом
    [[ "$(rc_of run_lib 'AICHAT_BACKUP_ENV=/nonexistent load_env')" == 1 ]] || fail "load_env без файла не упал"
    grep -q '^aichat-backup: Нет /nonexistent$' "$OUT" || { cat "$OUT"; fail "текст ошибки load_env"; }
    # load_env: нет пароля → die
    [[ "$(rc_of run_lib 'load_env')" == 1 ]] || fail "load_env без пароля не упал"
    use_test_password
    run_lib 'load_env; echo "$RESTIC_REPOSITORY"; bash -c "echo \$AWS_ACCESS_KEY_ID"' || { cat "$OUT"; fail "load_env"; }
    [[ "$(paste -sd ' ' "$OUT")" == "s3:http://minio:9000/aichat-test/aichat aichat-test-key" ]] \
        || { cat "$OUT"; fail "load_env не экспортировал переменные"; }

    # write_status: каталог 700, атомарная запись (имя в state-каталоге или полный путь), без временных файлов
    run_lib 'write_status status.json "{\"result\":\"ok\"}"; write_status "$AICHAT_STATE_DIR/r.json" "{}"' \
        || { cat "$OUT"; fail "write_status"; }
    [[ "$(stat -c %a "$AICHAT_STATE_DIR")" == 700 ]] || fail "права state-каталога не 700"
    [[ "$(jq -r .result "$AICHAT_STATE_DIR/status.json")" == ok ]] || fail "status.json"
    local files=("$AICHAT_STATE_DIR"/*)
    [[ "${files[*]}" == "$AICHAT_STATE_DIR/r.json $AICHAT_STATE_DIR/status.json" ]] || fail "лишние файлы: ${files[*]}"

    # now_epoch
    run_lib 'AICHAT_NOW=1700000000 now_epoch' || fail "now_epoch"
    [[ "$(cat "$OUT")" == 1700000000 ]] || fail "now_epoch с AICHAT_NOW"
    run_lib 'now_epoch' || fail "now_epoch"
    [[ "$(cat "$OUT")" =~ ^[0-9]{10}$ ]] || fail "now_epoch без AICHAT_NOW"

    # db_psql / db_dump / db_restore / restic_cmd через переопределённые команды (stdin пробрасывается)
    run_lib 'db_psql -tAc "SELECT count(*) FROM users"' || { cat "$OUT"; fail "db_psql"; }
    [[ "$(cat "$OUT")" == 2 ]] || { cat "$OUT"; fail "db_psql: ожидалось 2 users"; }
    run_lib 'db_dump | db_restore --list | grep -c "TABLE DATA public users"' || { cat "$OUT"; fail "db_dump | db_restore"; }
    [[ "$(cat "$OUT")" == 1 ]] || { cat "$OUT"; fail "в дампе нет данных users"; }
    run_lib 'load_env; restic_cmd init >/dev/null; printf "abc" | restic_cmd backup -q --stdin --stdin-filename x.txt; restic_cmd dump latest /x.txt' \
        || { cat "$OUT"; fail "restic_cmd"; }
    [[ "$(cat "$OUT")" == abc ]] || { cat "$OUT"; fail "restic_cmd: stdin не проброшен"; }

    # значения по умолчанию (сервер)
    env -u AICHAT_BACKUP_ENV -u RESTIC_PASSWORD_FILE -u AICHAT_STATE_DIR -u RESTIC_CMD -u PG_DUMP_CMD -u PSQL_CMD -u PG_RESTORE_CMD \
        bash -c ". '$BACKUP_DIR/lib.sh'; printf '%s\n' \"\$AICHAT_BACKUP_ENV\" \"\$RESTIC_PASSWORD_FILE\" \"\$AICHAT_STATE_DIR\" \"\$RESTIC_CMD\" \"\$PG_DUMP_CMD\" \"\$PSQL_CMD\" \"\$PG_RESTORE_CMD\"" >"$OUT"
    diff -u - "$OUT" <<EOF || fail "значения по умолчанию"
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
    [[ "$(cat "$OUT")" == 'a *** b *** c ***/x d ***' ]] || { cat "$OUT"; fail "redact не вырезал секреты"; }

    run_lib 'error_text last "$(printf "one\ntwo\n\n  \n")"; echo; error_text first "$(printf "\n one\ntwo\n")"' \
        || { cat "$OUT"; fail "error_text"; }
    [[ "$(paste -sd '|' "$OUT")" == 'two| one' ]] || { cat "$OUT"; fail "error_text: первая/последняя строка"; }

    # JSON-ошибка restic (--json) → поле message; секреты вырезаются и в нём.
    run_lib 'load_env; error_text last "{\"message_type\":\"exit_error\",\"code\":1,\"message\":\"Fatal: $AWS_SECRET_ACCESS_KEY\"}"' \
        || { cat "$OUT"; fail "error_text json"; }
    [[ "$(cat "$OUT")" == 'Fatal: ***' ]] || { cat "$OUT"; fail "error_text: message из JSON"; }

    run_lib 'error_text last "$(printf "x%.0s" {1..3000})" | wc -c; error_text last "" | wc -c' \
        || { cat "$OUT"; fail "error_text длинная/пустая"; }
    [[ "$(paste -sd ' ' "$OUT")" == '1024 0' ]] || { cat "$OUT"; fail "error_text: не обрезано до 1024 байт"; }
}

# --- тесты: backup.sh ---------------------------------------------------------

test_backup_creates_snapshot() {
    init_test_repo
    run_script backup.sh || { cat "$OUT"; fail "backup.sh вернул ошибку"; }
    [[ "$(snapshot_count)" == 1 ]] || { snapshots_json; fail "снапшотов не 1"; }
    snapshots_json | jq -e '.[0].hostname == "aichat" and .[0].paths == ["/aichat.dump"]' >/dev/null \
        || { snapshots_json; fail "хост или путь снапшота"; }
    # Без AICHAT_NOW снапшот — текущего времени (--time не передаётся).
    local age
    age=$(($(date +%s) - $(date -d "$(snapshots_json | jq -r '.[0].time')" +%s)))
    ((age >= 0 && age < 600)) || fail "время снапшота отличается от текущего на $age с"

    jq -e '.result == "ok" and (.snapshot | test("^[0-9a-f]{8}$")) and (.size_bytes > 0)
        and (.duration_s | type == "number") and .error == null
        and (.time | test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"))' "$STATUS" >/dev/null \
        || { cat "$STATUS"; fail "status.json"; }
    [[ "$(jq -r .snapshot "$STATUS")" == "$(snapshots_json | jq -r '.[0].short_id')" ]] \
        || { cat "$STATUS"; fail "snapshot в status.json не совпадает со снапшотом"; }
    local size
    size=$(restic_t dump latest /aichat.dump | wc -c)
    [[ "$(jq -r .size_bytes "$STATUS")" == "$size" ]] || { cat "$STATUS"; fail "size_bytes != размер дампа ($size)"; }
    restic_t dump latest /aichat.dump | pg_restore --list | grep -q "TABLE DATA public users" \
        || fail "в снапшоте нет дампа с данными users"
    [[ "$(hc_paths | paste -sd ' ')" == "/start /" ]] || { cat "$HC_LOG"; fail "пинги не /start, /"; }
}

test_backup_never_inits() {
    use_test_password
    local rc
    rc=$(rc_of run_script backup.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "без репозитория: код $rc"; }
    local msg="Репозиторий не найден — init не выполняется, проверьте бакет (возможна атака)"
    grep -qF "$msg" "$OUT" || { cat "$OUT"; fail "нет сообщения о ненайденном репозитории"; }
    [[ "$(rc_of restic_t cat config 2>/dev/null)" != 0 ]] || fail "репозиторий создан"
    [[ -z "$(mc ls --recursive "t/$BUCKET")" ]] || fail "backup.sh что-то записал в бакет"
    jq -e --arg msg "$msg" '.result == "error" and .snapshot == null and .size_bytes == null and .error == $msg' \
        "$STATUS" >/dev/null || { cat "$STATUS"; fail "status.json"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "пинги не /start, /fail"; }
    [[ "$(jq -r 'select(.path == "/fail") | .body' "$HC_LOG")" == "$msg" ]] || { cat "$HC_LOG"; fail "тело /fail"; }
}

test_pg_dump_failure_no_snapshot() {
    init_test_repo
    export PG_DUMP_CMD="sh -c 'echo partial; exit 3'"
    local rc
    rc=$(rc_of run_script backup.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "сбой pg_dump: код $rc"; }
    # restic успел записать снапшот из неполного дампа — backup.sh обязан его удалить.
    grep -q "из неполного дампа удалён" "$OUT" || { cat "$OUT"; fail "снапшот неполного дампа не удалялся"; }
    [[ "$(snapshot_count)" == 0 ]] || { snapshots_json; fail "остался снапшот"; }
    jq -e '.result == "error" and .snapshot == null and (.error | startswith("pg_dump: "))' "$STATUS" >/dev/null \
        || { cat "$STATUS"; fail "status.json"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "пинги"; }
    [[ -n "$(jq -r 'select(.path == "/fail") | .body' "$HC_LOG")" ]] || { cat "$HC_LOG"; fail "пустое тело /fail"; }

    # Последняя содержательная строка stderr pg_dump — в тексте ошибки.
    export PG_DUMP_CMD="sh -c 'echo partial; echo \"pg_dump: error: connection refused\" >&2; echo >&2; exit 1'"
    rc=$(rc_of run_script backup.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "сбой pg_dump (2): код $rc"; }
    [[ "$(jq -r .error "$STATUS")" == "pg_dump: pg_dump: error: connection refused" ]] \
        || { cat "$STATUS"; fail "текст ошибки pg_dump"; }
    [[ "$(snapshot_count)" == 0 ]] || { snapshots_json; fail "остался снапшот (2)"; }
}

test_restic_backup_failure() {
    init_test_repo
    # Настоящий restic, но `backup` — с неверным паролем: ошибка в JSON (exit_error) на stderr.
    printf 'wrong-password\n' >/tmp/wrong-password
    cat >/tmp/badpw-restic <<'EOF'
#!/bin/sh
if [ "$1" = backup ]; then RESTIC_PASSWORD_FILE=/tmp/wrong-password exec restic "$@"; fi
exec restic "$@"
EOF
    chmod +x /tmp/badpw-restic
    local rc
    rc=$(RESTIC_CMD=/tmp/badpw-restic rc_of run_script backup.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "сбой restic backup: код $rc"; }
    [[ "$(jq -r .error "$STATUS")" == "restic backup: Fatal: wrong password or no key found" ]] \
        || { cat "$STATUS"; fail "текст ошибки restic backup"; }
    [[ "$(snapshot_count)" == 0 ]] || { snapshots_json; fail "есть снапшот"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "пинги"; }

    # Дамп больше буфера канала: restic вышел, не читая, — pg_dump получает SIGPIPE (141);
    # это следствие, а не причина, и в текст ошибки не попадает.
    export PG_DUMP_CMD="sh -c 'yes | head -c 10000000'"
    rc=$(RESTIC_CMD=/tmp/badpw-restic rc_of run_script backup.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "сбой restic backup (SIGPIPE): код $rc"; }
    [[ "$(jq -r .error "$STATUS")" == "restic backup: Fatal: wrong password or no key found" ]] \
        || { cat "$STATUS"; fail "текст ошибки при SIGPIPE у pg_dump"; }
}

test_backup_env_error() {
    # Нет пароля репозитория: load_env падает до пингов (URL пинга ещё не известен),
    # но status.json получает ошибку — старый «ok» не остаётся.
    install -d -m 700 "$AICHAT_STATE_DIR"
    echo '{"result":"ok"}' >"$STATUS"
    local rc
    rc=$(rc_of run_script backup.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "код $rc"; }
    grep -q "Нет файла пароля репозитория" "$OUT" || { cat "$OUT"; fail "нет сообщения"; }
    jq -e '.result == "error" and .snapshot == null and (.error | startswith("Нет файла пароля репозитория"))' \
        "$STATUS" >/dev/null || { cat "$STATUS"; fail "status.json"; }
    [[ ! -s "$HC_LOG" ]] || { cat "$HC_LOG"; fail "были пинги"; }
}

test_cleanup_forget_failure_redacted() {
    init_test_repo
    make_leaky_restic
    export PG_DUMP_CMD="sh -c 'echo partial; exit 3'"
    local rc
    rc=$(RESTIC_CMD=/tmp/leaky-restic rc_of run_script backup.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "код $rc"; }
    jq -e '.error | test("^pg_dump: код выхода 3; не удалось удалить снапшот [0-9a-f]{8} из неполного дампа: Fatal: forget \\*\\*\\* \\*\\*\\*$")' \
        "$STATUS" >/dev/null || { cat "$STATUS"; fail "текст ошибки удаления снапшота"; }
    # stderr неудачного forget — в журнале, с вырезанными секретами.
    grep -qx 'Fatal: forget \*\*\* \*\*\*' "$OUT" || { cat "$OUT"; fail "stderr forget не в журнале"; }
    local all=/tmp/all-output
    cat "$OUT" "$STATUS" >"$all"
    jq -r .body "$HC_LOG" >>"$all"
    [[ "$(hc_paths | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "пинги"; }
    assert_no_secrets "$all"
    assert_no_key_id "$all"
}

test_sigterm_mid_dump_no_snapshot() {
    init_test_repo
    # Дамп «зависает» на 3 с; SIGTERM приходит backup.sh во время дампа. Дамп затем завершается,
    # restic сохраняет снапшот — backup.sh обязан его удалить и сообщить о прерывании.
    export PG_DUMP_CMD="sh -c 'touch /tmp/dump-started; echo partial; sleep 3; echo rest'"
    rm -f /tmp/dump-started
    bash "$BACKUP_DIR/backup.sh" >"$OUT" 2>&1 &
    local pid=$! i rc=0
    for i in $(seq 1 150); do
        [[ ! -e /tmp/dump-started ]] || break
        sleep 0.2
    done
    [[ -e /tmp/dump-started ]] || { kill "$pid"; cat "$OUT"; fail "дамп не начался"; }
    kill -TERM "$pid"
    wait "$pid" || rc=$?
    ((rc != 0)) || { cat "$OUT"; fail "прерванный бэкап вернул 0"; }
    [[ "$(snapshot_count)" == 0 ]] || { cat "$OUT"; snapshots_json; fail "остался снапшот прерванного дампа"; }
    jq -e '.result == "error" and .snapshot == null and (.error | startswith("Бэкап прерван сигналом (таймаут/остановка)"))' \
        "$STATUS" >/dev/null || { cat "$OUT" "$STATUS"; fail "status.json"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "пинги"; }
    [[ -n "$(jq -r 'select(.path == "/fail") | .body' "$HC_LOG")" ]] || fail "пустое тело /fail"
}

test_unexpected_error_reported() {
    init_test_repo
    # die внутри lib.sh (не через fail) — статус и /fail всё равно отправляются из trap EXIT.
    local rc
    rc=$(AICHAT_REPO_CHECK_TIMEOUT=abc rc_of run_script backup.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "код $rc"; }
    jq -e '.result == "error" and (.error | startswith("Непредвиденная ошибка backup.sh (код 1)"))' "$STATUS" >/dev/null \
        || { cat "$STATUS"; fail "status.json"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "пинги"; }
    [[ -z "$(find /tmp -maxdepth 1 -name 'tmp.*')" ]] || fail "временный каталог не удалён"
}

test_rotation_applied() {
    init_test_repo
    local base i
    base=$(date -u -d '2026-01-01 03:30:00' +%s)
    for i in $(seq 0 9); do
        export AICHAT_NOW=$((base + i * 86400))
        run_script backup.sh || { cat "$OUT"; fail "бэкап дня $i упал"; }
    done
    local count
    count=$(snapshot_count)
    ((count < 10)) || { snapshots_json; fail "ротация не применена: $count снапшотов"; }
    local days
    days=$(snapshots_json | jq -r '.[].time[:10]')
    for i in $(seq 3 9); do
        grep -qx "$(date -u -d "@$((base + i * 86400))" +%F)" <<<"$days" \
            || { echo "$days"; fail "нет снапшота дня $i"; }
    done
}

test_check_on_sunday_only() {
    init_test_repo
    local sunday
    sunday=$(date -u -d '2026-01-04 03:30:00' +%s)
    [[ "$(date -u -d "@$sunday" +%u)" == 7 ]] || fail "2026-01-04 не воскресенье"
    export AICHAT_NOW=$sunday
    run_script backup.sh || { cat "$OUT"; fail "бэкап в воскресенье упал"; }
    grep -q "no errors were found" "$OUT" || { cat "$OUT"; fail "в воскресенье нет restic check"; }
    export AICHAT_NOW=$((sunday + 86400))
    run_script backup.sh || { cat "$OUT"; fail "бэкап в понедельник упал"; }
    ! grep -q "no errors were found" "$OUT" || { cat "$OUT"; fail "restic check не в воскресенье"; }
}

test_no_secrets_in_output() {
    init_test_repo
    local all=/tmp/all-output
    collect() {
        cat "$OUT" "$STATUS" >>"$all"
        jq -r .body "$HC_LOG" >>"$all"
    }
    run_script backup.sh || { cat "$OUT"; fail "backup.sh вернул ошибку"; }
    collect

    # Неверный ключ AWS.
    local env=/tmp/bad-key.env rc
    sed 's/^AWS_SECRET_ACCESS_KEY=.*/AWS_SECRET_ACCESS_KEY=wrong-secret/' "$AICHAT_BACKUP_ENV" >"$env"
    rc=$(AICHAT_BACKUP_ENV=$env rc_of run_script backup.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "неверный ключ: код $rc"; }
    grep -q "Нет доступа к репозиторию: " "$OUT" || { cat "$OUT"; fail "нет текста об ошибке доступа"; }
    collect

    make_leaky_restic
    curl -fsS -X POST "$HC_URL/reset" >/dev/null
    rc=$(RESTIC_CMD=/tmp/leaky-restic rc_of run_script backup.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "сбой forget: код $rc"; }
    grep -qF 'leak: *** *** *** ***' "$OUT" || { cat "$OUT"; fail "stderr restic не попал в журнал с вырезанными секретами"; }
    [[ "$(jq -r .error "$STATUS")" == 'restic forget: Fatal: forget *** ***' ]] || { cat "$STATUS"; fail "текст ошибки forget"; }
    collect

    [[ "$(jq -r .path "$HC_LOG" | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "пинги"; }
    assert_no_secrets "$all"
    assert_no_key_id "$all"
}

test_dump_never_touches_disk() {
    init_test_repo
    touch /tmp/marker
    sleep 1
    run_script backup.sh || { cat "$OUT"; fail "backup.sh вернул ошибку"; }
    local found
    found=$(find / -xdev -name '*.dump' -newer /tmp/marker 2>/dev/null)
    [[ -z "$found" ]] || fail "файлы дампа на диске: $found"
    # Временный каталог для stderr шагов удалён.
    found=$(find /tmp -maxdepth 1 -name 'tmp.*' -newer /tmp/marker)
    [[ -z "$found" ]] || fail "остались временные файлы: $found"
}

test_stale_lock_removed() {
    init_test_repo
    # Фоновый подшелл сам становится restic (exec): $! — PID процесса restic.
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
    [[ -n "$locks" ]] || fail "lock не появился"
    kill -9 "$pid"
    wait "$pid" 2>/dev/null || true
    ! kill -0 "$pid" 2>/dev/null || fail "процесс restic жив"
    restic_t cat lock "$locks" | jq -e --arg h "$(hostname)" --argjson pid "$pid" \
        '.hostname == $h and .pid == $pid' >/dev/null || { restic_t cat lock "$locks"; fail "lock не того процесса"; }

    run_script backup.sh || { cat "$OUT"; fail "backup.sh со старым lock упал"; }
    [[ -z "$(restic_t list locks --no-lock)" ]] || fail "lock не снят"
    [[ "$(snapshot_count)" == 1 ]] || { snapshots_json; fail "снапшотов не 1"; }
}

# --- тесты: restore-test.sh ---------------------------------------------------

test_restore_ok() {
    init_test_repo
    run_script backup.sh || { cat "$OUT"; fail "backup.sh вернул ошибку"; }
    curl -fsS -X POST "$HC_URL/reset" >/dev/null
    run_script restore-test.sh || { cat "$OUT"; fail "restore-test.sh вернул ошибку"; }
    jq -e --arg id "$(snapshots_json | jq -r '.[0].short_id')" \
        '.result == "ok" and .snapshot == $id and .users == 2 and .error == null
        and (.snapshot_age_h | type == "number" and . >= 0 and . < 1)
        and (.time | test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"))' "$RESTORE_STATUS" >/dev/null \
        || { cat "$RESTORE_STATUS"; fail "restore-test.json"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start /" ]] || { cat "$HC_LOG"; fail "пинги не /start, /"; }
    ! db_exists restore_test || fail "restore_test не удалена"
}

test_restore_fails_on_stale_snapshot() {
    init_test_repo
    export AICHAT_NOW=$RT_BASE
    run_script backup.sh || { cat "$OUT"; fail "backup.sh вернул ошибку"; }
    curl -fsS -X POST "$HC_URL/reset" >/dev/null

    # 47 часов — ещё в норме.
    AICHAT_NOW=$((RT_BASE + 47 * 3600)) run_script restore-test.sh || { cat "$OUT"; fail "снапшот 47 ч: ошибка"; }
    jq -e '.result == "ok" and .snapshot_age_h == 47' "$RESTORE_STATUS" >/dev/null \
        || { cat "$RESTORE_STATUS"; fail "snapshot_age_h при 47 ч"; }

    # 49 часов — ошибка, БД не создавалась.
    curl -fsS -X POST "$HC_URL/reset" >/dev/null
    local rc msg="Последний снапшот старше 48 часов: бэкапы не создаются"
    rc=$(AICHAT_NOW=$((RT_BASE + 49 * 3600)) rc_of run_script restore-test.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "снапшот 49 ч: код $rc"; }
    grep -qF "$msg" "$OUT" || { cat "$OUT"; fail "нет сообщения о 48 часах"; }
    jq -e --arg msg "$msg" '.result == "error" and .error == $msg and .users == null and .snapshot_age_h == 49
        and (.snapshot | test("^[0-9a-f]{8}$"))' "$RESTORE_STATUS" >/dev/null \
        || { cat "$RESTORE_STATUS"; fail "restore-test.json"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "пинги не /start, /fail"; }
    [[ "$(jq -r 'select(.path == "/fail") | .body' "$HC_LOG")" == "$msg" ]] || { cat "$HC_LOG"; fail "тело /fail"; }
    ! db_exists restore_test || fail "restore_test осталась"
}

test_restore_db_dropped_on_failure() {
    init_test_repo
    run_script backup.sh || { cat "$OUT"; fail "backup.sh вернул ошибку"; }
    curl -fsS -X POST "$HC_URL/reset" >/dev/null
    local rc
    rc=$(PG_RESTORE_CMD="sh -c 'cat >/dev/null; exit 1'" rc_of run_script restore-test.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "код $rc"; }
    ! db_exists restore_test || fail "restore_test осталась после сбоя"
    jq -e '.result == "error" and .users == null and (.error | startswith("pg_restore: "))' "$RESTORE_STATUS" >/dev/null \
        || { cat "$RESTORE_STATUS"; fail "restore-test.json"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "пинги"; }

    # Сбой проверки содержимого (таблица users пуста) — тоже ошибка, БД удалена.
    curl -fsS -X POST "$HC_URL/reset" >/dev/null
    rc=$(PG_RESTORE_CMD="sh -c 'cat >/dev/null; psql -h db -U aichat -d restore_test -c \"CREATE TABLE users (id int); CREATE TABLE alembic_version (v text)\"'" \
        rc_of run_script restore-test.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "пустая users: код $rc"; }
    jq -e '.result == "error" and (.error | test("users пуста"))' "$RESTORE_STATUS" >/dev/null \
        || { cat "$RESTORE_STATUS"; fail "ошибка пустой users"; }
    ! db_exists restore_test || fail "restore_test осталась после пустой users"
}

test_restore_fails_without_snapshots() {
    init_test_repo
    local rc msg="В репозитории нет снапшотов aichat-db: бэкапы не создаются"
    rc=$(rc_of run_script restore-test.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "код $rc"; }
    grep -qF "$msg" "$OUT" || { cat "$OUT"; fail "нет сообщения об отсутствии снапшотов"; }
    jq -e --arg msg "$msg" '.result == "error" and .error == $msg and .snapshot == null and .snapshot_age_h == null' \
        "$RESTORE_STATUS" >/dev/null || { cat "$RESTORE_STATUS"; fail "restore-test.json"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "пинги"; }
    ! db_exists restore_test || fail "restore_test создана"

    # Репозитория нет вовсе — ошибка restic в статусе.
    reset_env
    use_test_password
    rc=$(rc_of run_script restore-test.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "без репозитория: код $rc"; }
    jq -e '.result == "error" and (.error | startswith("restic snapshots: "))' "$RESTORE_STATUS" >/dev/null \
        || { cat "$RESTORE_STATUS"; fail "restore-test.json без репозитория"; }
}

test_restore_does_not_touch_live_db() {
    init_test_repo
    run_script backup.sh || { cat "$OUT"; fail "backup.sh вернул ошибку"; }
    # Меняем рабочую БД после бэкапа: восстановленная копия (2 users) не должна затронуть её.
    local extra
    extra=$(psql -h db -U aichat -d aichat -tAq -c 'INSERT INTO users DEFAULT VALUES RETURNING id') || fail "INSERT"
    # shellcheck disable=SC2064  # id известен сейчас; тест идёт в подшелле, ловушка не выходит за него
    trap "psql -h db -U aichat -d aichat -qc 'DELETE FROM users WHERE id = $extra'" EXIT
    run_script restore-test.sh || { cat "$OUT"; fail "restore-test.sh вернул ошибку"; }
    [[ "$(psql -h db -U aichat -d aichat -tAc 'SELECT count(*) FROM users')" == 3 ]] || fail "рабочая БД изменена"
    [[ "$(psql -h db -U aichat -d aichat -tAc 'SELECT count(*) FROM alembic_version')" == 1 ]] || fail "alembic_version изменена"
    jq -e '.users == 2' "$RESTORE_STATUS" >/dev/null || { cat "$RESTORE_STATUS"; fail "в копии не 2 users"; }
}

test_restore_env_error() {
    install -d -m 700 "$AICHAT_STATE_DIR"
    echo '{"result":"ok"}' >"$RESTORE_STATUS"
    local rc
    rc=$(rc_of run_script restore-test.sh)
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "код $rc"; }
    grep -q "Нет файла пароля репозитория" "$OUT" || { cat "$OUT"; fail "нет сообщения"; }
    jq -e '.result == "error" and .snapshot == null and (.error | startswith("Нет файла пароля репозитория"))' \
        "$RESTORE_STATUS" >/dev/null || { cat "$RESTORE_STATUS"; fail "restore-test.json"; }
    [[ ! -s "$HC_LOG" ]] || { cat "$HC_LOG"; fail "были пинги"; }
}

test_restore_sigterm_drops_db() {
    init_test_repo
    run_script backup.sh || { cat "$OUT"; fail "backup.sh вернул ошибку"; }
    curl -fsS -X POST "$HC_URL/reset" >/dev/null
    rm -f /tmp/restore-started
    export PG_RESTORE_CMD="sh -c 'touch /tmp/restore-started; sleep 3; cat >/dev/null'"
    bash "$BACKUP_DIR/restore-test.sh" >"$OUT" 2>&1 &
    local pid=$! i rc=0
    for i in $(seq 1 150); do
        [[ ! -e /tmp/restore-started ]] || break
        sleep 0.2
    done
    [[ -e /tmp/restore-started ]] || { kill "$pid"; cat "$OUT"; fail "восстановление не началось"; }
    kill -TERM "$pid"
    wait "$pid" || rc=$?
    ((rc != 0)) || { cat "$OUT"; fail "прерванная проверка вернула 0"; }
    jq -e '.result == "error" and (.error | startswith("Проверка восстановления прервана сигналом"))' "$RESTORE_STATUS" >/dev/null \
        || { cat "$OUT" "$RESTORE_STATUS"; fail "restore-test.json"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "пинги"; }
    ! db_exists restore_test || fail "restore_test осталась после сигнала"
}

test_restore_no_secrets_on_failure() {
    init_test_repo
    run_script backup.sh || { cat "$OUT"; fail "backup.sh вернул ошибку"; }
    curl -fsS -X POST "$HC_URL/reset" >/dev/null
    # restic, печатающий секреты на каждом вызове и падающий на dump с секретами в тексте ошибки.
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
    [[ "$rc" == 1 ]] || { cat "$OUT"; fail "код $rc"; }
    grep -qF 'leak: *** *** *** *** ***' "$OUT" || { cat "$OUT"; fail "stderr restic не попал в журнал с вырезанными секретами"; }
    jq -e '.error | startswith("restic dump: Fatal: dump *** *** ***")' "$RESTORE_STATUS" >/dev/null \
        || { cat "$RESTORE_STATUS"; fail "текст ошибки restic dump"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start /fail" ]] || { cat "$HC_LOG"; fail "пинги"; }
    local all=/tmp/all-output
    cat "$OUT" "$RESTORE_STATUS" >"$all"
    jq -r .body "$HC_LOG" >>"$all"
    assert_no_secrets "$all"
    assert_no_key_id "$all"
    ! db_exists restore_test || fail "restore_test осталась"
}

test_iso_to_epoch() {
    run_lib 'iso_to_epoch 2026-01-01T03:30:00Z; iso_to_epoch 2026-01-01T03:30:00.123456789Z;
        iso_to_epoch 2026-01-01T05:30:00.5+02:00; iso_to_epoch 2026-01-01T03:30:00-00:00; iso_to_epoch 2025-12-31T22:30:00.25-05:00' \
        || { cat "$OUT"; fail "iso_to_epoch"; }
    [[ "$(sort -u "$OUT" | paste -sd ' ')" == "$RT_BASE" ]] || { cat "$OUT"; fail "iso_to_epoch: разные эпохи, ожидалось $RT_BASE"; }
    [[ "$(rc_of run_lib 'iso_to_epoch not-a-time')" != 0 ]] || fail "iso_to_epoch принял мусор"
}

test_shellcheck() {
    shellcheck -x "$BACKUP_DIR"/*.sh "$TEST_DIR/run.sh" || fail "shellcheck нашёл замечания"
}

# --- запуск ------------------------------------------------------------------

main() {
    local tests=("$@") t rc failed=0
    if ((${#tests[@]} == 0)); then
        mapfile -t tests < <(declare -F | awk '$3 ~ /^test_/ {print $3}')
    fi
    for t in "${tests[@]}"; do
        if ! declare -F "$t" >/dev/null; then
            echo "FAIL $t (нет такого теста)"
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
