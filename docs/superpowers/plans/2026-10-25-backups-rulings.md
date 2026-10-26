# Бэкапы — решения, принятые при выполнении плана

Источник: ledger subagent-driven-development (2026-10-25/26). Каждая строка: решение — почему — цена ошибки.

- Ruling: restic.env is plain KEY=VALUE without quotes (docker --env-file semantics), loaded in lib.sh via `set -a; . file; set +a` — one format for both consumers — cost: values with spaces unsupported (none needed).
- Ruling: if docker CLI fails under ProtectSystem=strict, T5 relaxes to ProtectSystem=full (+ReadWritePaths) and documents why — cost: weaker sandbox.
- Ruling: commit trailer = model that wrote the commit.
- Ruling: test-only S3 server = pgsty/minio + pgsty/mc (community fork; upstream minio images no longer published on Docker Hub) — test harness only, never on server — cost: third-party image in local tests; swap to another S3 emulator if it disappears.
- Ruling: repo_exists runs under timeout (AICHAT_REPO_CHECK_TIMEOUT, 120s) because restic 0.19 retries ~15 min; Task 2 reuses REPO_EXISTS_ERROR; long restic backup retries bounded by systemd TimeoutStartSec=2h.
- Ruling: fold cheap hardening into T1 fix round: init validates env before generating password + no-clobber publish; timeout var validated; "repo missing" only on exit 10 or exact text; secret-leak asserts; multibyte truncation test; HC URL with quote/backslash rejected — cost: slightly larger fix diff
- Ruling: undelete refusals returned as main()==2 (script exits 2) instead of raised SystemExit — equivalent for CLI, testable — cost: none.
- Ruling: fold Task 4 minors 1-3 into Task 5 dispatch (undelete: keep when current ETag/size equals target + per-key progress; warn/refuse as_of older than 30 days without --force-old; default --prefix aichat/ and warn without trailing slash) — needed for an honest runbook — cost: Task 5 touches undelete.py too
- Ruling: NoncurrentDays stays 30 (spec); raising to 90 documented as owner's option — cost: shorter detection window unless owner opts in
- Ruling: undelete MinIO round-trip test and production RESTIC_CMD path in automated suite deferred (moto + manual systemd verification) — cost: regression would surface only on server
- Final: parked — docs/backups.md §8 says "примерно 14" snapshots; real steady state ≈ 11 (10–12) with 7/4/3 policy; owner might start an unneeded rollback — Ruling: residual, controller's brief error; surfaced to owner for a one-line doc fix before merge
- Final: parked — init-repo interrupted after successful restic init deletes the only password (very unlikely) — Ruling: residual, deferred (doc note)
