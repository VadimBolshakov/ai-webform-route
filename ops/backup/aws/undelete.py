#!/usr/bin/env python3
"""Roll a versioned S3 bucket (restic repository) back to a point in time.

Run by the owner from their own computer, with the owner's account (not the server key):
    python undelete.py --bucket B [--prefix aichat/] --as-of "2026-11-02T10:00:00Z" [--apply]

Without --apply only the plan is printed. The state of a key at as_of is the most recent item
(object version or delete marker) with LastModified <= as_of:
  object version -> restore (if the current version differs from it by ETag and size),
  delete marker or nothing -> the key did not exist.
So a repeated run after --apply shows that there is nothing left to do.
The bucket lifecycle deletes old versions after 30 days: an earlier as_of only with --force-old.
Exit codes: 0 - success, 2 - refusal (no versioning / no permissions / as_of older than 30 days),
1 - other errors.
Dependency: boto3 (owner side only; the application does not use it).
"""

import argparse
import sys
from datetime import UTC, datetime, timedelta

# Page size of list_object_versions (overridden in tests).
PAGE_SIZE = 1000

# The lifecycle deletes noncurrent versions after this many days (aws/lifecycle.json).
RETENTION_DAYS = 30

# How many processed keys to list in the --apply error message.
DONE_KEYS_SHOWN = 20

# Action = (kind: "restore" | "delete" | "keep", key, version_id | None)
Action = tuple[str, str, str | None]


class Refusal(Exception):
    """Refusal to proceed (exit code 2)."""


