# Telegram Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Owner-only Telegram bot inside the app: instant alerts (replacing LogNotifier), daily/weekly usage digests, and commands `/help /stats /users /invite /block /unblock`.

**Architecture:** New package `app/telegram/` — a thin async client over the Telegram Bot HTTP API (httpx), a `TelegramNotifier` implementing the existing `Notifier` protocol, a `stats` module with pure SQL aggregation + text formatting, and a long-polling command loop started from the app lifespan. Block/unblock logic moves from `app/admin/routes.py` into `app/admin/service.py`, shared by the admin UI and the bot.

**Tech Stack:** Python 3.13, FastAPI lifespan, httpx (already a dependency), APScheduler (already), SQLAlchemy async, respx (tests). No new runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-10-29-telegram-bot-design.md`

## Global Constraints

- Settings (pydantic, all optional): `telegram_bot_token: str = ""`, `telegram_owner_chat_id: int | None = None`, `digest_timezone: str = "Europe/Moscow"` (validated with `zoneinfo.ZoneInfo`), `digest_time: str = "09:00"` (validated `HH:MM`). Bot is "configured" only when token and chat id are both set.
- Bot texts to the owner: Russian, plain text (no `parse_mode`). Server log lines: English.
- Never log Telegram API URLs (the token is in the path): set `logging.getLogger("httpx")` and `"httpcore"` to WARNING in `create_app`; log send failures as exception type + HTTP status only.
- Sending: `sendMessage` with `chat_id`, `text`, `disable_web_page_preview: true`; timeout 5 s; one retry on network error/5xx; on 429 sleep `parameters.retry_after` (cap 30 s) once; never raise to callers.
- Messages > 4000 chars are split on line boundaries into several messages.
- Alert rate limit: max 20 alerts per rolling hour; extras dropped and counted; the next alert or a 10-minute tick after the window frees sends «Пропущено уведомлений: N». Digests and command replies bypass the limit.
- Only `telegram_owner_chat_id` is served; other chats: no reply, log `telegram: ignored update from chat <id>`.
- Long polling: `getUpdates(offset, timeout=30, allowed_updates=["message"])`; on startup first call `getUpdates(offset=-1, timeout=0)` and start after its last update_id (skip backlog); on errors back off 1, 2, 4 … 60 s.
- Digest: daily at `digest_time` in `digest_timezone` for the previous calendar day in that zone; on Mondays a weekly digest for the previous Mon–Sun replaces the daily one. Only metadata — never message content, chat titles, filenames or IPs.
- Audit for bot actions: `ip = "telegram"`; `/invite` acts as the oldest active admin (`role == "admin" and is_active`, lowest id).
- The polling loop and digest job run only when `settings.enable_jobs` and the bot is configured.
- Commit trailer: Co-Authored-By line of the model that wrote the commit.

## Review Focus

1. **Token leaks into logs** — no log record (any level) contains the token, on success, HTTP error, network error or 429. Test: `test_token_never_logged` (Task 1).
2. **Content leaks into digests** — a message with a unique marker string and a chat titled with another marker exist; neither marker appears in daily/weekly digest or `/stats`. Test: `test_digest_has_no_content` (Task 2).
3. **Stranger commands** — `/block alice` from a non-owner chat changes nothing and sends nothing. Test: `test_foreign_chat_ignored` (Task 3).
4. **Backlog replay** — updates that existed before startup are not executed. Test: `test_backlog_skipped_on_start` (Task 3).
5. **Day boundaries in Europe/Moscow** — usage at 23:30 and 00:30 Moscow time land in the right days. Test: `test_daily_window_timezone` (Task 2).

---

## File Structure

```
app/telegram/__init__.py
app/telegram/client.py      TelegramClient: send_message, get_updates (httpx)
app/telegram/notifier.py    TelegramNotifier (Notifier protocol, rate limit, split)
app/telegram/stats.py       period windows, aggregation queries, text formatting for digests/stats/users
app/telegram/bot.py         command dispatch + polling loop
app/admin/service.py        block_user / unblock_user shared by admin UI and bot
tests/test_telegram_client.py, tests/test_telegram_notifier.py, tests/test_telegram_stats.py, tests/test_telegram_bot.py
```

---

### Task 1: Client, notifier, settings, wiring, `telegram-whoami`

**Files:**
- Create: `app/telegram/{__init__,client,notifier}.py`, `tests/test_telegram_client.py`, `tests/test_telegram_notifier.py`
- Modify: `app/config.py`, `app/main.py`, `app/cli.py`, `.env.example`, `tests/test_cli.py`

**Interfaces:**
- `TelegramClient(token: str, http: httpx.AsyncClient)`: `async send_message(chat_id: int, text: str) -> bool` (True if every part was accepted); `async get_updates(offset: int | None, timeout: int) -> list[dict]` (raises `TelegramError` on failure — the polling loop handles it); base URL `https://api.telegram.org/bot<token>/`.
- `TelegramNotifier(client: TelegramClient, chat_id: int, clock=time.monotonic)`: `async alert(text: str) -> None` (rate-limited per Global Constraints); `async send(text: str) -> None` (no limit — digests and replies); `async flush_dropped() -> None` (sends the «Пропущено…» line if anything was dropped and the window freed).
- `create_app`: if bot configured → `app.state.notifier = TelegramNotifier(...)` sharing the app's `httpx.AsyncClient`, plus `app.state.telegram = client`; else `LogNotifier` (unchanged). httpx/httpcore loggers → WARNING.
- CLI `python -m app.cli telegram-whoami`: reads `TELEGRAM_BOT_TOKEN` from settings, calls `getUpdates(timeout=0)`, prints `chat_id=<id> from=<username or first_name> text=<first 30 chars>` for each, or «No messages. Send any message to the bot first.»; exit 1 if token missing.
- `.env.example`: the four settings with English comments.

