"""Откат бакета на момент времени (ops/backup/aws/undelete.py) на moto, без Docker."""

import importlib.util
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

AWS_DIR = Path(__file__).resolve().parents[1] / "ops" / "backup" / "aws"
BUCKET = "test-bucket"

_spec = importlib.util.spec_from_file_location("undelete", AWS_DIR / "undelete.py")
undelete = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(undelete)


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        client = boto3.client("s3", region_name="eu-central-1")
        client.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )
        client.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        yield client


def _tick() -> datetime:
    """Момент между «до» и «после»: LastModified в S3 с точностью до секунды."""
    time.sleep(1.1)
    t = datetime.now(UTC)
    time.sleep(1.1)
    return t


def _body(s3, key):
    return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()


def _kinds(actions):
    return {a[1]: a[0] for a in actions}


def _live_keys(s3):
    return sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", []))


def test_restores_deleted_objects(s3):
    s3.put_object(Bucket=BUCKET, Key="aichat/a", Body=b"a1")
    s3.put_object(Bucket=BUCKET, Key="aichat/b", Body=b"b1")
    as_of = _tick()
    s3.delete_object(Bucket=BUCKET, Key="aichat/a")
    s3.put_object(Bucket=BUCKET, Key="aichat/b", Body=b"b2")

    actions = undelete.plan_restore(s3, BUCKET, "aichat/", as_of)
    assert _kinds(actions) == {"aichat/a": "restore", "aichat/b": "restore"}
    undelete.apply_actions(s3, BUCKET, actions)
    assert _body(s3, "aichat/a") == b"a1"
    assert _body(s3, "aichat/b") == b"b1"


def test_deletes_objects_created_after(s3):
    s3.put_object(Bucket=BUCKET, Key="aichat/a", Body=b"a1")
    as_of = _tick()
    s3.put_object(Bucket=BUCKET, Key="aichat/c", Body=b"c1")

    actions = undelete.plan_restore(s3, BUCKET, "aichat/", as_of)
    assert _kinds(actions) == {"aichat/a": "keep", "aichat/c": "delete"}
    undelete.apply_actions(s3, BUCKET, actions)
    assert _live_keys(s3) == ["aichat/a"]


def test_keeps_untouched(s3):
    s3.put_object(Bucket=BUCKET, Key="aichat/a", Body=b"a1")
    s3.put_object(Bucket=BUCKET, Key="aichat/b", Body=b"b1")
    as_of = _tick()

    actions = undelete.plan_restore(s3, BUCKET, "aichat/", as_of)
    assert {a[0] for a in actions} == {"keep"}
    before = [v["VersionId"] for v in s3.list_object_versions(Bucket=BUCKET)["Versions"]]
    undelete.apply_actions(s3, BUCKET, actions)
    after = [v["VersionId"] for v in s3.list_object_versions(Bucket=BUCKET)["Versions"]]
    assert before == after


def test_already_deleted_at_as_of_is_kept(s3):
    s3.put_object(Bucket=BUCKET, Key="aichat/gone", Body=b"x")
    s3.delete_object(Bucket=BUCKET, Key="aichat/gone")
    as_of = _tick()
    actions = undelete.plan_restore(s3, BUCKET, "aichat/", as_of)
    assert _kinds(actions) == {"aichat/gone": "keep"}


def test_deleted_at_as_of_but_recreated_later_is_deleted(s3):
    s3.put_object(Bucket=BUCKET, Key="aichat/k", Body=b"x")
    time.sleep(1.1)  # версия и marker в разные секунды
    s3.delete_object(Bucket=BUCKET, Key="aichat/k")
    as_of = _tick()
    s3.put_object(Bucket=BUCKET, Key="aichat/k", Body=b"y")
    actions = undelete.plan_restore(s3, BUCKET, "aichat/", as_of)
    assert _kinds(actions) == {"aichat/k": "delete"}


def test_respects_prefix(s3):
    s3.put_object(Bucket=BUCKET, Key="aichat/a", Body=b"a1")
    s3.put_object(Bucket=BUCKET, Key="other/a", Body=b"o1")
    as_of = _tick()
    s3.delete_object(Bucket=BUCKET, Key="aichat/a")
    s3.delete_object(Bucket=BUCKET, Key="other/a")

    actions = undelete.plan_restore(s3, BUCKET, "aichat/", as_of)
    assert _kinds(actions) == {"aichat/a": "restore"}
    undelete.apply_actions(s3, BUCKET, actions)
    assert _body(s3, "aichat/a") == b"a1"
    assert _live_keys(s3) == ["aichat/a"]


def test_pagination(s3, monkeypatch):
    for i in range(5):
        s3.put_object(Bucket=BUCKET, Key=f"aichat/k{i}", Body=b"v1")
    as_of = _tick()
    for i in range(5):
        s3.put_object(Bucket=BUCKET, Key=f"aichat/k{i}", Body=b"v2")
    monkeypatch.setattr(undelete, "PAGE_SIZE", 3)
    actions = undelete.plan_restore(s3, BUCKET, "aichat/", as_of)
    assert len(actions) == 5 and {a[0] for a in actions} == {"restore"}


