#!/bin/sh
set -e
alembic upgrade head
exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 \
    --no-access-log --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-172.28.0.10}"
