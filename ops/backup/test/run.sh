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
    rm -f "$RESTIC_PASSWORD_FILE"
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
}

test_hc_ping_paths() {
    run_lib "hc_ping $HC_URL start; hc_ping $HC_URL ok; hc_ping $HC_URL fail boom" \
        || { cat "$OUT"; fail "hc_ping вернул ошибку"; }
    [[ "$(hc_paths | paste -sd ' ')" == "/start / /fail" ]] || { cat "$HC_LOG"; fail "пути пингов"; }
    [[ "$(jq -r 'select(.path == "/fail") | .body' "$HC_LOG")" == boom ]] || { cat "$HC_LOG"; fail "тело fail"; }

    curl -fsS -X POST "$HC_URL/reset" >/dev/null
    run_lib "hc_ping $HC_URL/ fail \"\$(printf 'x%.0s' {1..3000})\"" || { cat "$OUT"; fail "длинное тело"; }
    [[ "$(hc_paths)" == /fail ]] || { cat "$HC_LOG"; fail "путь при URL со слэшем"; }
    [[ "$(jq -r '.body | length' "$HC_LOG")" == 1024 ]] || { cat "$HC_LOG"; fail "тело не обрезано до 1024 байт"; }
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
