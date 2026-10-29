# Telegram-бот — решения, принятые при выполнении плана

Источник: ledger subagent-driven-development (2026-10-29). Решение — почему — цена ошибки.

- Ruling: commit trailer = model that wrote the commit.
- Ruling: Task 2 adds a 10-minute flush_dropped job (only when notifier is TelegramNotifier); the «Пропущено» line counts toward the hourly window — cost: summary may lag up to the window end
- Ruling: tzdata added as runtime dependency (ZoneInfo on Windows) — cost: one small package
- Ruling: Task 2 wraps notifier.drain() in asyncio.wait_for(…, 20) in lifespan shutdown — bounded shutdown — cost: alerts in flight may be lost at shutdown
- Ruling: accept Task 2 open choices (month cost = UTC month of window end; active = user messages or usage; weekly title format; stats_text helper) — cost: cosmetic
- Ruling: Task 3 passes settings.digest_timezone to users_text/stats_text; /users last activity = max(latest Usage, AuthSession.last_seen) for consistency with admin page — cost: extra query
- Ruling: accept /start alias for /help, WARNING level for ignored-chat log, BotDeps.clock, sequential update handling — cost: none
- Ruling: owner alerts stay English (they are also server log lines; owner asked for English server-facing text); digests/command replies Russian — cost: mixed-language Telegram messages
- Ruling: revoked-token 60 s warning loop not rate-limited — acceptable log noise — cost: ~1440 lines/day if token revoked
