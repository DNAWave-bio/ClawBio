"""Offline tests for the allow-listed S3 client.

No test here constructs a real boto3 client, reads a credential, or touches
the network. Every call substitutes a recording fake through the S3Client
protocol.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
# sys.path injection lives in tests/conftest.py.

import s3_client as s3  # noqa: E402


class FakeS3:
    """Records method calls; returns canned botocore-shaped responses."""

    def __init__(self, **responses: Any) -> None:
        self.responses = responses
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        def method(*args: Any, **kwargs: Any):
            self.calls.append((name, args, kwargs))
            if name not in self.responses:
                raise AssertionError(f"Unexpected S3 method: {name}")
            value = self.responses[name]
            return value(*args, **kwargs) if callable(value) else value

        return method


# ---------------------------------------------------------------------------
# The allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method",
    ["delete_object", "delete_objects", "delete_bucket", "create_bucket",
     "put_bucket_policy", "put_object_acl", "put_bucket_acl"],
)
def test_destructive_and_permission_methods_are_refused(method):
    """Refusal happens before dispatch, so a refused method never reaches AWS."""
    client = s3.S3Operations(_boto=object())  # never used; refusal is first
    with pytest.raises(s3.S3MethodNotAllowed) as exc:
        client.call(method)
    assert method in str(exc.value)


def test_allowlist_is_exactly_what_this_skill_uses():
    assert s3.ALLOWED_S3_METHODS == {
        "list_objects_v2", "upload_file", "download_file",
    }
    assert not any(m.startswith(("delete_", "create_bucket", "put_bucket"))
                   for m in s3.ALLOWED_S3_METHODS)


def test_every_allow_listed_method_is_actually_reachable():
    """An allowlist entry no code path can invoke is not a permission, it is a
    lie about blast radius. Same invariant the omics client enforces.

    Matches on the dispatch site -- `call("<method>"` -- not on a bare mention,
    because the allowlist literal itself would otherwise satisfy the search and
    the test would prove nothing."""
    combined = (
        (SKILL_DIR / "healthomics_bridge.py").read_text(encoding="utf-8")
        + (SKILL_DIR / "s3_client.py").read_text(encoding="utf-8")
    )
    for method in s3.ALLOWED_S3_METHODS:
        assert f'call("{method}"' in combined, (
            f"{method} is allow-listed but nothing dispatches it — wire it up or drop it."
        )


def test_the_allowlist_gates_methods_not_operations_and_says_so():
    """upload_file/download_file are managed transfers that fan out into many
    API operations (CreateMultipartUpload, UploadPart, ...). Gating at method
    level is the honest description; claiming operation-level control here
    would be false."""
    doc = (SKILL_DIR / "s3_client.py").read_text(encoding="utf-8")
    assert "multipart" in doc.lower()
    assert "method" in doc.lower()


# ---------------------------------------------------------------------------
# URI handling
# ---------------------------------------------------------------------------


def test_s3_uri_splits_into_bucket_and_key():
    assert s3.parse_s3_uri("s3://bucket/a/b.txt") == ("bucket", "a/b.txt")
    assert s3.parse_s3_uri("s3://bucket/prefix/") == ("bucket", "prefix/")
    assert s3.parse_s3_uri("s3://bucket") == ("bucket", "")


@pytest.mark.parametrize("bad", ["gs://b/k", "https://example.com/x", "/local/path", ""])
def test_a_non_s3_uri_is_refused(bad):
    """A gs:// or https:// destination means the caller believes this skill
    talks to something it does not."""
    with pytest.raises(ValueError):
        s3.parse_s3_uri(bad)


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_listing_pages_through_every_object():
    pages = [
        {"Contents": [{"Key": f"out/{i}", "Size": i, "ETag": '"abc"'} for i in range(2)],
         "NextContinuationToken": "t2", "IsTruncated": True},
        {"Contents": [{"Key": "out/2", "Size": 2, "ETag": '"def"'}], "IsTruncated": False},
    ]
    seen: list[dict] = []

    class Paging(FakeS3):
        def __getattr__(self, name):
            def method(**kwargs):
                self.calls.append((name, (), kwargs))
                seen.append(kwargs)
                return pages[len(seen) - 1]
            return method

    client = s3.S3Operations(_boto=Paging())
    objects = s3.list_objects(client=client, uri="s3://bucket/out/")

    assert [o["key"] for o in objects] == ["out/0", "out/1", "out/2"]
    assert seen[1].get("ContinuationToken") == "t2", "must follow the continuation token"


def test_listing_strips_the_quotes_s3_wraps_around_etags():
    client = s3.S3Operations(_boto=FakeS3(
        list_objects_v2={"Contents": [{"Key": "k", "Size": 1, "ETag": '"deadbeef"'}],
                         "IsTruncated": False}))
    assert s3.list_objects(client=client, uri="s3://b/k")[0]["etag"] == "deadbeef"


def test_a_multipart_etag_is_flagged_as_not_a_checksum():
    """An S3 ETag is an MD5 only for single-part uploads. For multipart it is
    <md5-of-part-md5s>-<n> and hashes nothing a caller can recompute. Genomic
    outputs are routinely multipart, so mislabelling this as a checksum would
    put a false guarantee in the repro bundle."""
    single = s3.describe_etag("deadbeef")
    multi = s3.describe_etag("deadbeef-12")

    assert single["is_md5"] is True
    assert multi["is_md5"] is False
    assert multi["parts"] == 12
    assert "not" in multi["note"].lower()


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------


def test_upload_sends_each_source_to_the_destination_prefix(tmp_path):
    a = tmp_path / "a.txt"; a.write_text("x")
    b = tmp_path / "b.txt"; b.write_text("y")
    fake = FakeS3(upload_file=None)
    client = s3.S3Operations(_boto=fake)

    result = s3.upload_files(client=client, sources=[a, b], destination="s3://bucket/in/")

    keys = [c[2]["Key"] for c in fake.calls]
    assert keys == ["in/a.txt", "in/b.txt"]
    assert result["n_uploaded"] == 2
    assert result["uris"] == ["s3://bucket/in/a.txt", "s3://bucket/in/b.txt"]


def test_upload_refuses_a_missing_source_before_any_transfer(tmp_path):
    fake = FakeS3(upload_file=None)
    client = s3.S3Operations(_boto=fake)
    with pytest.raises(FileNotFoundError):
        s3.upload_files(client=client, sources=[tmp_path / "nope.txt"],
                        destination="s3://bucket/in/")
    assert fake.calls == [], "nothing may transfer once one source is missing"


def test_download_writes_under_the_destination_preserving_relative_layout(tmp_path):
    objects = [{"key": "out/7/a.txt", "size": 1, "etag": "e1"},
               {"key": "out/7/logs/b.txt", "size": 2, "etag": "e2"}]
    written: list[Path] = []

    def _download(Bucket, Key, Filename):  # noqa: N803 - botocore's own casing
        Path(Filename).parent.mkdir(parents=True, exist_ok=True)
        Path(Filename).write_text("data")
        written.append(Path(Filename))

    client = s3.S3Operations(_boto=FakeS3(download_file=_download))
    result = s3.download_objects(client=client, bucket="bucket", objects=objects,
                                 key_prefix="out/7/", destination=tmp_path)

    assert sorted(p.relative_to(tmp_path).as_posix() for p in written) == [
        "a.txt", "logs/b.txt",
    ]
    assert result["n_downloaded"] == 2


def test_download_reports_a_missing_object_rather_than_skipping_it(tmp_path):
    """clawbio.common.reproducibility.write_checksums silently skips files that
    do not exist, so it cannot detect an output that never landed. Deep
    verification has to notice that itself."""
    objects = [{"key": "out/a.txt", "size": 1, "etag": "e1"},
               {"key": "out/gone.txt", "size": 2, "etag": "e2"}]

    def _download(Bucket, Key, Filename):  # noqa: N803
        if Key.endswith("gone.txt"):
            raise RuntimeError("NoSuchKey")
        Path(Filename).parent.mkdir(parents=True, exist_ok=True)
        Path(Filename).write_text("data")

    client = s3.S3Operations(_boto=FakeS3(download_file=_download))
    result = s3.download_objects(client=client, bucket="bucket", objects=objects,
                                 key_prefix="out/", destination=tmp_path)

    assert result["n_downloaded"] == 1
    assert len(result["failures"]) == 1
    assert "gone.txt" in result["failures"][0]["key"]


def test_directory_markers_are_not_reported_as_objects():
    """S3 has no directories; the console and some SDKs fake them with
    zero-byte keys ending in '/'. A live deep verification counted one of those
    as an output that failed to download, so a complete run reported itself
    incomplete. They are not outputs and must not be listed as any."""
    client = s3.S3Operations(_boto=FakeS3(list_objects_v2={"Contents": [
        {"Key": "out/7/", "Size": 0, "ETag": '"d41d8cd98f00b204e9800998ecf8427e"'},
        {"Key": "out/7/real.txt", "Size": 5, "ETag": '"abc"'},
    ]}))
    objects = s3.list_objects(client=client, uri="s3://bucket/out/7/")
    assert [o["key"] for o in objects] == ["out/7/real.txt"]


def test_a_zero_byte_real_file_is_still_listed():
    """Only a trailing slash marks a directory. An empty output file is a
    legitimate result -- often a meaningful one -- and must survive."""
    client = s3.S3Operations(_boto=FakeS3(list_objects_v2={"Contents": [
        {"Key": "out/7/empty.vcf", "Size": 0, "ETag": '"d41d8"'},
    ]}))
    assert [o["key"] for o in s3.list_objects(client=client, uri="s3://b/out/7/")] == [
        "out/7/empty.vcf"]