def test_undelete_refuses_without_versioning(monkeypatch, capsys):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        c = boto3.client("s3", region_name="eu-central-1")
        c.create_bucket(
            Bucket="plain", CreateBucketConfiguration={"LocationConstraint": "eu-central-1"}
        )
        code = undelete.main(["--bucket", "plain", "--as-of", "2026-01-01T00:00:00Z"])
    assert code == 2
    assert "Версионирование бакета не включено — откат невозможно" in capsys.readouterr().err


def test_access_denied_exits_2(s3, monkeypatch, capsys):
    real = s3

    class Denying:
        def get_bucket_versioning(self, **kw):
            return real.get_bucket_versioning(**kw)

        def list_object_versions(self, **kw):
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "no"}}, "ListObjectVersions"
            )

    monkeypatch.setattr(undelete, "make_client", lambda profile, region: Denying())
    code = undelete.main(["--bucket", BUCKET, "--as-of", "2026-01-01T00:00:00Z"])
    assert code == 2
    assert "Нет прав на чтение версий" in capsys.readouterr().err


def test_naive_as_of_rejected(s3, capsys):
    code = undelete.main(["--bucket", BUCKET, "--as-of", "2026-01-01T00:00:00"])
    assert code == 1
    assert "часовой пояс" in capsys.readouterr().err


def test_dry_run_changes_nothing(s3, capsys):
    s3.put_object(Bucket=BUCKET, Key="aichat/a", Body=b"a1")
    s3.put_object(Bucket=BUCKET, Key="aichat/b", Body=b"b1")
    as_of = _tick()
    s3.delete_object(Bucket=BUCKET, Key="aichat/a")
    s3.put_object(Bucket=BUCKET, Key="aichat/c", Body=b"c1")
    before = s3.list_object_versions(Bucket=BUCKET)

    code = undelete.main(["--bucket", BUCKET, "--prefix", "aichat/", "--as-of", as_of.isoformat()])
    out = capsys.readouterr().out
    assert code == 0
    assert "восстановить 1, удалить 1, без изменений 1" in out
    assert "aichat/a" in out and "aichat/c" in out
    after = s3.list_object_versions(Bucket=BUCKET)
    assert len(before["Versions"]) == len(after["Versions"])
    assert len(before.get("DeleteMarkers", [])) == len(after.get("DeleteMarkers", []))


def test_apply_via_main(s3):
    s3.put_object(Bucket=BUCKET, Key="aichat/a", Body=b"a1")
    as_of = _tick()
    s3.delete_object(Bucket=BUCKET, Key="aichat/a")
    code = undelete.main(
        ["--bucket", BUCKET, "--as-of", as_of.isoformat().replace("+00:00", "Z"), "--apply"]
    )
    assert code == 0
    assert _body(s3, "aichat/a") == b"a1"


def _as_list(v):
    return v if isinstance(v, list) else [v]


def test_policy_files_valid():
    pol = json.loads((AWS_DIR / "iam-policy.json").read_text(encoding="utf-8"))
    allow, deny = set(), set()
    for st in pol["Statement"]:
        acts = _as_list(st["Action"])
        assert not any(a == "*" or a.endswith("*") for a in acts)
        assert all(r.startswith("arn:aws:s3:::BUCKET_NAME") for r in _as_list(st["Resource"]))
        (allow if st["Effect"] == "Allow" else deny).update(acts)
    bucket_acts = {"s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads"}
    object_acts = {
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts",
    }
    assert allow == bucket_acts | object_acts
    assert deny == {
        "s3:DeleteObjectVersion",
        "s3:PutBucketVersioning",
        "s3:PutLifecycleConfiguration",
        "s3:PutBucketPolicy",
        "s3:DeleteBucketPolicy",
        "s3:PutBucketObjectLockConfiguration",
        "s3:DeleteBucket",
    }
    for st in pol["Statement"]:
        if st["Effect"] == "Allow":
            res = _as_list(st["Resource"])
            if set(_as_list(st["Action"])) == bucket_acts:
                assert res == ["arn:aws:s3:::BUCKET_NAME"]
            else:
                assert set(_as_list(st["Action"])) == object_acts
                assert res == ["arn:aws:s3:::BUCKET_NAME/*"]
        else:
            assert set(_as_list(st["Resource"])) == {
                "arn:aws:s3:::BUCKET_NAME",
                "arn:aws:s3:::BUCKET_NAME/*",
            }

    rules = json.loads((AWS_DIR / "lifecycle.json").read_text(encoding="utf-8"))["Rules"]
    assert len(rules) == 3
    assert all(r["Filter"] == {} and r["Status"] == "Enabled" for r in rules)
    assert any(r.get("NoncurrentVersionExpiration") == {"NoncurrentDays": 30} for r in rules)
    assert any(r.get("AbortIncompleteMultipartUpload") == {"DaysAfterInitiation": 7} for r in rules)
    assert [r["Expiration"] for r in rules if "Expiration" in r] == [
        {"ExpiredObjectDeleteMarker": True}
    ]
