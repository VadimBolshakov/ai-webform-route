#!/usr/bin/env python3
"""Откат версионированного S3-бакета (репозиторий restic) на момент времени.

Запускать владельцу со своего компьютера, учётной записью владельца (не ключом сервера):
    python undelete.py --bucket B [--prefix aichat/] --as-of "2026-11-02T10:00:00Z" [--apply]

Без --apply печатается только план. Состояние ключа на момент as_of — самый свежий элемент
(версия объекта или delete marker) с LastModified <= as_of:
  версия объекта -> восстановить (если текущая версия не совпадает с ней по ETag и размеру),
  delete marker или ничего -> ключа не было.
Поэтому повторный запуск после --apply показывает, что делать больше нечего.
Lifecycle бакета удаляет старые версии через 30 дней: более ранний as_of — только с --force-old.
Коды выхода: 0 — успех, 2 — отказ (нет версионирования / нет прав / as_of старше 30 дней),
1 — прочие ошибки.
Зависимость: boto3 (только у владельца; приложение его не использует).
"""

import argparse
import sys
from datetime import UTC, datetime, timedelta

# Размер страницы list_object_versions (подменяется в тестах).
PAGE_SIZE = 1000

# Некактуальные версии lifecycle удаляет через столько дней (aws/lifecycle.json).
RETENTION_DAYS = 30

# Action = (kind: "restore" | "delete" | "keep", key, version_id | None)
Action = tuple[str, str, str | None]


class Refusal(Exception):
    """Отказ работать (код выхода 2)."""


def parse_as_of(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(
            f"Не удалось разобрать время «{value}»: нужен формат ISO 8601, "
            "например 2026-11-02T10:00:00Z"
        ) from None
    if dt.tzinfo is None:
        raise ValueError(
            f"В времени «{value}» не указан часовой пояс: добавьте Z (UTC) или смещение, "
            "например 2026-11-02T10:00:00Z или 2026-11-02T12:00:00+02:00"
        )
    return dt.astimezone(UTC)


def _error_code(exc) -> str:
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "")


def check_versioning(s3, bucket: str) -> None:
    try:
        status = s3.get_bucket_versioning(Bucket=bucket).get("Status")
    except Exception as exc:
        if _error_code(exc) == "AccessDenied":
            raise Refusal(
                "Нет прав на чтение версий: запустите с учётной записью владельца, "
                "не ключом сервера"
            ) from exc
        raise
    if status != "Enabled":
        raise Refusal("Версионирование бакета не включено — откат невозможно")


def _iter_entries(s3, bucket: str, prefix: str):
    """Все версии и delete markers под prefix:
    (key, is_marker, last_modified, is_latest, vid, pos, etag, size); у markers etag и size — None."""
    pos = 0
    kwargs = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": PAGE_SIZE}
    while True:
        try:
            page = s3.list_object_versions(**kwargs)
        except Exception as exc:
            if _error_code(exc) == "AccessDenied":
                raise Refusal(
                    "Нет прав на чтение версий: запустите с учётной записью владельца, "
                    "не ключом сервера"
                ) from exc
            raise
        for is_marker, items in (
            (False, page.get("Versions", [])),
            (True, page.get("DeleteMarkers", [])),
        ):
            for it in items:
                yield (
                    it["Key"],
                    is_marker,
                    it["LastModified"],
                    it["IsLatest"],
                    it["VersionId"],
                    pos,
                    it.get("ETag"),
                    it.get("Size"),
                )
                pos += 1
        if not page.get("IsTruncated"):
            return
        kwargs["KeyMarker"] = page["NextKeyMarker"]
        if page.get("NextVersionIdMarker"):
            kwargs["VersionIdMarker"] = page["NextVersionIdMarker"]
        else:
            kwargs.pop("VersionIdMarker", None)


def plan_restore(s3, bucket: str, prefix: str, as_of: datetime) -> list[Action]:
    if as_of.tzinfo is None:
        raise ValueError("as_of должен содержать часовой пояс")
    by_key: dict[str, list[tuple]] = {}
    for entry in _iter_entries(s3, bucket, prefix):
        by_key.setdefault(entry[0], []).append(entry)

    actions: list[Action] = []
    for key in sorted(by_key):
        entries = by_key[key]
        current = next((e for e in entries if e[3] and not e[1]), None)
        # При равных LastModified (секундная точность) новее текущая версия, затем та, что
        # раньше в ответе S3 (он отдаёт новые первыми); версии и markers приходят раздельно.
        at_as_of = [e for e in entries if e[2] <= as_of]
        target = max(at_as_of, key=lambda e: (e[2], e[3], -e[5]), default=None)
        if target is None or target[1]:
            # На момент as_of ключа не было (не существовал или был удалён).
            actions.append(("delete", key, None) if current else ("keep", key, None))
        elif target[3] or (
            current is not None and (current[6], current[7]) == (target[6], target[7])
        ):
            # Целевая версия текущая или текущая — её копия (например, после --apply).
            actions.append(("keep", key, target[4]))
        else:
            actions.append(("restore", key, target[4]))
    return actions