def parse_as_of(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(
            f"Could not parse the time '{value}': ISO 8601 format is required, "
            "for example 2026-11-02T10:00:00Z"
        ) from None
    if dt.tzinfo is None:
        raise ValueError(
            f"The time '{value}' has no timezone: add Z (UTC) or an offset, "
            "for example 2026-11-02T10:00:00Z or 2026-11-02T12:00:00+02:00"
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
                "No permission to read versions: run with the owner's account, "
                "not the server key"
            ) from exc
        raise
    if status != "Enabled":
        raise Refusal("Bucket versioning is not enabled - rollback is impossible")


def _iter_entries(s3, bucket: str, prefix: str):
    """All versions and delete markers under prefix:
    (key, is_marker, last_modified, is_latest, vid, pos, etag, size); for markers etag and size are None."""
    pos = 0
    kwargs = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": PAGE_SIZE}
    while True:
        try:
            page = s3.list_object_versions(**kwargs)
        except Exception as exc:
            if _error_code(exc) == "AccessDenied":
                raise Refusal(
                    "No permission to read versions: run with the owner's account, "
                    "not the server key"
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
        raise ValueError("as_of must contain a timezone")
    by_key: dict[str, list[tuple]] = {}
    for entry in _iter_entries(s3, bucket, prefix):
        by_key.setdefault(entry[0], []).append(entry)

    actions: list[Action] = []
    for key in sorted(by_key):
        entries = by_key[key]
        current = next((e for e in entries if e[3] and not e[1]), None)
        # With equal LastModified (one-second precision) the current version is newer, then the one that
        # comes earlier in the S3 response (it returns newest first); versions and markers arrive separately.
        at_as_of = [e for e in entries if e[2] <= as_of]
        target = max(at_as_of, key=lambda e: (e[2], e[3], -e[5]), default=None)
        if target is None or target[1]:
            # At as_of the key did not exist (never existed or was deleted).
            actions.append(("delete", key, None) if current else ("keep", key, None))
        elif target[3] or (
            current is not None and (current[6], current[7]) == (target[6], target[7])
        ):
            # The target version is the current one, or the current one is a copy of it (e.g. after --apply).
            actions.append(("keep", key, target[4]))
        else:
            actions.append(("restore", key, target[4]))
    return actions


class ApplyError(Exception):
    """Error in the middle of --apply: which keys are already processed."""

    def __init__(self, key: str, done: list[str], total: int, cause: Exception):
        super().__init__(str(cause))
        self.key, self.done, self.total, self.cause = key, done, total, cause

    def __str__(self) -> str:
        lines = [
            f"key {self.key}: {self.cause}",
            f"Done {len(self.done)} of {self.total} actions before the error.",
        ]
        if self.done:
            shown = ", ".join(self.done[:DONE_KEYS_SHOWN])
            rest = len(self.done) - DONE_KEYS_SHOWN
            lines.append("Already processed: " + shown + (f" and {rest} more" if rest > 0 else ""))
        lines.append("Run the same command again: processed keys will show as 'unchanged'.")
        return "\n".join(lines)


def apply_actions(s3, bucket: str, actions: list[Action], progress=None) -> None:
    """Performs restore/delete; progress(line) is called after each key."""
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
            label = "restored" if kind == "restore" else "deleted"
            progress(f"[{i}/{len(todo)}] {label} {key}")


def format_plan(actions: list[Action]) -> str:
    labels = {"restore": "restore", "delete": "delete", "keep": "unchanged"}
    width = max([len(a[1]) for a in actions] + [3])
    lines = [f"{'Action':<14} {'Key':<{width}} Version"]
    for kind, key, vid in actions:
        lines.append(f"{labels[kind]:<14} {key:<{width}} {vid or '-'}")
    return "\n".join(lines)


def summary(actions: list[Action]) -> str:
    n = {k: sum(1 for a in actions if a[0] == k) for k in ("restore", "delete", "keep")}
    return f"restore {n['restore']}, delete {n['delete']}, unchanged {n['keep']}"


def make_client(profile: str | None, region: str):
    import boto3

    return boto3.Session(profile_name=profile, region_name=region).client("s3")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Roll a versioned S3 bucket back to a point in time.")
    p.add_argument("--bucket", required=True, help="bucket name")
    p.add_argument("--prefix", default="aichat/", help="key prefix (default aichat/)")
    p.add_argument(
        "--as-of",
        required=True,
        dest="as_of",
        help='rollback moment, ISO 8601 with a timezone: "2026-11-02T10:00:00Z"',
    )
    p.add_argument("--apply", action="store_true", help="perform the rollback (without the flag - plan only)")
    p.add_argument(
        "--force-old",
        action="store_true",
        dest="force_old",
        help=f"allow as_of older than {RETENTION_DAYS} days (versions may already be deleted)",
    )
    p.add_argument("--profile", default=None, help="owner's AWS CLI profile")
    p.add_argument("--region", default="eu-central-1")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        as_of = parse_as_of(args.as_of)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if as_of < datetime.now(UTC) - timedelta(days=RETENTION_DAYS) and not args.force_old:
        print(
            f"Error: the moment {as_of.astimezone(UTC):%Y-%m-%d %H:%M:%S} UTC is older than {RETENTION_DAYS} days. "
            f"The bucket lifecycle deletes old versions after {RETENTION_DAYS} days, so "
            "the state at that moment will not be fully restored. If you understand this, "
            "add --force-old.",
            file=sys.stderr,
        )
        return 2
    if not args.prefix:
        print("Warning: the prefix is empty - the whole bucket will be rolled back.", file=sys.stderr)
    elif not args.prefix.endswith("/"):
        print(
            f"Warning: the prefix '{args.prefix}' does not end with '/' - keys like "
            f"'{args.prefix}-old/...' will be affected too. Usually '{args.prefix}/' is intended.",
            file=sys.stderr,
        )
    try:
        s3 = make_client(args.profile, args.region)
        check_versioning(s3, args.bucket)
        actions = plan_restore(s3, args.bucket, args.prefix, as_of)
        print(format_plan(actions) if actions else "No objects under this prefix.")
        print(summary(actions))
        if args.apply:
            apply_actions(s3, args.bucket, actions, progress=lambda line: print(line, flush=True))
            print("Rollback done.")
        else:
            print("This is only a plan. Add --apply to execute it.")
    except Refusal as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