- [ ] **Step 1:** Tests (respx): `test_send_message_ok` (payload chat_id/text/disable_web_page_preview, no parse_mode); `test_send_splits_long_text` (9000 chars of lines → 3 requests, each ≤ 4000, order kept); `test_send_retries_once_on_5xx`; `test_send_honours_retry_after` (429 with retry_after=2 → one wait via injected sleep, then success); `test_send_never_raises` (network error twice → returns False); `test_token_never_logged` (caplog at DEBUG over success, 500, network error, 429 paths: token string absent); `test_alert_rate_limit` (25 alerts with fake clock → 20 sent; after window, `flush_dropped` sends «Пропущено уведомлений: 5»); `test_send_bypasses_limit`; `test_create_app_picks_notifier` (configured → TelegramNotifier, missing chat id → LogNotifier); `test_settings_validation` (bad timezone / bad time → ValidationError); `test_whoami_prints_chat_ids` (respx getUpdates).
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS (+ full app suite once). 
- [ ] **Step 5:** Commit `feat(telegram): client, owner notifier, settings and whoami CLI`.

### Task 2: Stats, digests and the digest job

**Files:**
- Create: `app/telegram/stats.py`, `tests/test_telegram_stats.py`
- Modify: `app/jobs.py`, `app/main.py`

**Interfaces:**
- `period_window(kind: Literal["day","week","today"], now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime, str]` — UTC start/end and a label (`"28.10.2026"`, `"21.10–27.10.2026"`, `"сегодня, 29.10.2026"`).
- `async collect(db, start: datetime, end: datetime) -> PeriodStats` — dataclass: `active_users`, `total_users`, `messages`, `new_chats`, `prompt_tokens`, `completion_tokens`, `cost_usd`, `month_cost_usd`, `per_user: list[UserLine(username, messages, cost, month_cost, limit)]` (only users with activity in the period, sorted by cost desc), `model_errors` (usage status error), `failed_logins` (audit `login_fail`), `new_users` (audit `invite_redeemed`), `top_models: list[(model, cost)]` (top 3). Counts user messages (`role == "user"`) only. Month = calendar month UTC (same as billing).
- `format_digest(title: str, s: PeriodStats, weekly: bool) -> str` — exactly the layout in spec §4 (numbers with space thousands separator, money `$0.84`); the no-activity one-liner when `messages == 0`.
- `async users_text(db) -> str` — `/users` text: `login — активен|заблокирован, $X из $Y, последняя активность ДД.ММ ЧЧ:ММ` (in digest tz) or «—».
- `jobs.py`: `async send_digest(factory, notifier, settings, now=None) -> str | None` (builds daily or, on Monday, weekly; sends via `notifier.send`; returns the text for tests); scheduled in `make_scheduler` with a cron trigger at `digest_time` in `digest_timezone`, `max_instances=1`, `coalesce=True`, `misfire_grace_time=3600`, only when bot configured.

- [ ] **Step 1:** Tests: `test_daily_window_timezone` (usage rows at 23:30 and 00:30 Moscow → split between days correctly); `test_weekly_window_monday` (now = Monday → previous Mon–Sun); `test_collect_counts` (fixtures: 2 users with messages/usage/errors, 1 idle; assert every field incl. month cost and limit); `test_digest_format` (exact string for a fixed PeriodStats); `test_digest_no_activity`; `test_digest_has_no_content` (message content "SECRET-MSG-42" and chat title "SECRET-TITLE-7" absent from digest and `/stats` text); `test_users_text`; `test_send_digest_monday_sends_weekly`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat(telegram): daily and weekly usage digests`.

### Task 3: Commands, polling loop, shared admin service, docs

**Files:**
- Create: `app/telegram/bot.py`, `app/admin/service.py`, `tests/test_telegram_bot.py`
- Modify: `app/admin/routes.py` (use service), `app/main.py` (start/stop polling task in lifespan), `docs/deploy.md` (new section «Telegram-бот»: BotFather, `telegram-whoami`, two `.env` values, `docker compose up -d`, `/help`), `README.md` (one line)

**Interfaces:**
- `app/admin/service.py`: `async block_user(db, actor: User, target: User, ip: str) -> None`, `async unblock_user(db, actor: User, target: User, ip: str) -> None`; raise `AdminActionError(message)` for blocking self/another admin. Admin routes keep their behaviour and tests.
- `bot.py`: `async handle_update(update: dict, deps: BotDeps) -> None` where `BotDeps(factory, notifier, settings, owner_chat_id)`; commands `/help`, `/stats`, `/users`, `/invite`, `/block <login>`, `/unblock <login>` (case-insensitive, `@botname` suffix stripped); replies via `notifier.send`; unknown → «Неизвестная команда. /help».
- `async run_polling(client, deps, stop: asyncio.Event) -> None` — skip backlog on start, then loop with backoff; started as a task in lifespan when bot configured and `enable_jobs`; cancelled on shutdown.

- [ ] **Step 1:** Tests: `test_help`; `test_stats_command` (text from `stats`); `test_users_command`; `test_invite_command` (creates invite, reply contains `/a/<token>/join/`, audit `invite_created` with ip telegram); `test_block_and_unblock` (is_active toggles, sessions revoked, audit rows); `test_block_admin_refused`; `test_block_unknown_user`; `test_foreign_chat_ignored`; `test_backlog_skipped_on_start` (respx: first getUpdates returns old update id 10 with `/block alice`; loop starts at 11; alice still active); `test_polling_backoff_on_error`; existing admin route tests still pass.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS + `.\scripts\check.ps1` green.
- [ ] **Step 5:** Commit `feat(telegram): owner commands and polling`.