class ApplyError(Exception):
    """Ошибка посреди --apply: какие ключи уже обработаны."""

    def __init__(self, key: str, done: list[str], total: int, cause: Exception):
        super().__init__(str(cause))
        self.key, self.done, self.total, self.cause = key, done, total, cause

    def __str__(self) -> str:
        lines = [
            f"ключ {self.key}: {self.cause}",
            f"Выполнено {len(self.done)} из {self.total} действий до ошибки.",
        ]
        if self.done:
            lines.append("Уже обработаны: " + ", ".join(self.done))
        lines.append("Запустите ту же команду снова: обработанные ключи будут «без изменений».")
        return "\n".join(lines)


def apply_actions(s3, bucket: str, actions: list[Action], progress=None) -> None:
    """Выполняет restore/delete; progress(line) вызывается после каждого ключа."""
    todo = [a for a in actions if a[0] != "keep"]
    done: list[str] = []
    for i, (kind, key, version_id) in enumerate(todo, 1):
        try:
            if kind == "restore":
                s3.copy_object(
                    Bucket=bucket,
                    Key=key,
                    CopySource={"Bucket": bucket, "Key": key, "VersionId": version_id},
                )
            else:
                s3.delete_object(Bucket=bucket, Key=key)
        except Exception as exc:
            raise ApplyError(key, done, len(todo), exc) from exc
        done.append(key)
        if progress is not None:
            label = "восстановлено" if kind == "restore" else "удалено"
            progress(f"[{i}/{len(todo)}] {label} {key}")


def format_plan(actions: list[Action]) -> str:
    labels = {"restore": "восстановить", "delete": "удалить", "keep": "без изменений"}
    width = max([len(a[1]) for a in actions] + [3])
    lines = [f"{'Действие':<14} {'Ключ':<{width}} Версия"]
    for kind, key, vid in actions:
        lines.append(f"{labels[kind]:<14} {key:<{width}} {vid or '-'}")
    return "\n".join(lines)


def summary(actions: list[Action]) -> str:
    n = {k: sum(1 for a in actions if a[0] == k) for k in ("restore", "delete", "keep")}
    return f"восстановить {n['restore']}, удалить {n['delete']}, без изменений {n['keep']}"


def make_client(profile: str | None, region: str):
    import boto3

    return boto3.Session(profile_name=profile, region_name=region).client("s3")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Откат S3-бакета с версионированием на момент времени.")
    p.add_argument("--bucket", required=True, help="имя бакета")
    p.add_argument("--prefix", default="aichat/", help="префикс ключей (по умолчанию aichat/)")
    p.add_argument(
        "--as-of",
        required=True,
        dest="as_of",
        help='момент отката, ISO 8601 с поясом: "2026-11-02T10:00:00Z"',
    )
    p.add_argument("--apply", action="store_true", help="выполнить откат (без флага — только план)")
    p.add_argument(
        "--force-old",
        action="store_true",
        dest="force_old",
        help=f"разрешить as_of старше {RETENTION_DAYS} дней (версии могли быть уже удалены)",
    )
    p.add_argument("--profile", default=None, help="профиль AWS CLI владельца")
    p.add_argument("--region", default="eu-central-1")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        as_of = parse_as_of(args.as_of)
    except ValueError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    if as_of < datetime.now(UTC) - timedelta(days=RETENTION_DAYS) and not args.force_old:
        print(
            f"Ошибка: момент {as_of:%Y-%m-%d %H:%M:%S} UTC старше {RETENTION_DAYS} дней. "
            f"Lifecycle бакета удаляет старые версии через {RETENTION_DAYS} дней, поэтому "
            "состояние на этот момент восстановится не полностью. Если вы понимаете это, "
            "добавьте --force-old.",
            file=sys.stderr,
        )
        return 2
    if not args.prefix:
        print("Внимание: префикс пуст — откатывается весь бакет.", file=sys.stderr)
    elif not args.prefix.endswith("/"):
        print(
            f"Внимание: префикс «{args.prefix}» не заканчивается на «/» — будут затронуты и "
            f"ключи вроде «{args.prefix}-old/…». Обычно нужен «{args.prefix}/».",
            file=sys.stderr,
        )
    try:
        s3 = make_client(args.profile, args.region)
        check_versioning(s3, args.bucket)
        actions = plan_restore(s3, args.bucket, args.prefix, as_of)
        print(format_plan(actions) if actions else "Объектов под этим префиксом нет.")
        print(summary(actions))
        if args.apply:
            apply_actions(s3, args.bucket, actions, progress=lambda line: print(line, flush=True))
            print("Откат выполнен.")
        else:
            print("Это только план. Для выполнения добавьте --apply.")
    except Refusal as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
