# AI Chat (MVP)

[![CI](https://github.com/VadimBolshakov/ai-webform-route/actions/workflows/ci.yml/badge.svg)](https://github.com/VadimBolshakov/ai-webform-route/actions/workflows/ci.yml)

Закрытый веб-чат с ИИ-моделями (FastAPI + PostgreSQL, модели через OpenRouter). Доступ только по приглашениям; вход по секретному адресу.

## Требования

- Docker Desktop (Compose v2)
- Python 3.13 (для локальных тестов и проверок; в образе используется `python:3.13-slim`)
- PowerShell (для `scripts/check.ps1`)

## Первый запуск

1. Создайте виртуальное окружение и поставьте зависимости (нужно для тестов и проверок):

   ```powershell
   python -m venv .venv
   .venv\Scripts\python -m pip install -e ".[dev]"
   ```

2. Сгенерируйте секреты:

   ```powershell
   # FERNET_KEY
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   # LOGIN_PATH_TOKEN
   python -c "import secrets; print(secrets.token_urlsafe(24))"
   ```

3. Создайте `.env` из примера и заполните его (`.env` не коммитится):

   ```powershell
   Copy-Item .env.example .env
   ```

   Обязательно задайте `FERNET_KEY`, `LOGIN_PATH_TOKEN`, `OPENROUTER_API_KEY`. `TITLE_MODEL` (модель для заголовков чатов) по умолчанию `openai/gpt-4o-mini`; пустое значение не допускается. `BASE_URL` — только origin (`https://host[:port]`, без пути; `http` — лишь для localhost), иначе приложение не стартует. Если порт 5432 на хосте занят (например, локальным PostgreSQL), поставьте `DB_HOST_PORT=5433`.

   **`AUTO_ALLOWED_MODELS`**: если список пуст, режим Auto (`openrouter/auto`) может выбрать любую модель, включая дорогие. Задайте список (пример закомментирован в `.env.example`) вместе с жёстким лимитом на ключе OpenRouter (см. ниже).

4. Запустите всё:

   ```powershell
   docker compose up -d --build
   docker compose ps   # db, app, caddy должны быть healthy
   ```

   При старте контейнер `app` сам выполняет `alembic upgrade head`.

5. Создайте администратора (пароль спросят интерактивно, минимум 10 символов):

   ```powershell
   docker compose exec app python -m app.cli create-admin vadim
   ```

## Восстановление доступа администратора

Если потерян телефон с 2FA или пароль (все сессии пользователя при этом отзываются, действие пишется в журнал аудита):

```powershell
docker compose exec app python -m app.cli reset-2fa vadim       # выключить 2FA; при входе админ настроит её заново
docker compose exec app python -m app.cli reset-password vadim  # новый пароль (спросят дважды)
```

## Вход

Откройте `https://localhost:8443/a/<LOGIN_PATH_TOKEN>` (значение из `.env`). Главная страница `https://localhost:8443/` — ширма-заглушка.

HTTPS терминирует Caddy с внутренним сертификатом (`tls internal`), поэтому браузер покажет предупреждение о самоподписанном сертификате: это ожидаемо при локальном запуске, продолжите вручную. HTTPS нужен для cookie `__Host-sid` (`Secure`).

Порт приложения наружу не публикуется: на хост проброшен только `127.0.0.1:8443` (Caddy). Приложение доверяет заголовкам `X-Forwarded-*` только от Caddy (фиксированный адрес `172.28.0.10` в сети compose; переменная `FORWARDED_ALLOW_IPS`). Access-логи uvicorn и Caddy отключены, чтобы токены входа и приглашений не попадали в логи. Примечание: в Docker Desktop все подключения с хоста приходят с адреса шлюза сети (`172.28.0.1`), поэтому в журнале аудита локально виден он; на сервере будет реальный IP клиента.

## Эксплуатация

- Uvicorn должен работать **в одном воркере** (как в `docker/entrypoint.sh`, без `--workers`): лимитеры запросов и планировщик фоновых задач живут в памяти процесса; несколько воркеров умножат лимиты и запустят задачи несколько раз.
- Тела запросов ограничены 64 КБ, `multipart/form-data` принимается только эндпоинтом отправки сообщений (у него свои лимиты вложений) — ничего не пишется на диск.
- Развёртывание на сервере — см. [docs/deploy.md](docs/deploy.md) (`docker-compose.prod.yml` + `docker/Caddyfile.prod`: настоящий сертификат Let's Encrypt, `/healthz` снаружи отдаёт 404, access-логи выключены — в путях `/a/*` и `/join/*` токены входа и приглашений).
- Telegram-бот владельца (алерты, ежедневные сводки, команды `/stats`, `/users`, `/invite`, `/block`) — см. [docs/deploy.md, раздел «Telegram-бот»](docs/deploy.md#10-telegram-бот).
- Бэкапы базы в AWS S3 (restic, ночью по таймеру systemd, ежемесячная проверка восстановления) и восстановление — см. [docs/backups.md](docs/backups.md).

## Тесты и проверки

Тесты используют БД `aichat_test` из контейнера `db`:

```powershell
docker compose up -d db
$env:DB_HOST_PORT=5433
$env:TEST_DATABASE_URL="postgresql+asyncpg://aichat:aichat@127.0.0.1:5433/aichat_test"
.venv\Scripts\python -m pytest -q
```

Полная проверка (ruff, pytest с `-W error`, pip-audit):

```powershell
.\scripts\check.ps1
```

Скрипт сам подставляет `DB_HOST_PORT=5433` и `TEST_DATABASE_URL`, если они не заданы.

Те же проверки и тест бэкапа запускаются в GitHub Actions на каждый push и PR (`.github/workflows/ci.yml`).

## Сброс базы

Скрипты из `docker/initdb` (создание `aichat_test`) выполняются только при первом создании тома `pgdata`. Если том уже есть, а БД нет, или нужно начать с чистого листа:

```powershell
docker compose down -v   # удалит данные БД и локальный корневой сертификат Caddy
docker compose up -d --build
```

## Ручные шаги в кабинете OpenRouter

Это нельзя сделать из кода, сделайте один раз:

1. Выпустите отдельный ключ для этого приложения и задайте на нём **жёсткий месячный лимит** (Credit limit), чтобы ошибка или утечка не привели к большому счёту.
2. В настройках **Privacy** отключите использование ваших запросов для обучения моделей (запретите обучение на prompts) и логирование запросов, если доступно.
