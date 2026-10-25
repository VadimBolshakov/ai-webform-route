# shellcheck shell=bash
# Общие функции скриптов бэкапа. Подключение: . "$(dirname "$0")/lib.sh"
# Переменные ниже можно переопределить окружением (тесты: ops/backup/test/compose.yml);
# без переопределения действуют значения для сервера.

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
# Для restic, запущенного без docker (тесты), пароль берётся из этого файла.
export RESTIC_PASSWORD_FILE

log() { printf 'aichat-backup: %s\n' "$*" >&2; }

die() {
    log "$*"
    exit 1
}

# Загружает /etc/aichat/restic.env (KEY=VALUE без кавычек) и экспортирует переменные.
load_env() {
    [[ -f "$AICHAT_BACKUP_ENV" ]] || die "Нет $AICHAT_BACKUP_ENV"
    set -a
    # shellcheck source=/dev/null
    . "$AICHAT_BACKUP_ENV"
    set +a
    [[ -s "$RESTIC_PASSWORD_FILE" ]] || die "Нет файла пароля репозитория $RESTIC_PASSWORD_FILE (или он пуст)"
}

# Команды из переменных выполняются через eval: в них могут быть кавычки (переопределения в тестах).
restic_cmd() { eval "$RESTIC_CMD"' "$@"'; }
db_dump() { eval "$PG_DUMP_CMD"; }
db_psql() { eval "$PSQL_CMD"' "$@"'; }
db_restore() { eval "$PG_RESTORE_CMD"' "$@"'; }

# repo_exists: 0 — репозиторий есть; 1 — restic сообщил, что репозитория нет; 2 — другая ошибка
# (сеть, ключи, пароль). Вывод restic не печатает; при 1 и 2 текст ошибки — в REPO_EXISTS_ERROR.
# При сетевых ошибках и неверных ключах restic повторяет запрос до 15 минут, поэтому проверка
# ограничена AICHAT_REPO_CHECK_TIMEOUT секундами (по умолчанию 120); истечение — код 2.
: "${AICHAT_REPO_CHECK_TIMEOUT:=120}"
# shellcheck disable=SC2034  # REPO_EXISTS_ERROR читают вызывающие скрипты
REPO_EXISTS_ERROR=""
# shellcheck disable=SC2034  # присваивание REPO_EXISTS_ERROR внутри функции
repo_exists() {
    local rc=0 out retry
    out=$(eval "timeout $AICHAT_REPO_CHECK_TIMEOUT $RESTIC_CMD cat config" 2>&1 >/dev/null </dev/null) || rc=$?
    REPO_EXISTS_ERROR=$out
    if ((rc == 0)); then
        REPO_EXISTS_ERROR=""
        return 0
    fi
    if ((rc == 124)); then
        retry=$(printf '%s\n' "$out" | grep -m 1 'returned error, retrying' | sed 's/^.*retrying after [^:]*: //') || true
        REPO_EXISTS_ERROR="restic не ответил за $AICHAT_REPO_CHECK_TIMEOUT с (сеть или S3 недоступны?)${retry:+: $retry}"
        return 2
    fi
    if ((rc == 10)) || [[ "$out" == *"repository does not exist"* ]] \
        || [[ "$out" == *"Is there a repository at the following location"* ]]; then
        return 1
    fi
    return 2
}

# hc_ping <url> <start|ok|fail> [body] — пинг healthchecks.io; сбой пинга не меняет код выхода.
# URL передаётся curl через конфиг на дескрипторе, а не аргументом (не виден в ps); в журнал не пишется.
hc_ping() {
    local url=${1:-} suffix=${2:-} body=${3-} target rc=0
    if [[ -z "$url" ]]; then
        log "healthchecks: URL не задан, пинг пропущен"
        return 0
    fi
    url=${url%/}
    case "$suffix" in
        start) target="$url/start" ;;
        ok) target="$url" ;;
        fail) target="$url/fail" ;;
        *)
            log "healthchecks: неизвестный тип пинга «$suffix», пинг пропущен"
            return 0
            ;;
    esac
    local LC_ALL=C
    body=${body:0:1024}
    printf '%s' "$body" | curl -fsS -m 10 --retry 3 -o /dev/null --data-binary @- \
        -K <(printf 'url = "%s"\n' "$target") 2>/dev/null || rc=$?
    if ((rc != 0)); then
        log "healthchecks: пинг «$suffix» не доставлен (curl, код $rc)"
    fi
    return 0
}

# write_status <file> <json> — атомарная запись; file — имя в $AICHAT_STATE_DIR или полный путь.
write_status() {
    local file=$1 json=$2 tmp
    [[ "$file" == */* ]] || file="$AICHAT_STATE_DIR/$file"
    [[ -d "$AICHAT_STATE_DIR" ]] || install -d -m 700 "$AICHAT_STATE_DIR"
    tmp=$(mktemp "$file.XXXXXX")
    printf '%s\n' "$json" >"$tmp"
    mv -f "$tmp" "$file"
}

now_epoch() { printf '%s\n' "${AICHAT_NOW:-$(date +%s)}"; }
