"""Offline tests for workflow registration (WDL / CWL / Nextflow).

No test constructs a real boto3 client or reaches AWS.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
# sys.path injection lives in tests/conftest.py.

import registration as reg  # noqa: E402


class FakeOmics:
    def __init__(self, **responses: Any) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((operation, kwargs))
        if operation not in self.responses:
            raise AssertionError(f"Unexpected operation: {operation}")
        value = self.responses[operation]
        return value(**kwargs) if callable(value) else value


# ---------------------------------------------------------------------------
# Engine inference
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,engine",
    [("main.wdl", "WDL"), ("pipeline.cwl", "CWL"), ("main.nf", "NEXTFLOW")],
)
def test_engine_is_inferred_from_the_extension(name, engine):
    assert reg.infer_engine(Path(name)) == engine


def test_an_unknown_extension_is_refused_rather_than_guessed():
    """Guessing WDL for a .txt produces an AWS-side failure whose message says
    nothing about the real problem."""
    with pytest.raises(ValueError) as exc:
        reg.infer_engine(Path("pipeline.txt"))
    assert "--engine" in str(exc.value)


def test_an_explicit_engine_overrides_inference():
    assert reg.resolve_engine(Path("main.wdl"), "WDL_LENIENT") == "WDL_LENIENT"
    assert reg.resolve_engine(Path("main.wdl"), None) == "WDL"


def test_only_engines_aws_accepts_are_allowed():
    """Verified against botocore's own service model for CreateWorkflow."""
    assert reg.SUPPORTED_ENGINES == {"WDL", "CWL", "NEXTFLOW", "WDL_LENIENT"}
    with pytest.raises(ValueError):
        reg.resolve_engine(Path("main.wdl"), "SNAKEMAKE")


# ---------------------------------------------------------------------------
# The definition archive
# ---------------------------------------------------------------------------


def _wdl(tmp_path: Path) -> Path:
    p = tmp_path / "main.wdl"
    p.write_text("version 1.0\nworkflow W { }\n")
    return p


def test_the_zip_is_byte_identical_across_builds(tmp_path):
    """The archive's sha256 is the one piece of remote state this skill can
    pin honestly -- it built those bytes itself. That only means something if
    the same inputs always produce the same bytes, so mtimes, platform
    attributes and compression variance are all pinned out."""
    main = _wdl(tmp_path)
    first = reg.build_definition_zip(
        members=reg.resolve_zip_members(main, []), destination=tmp_path / "a.zip")
    second = reg.build_definition_zip(
        members=reg.resolve_zip_members(main, []), destination=tmp_path / "b.zip")

    assert first["sha256"] == second["sha256"]
    assert (tmp_path / "a.zip").read_bytes() == (tmp_path / "b.zip").read_bytes()


def test_the_zip_pins_timestamp_mode_and_platform(tmp_path):
    main = _wdl(tmp_path)
    reg.build_definition_zip(
        members=reg.resolve_zip_members(main, []), destination=tmp_path / "d.zip")

    with zipfile.ZipFile(tmp_path / "d.zip") as zf:
        info = zf.infolist()[0]
        assert info.date_time == reg._ZIP_EPOCH
        assert info.create_system == 3, "unix, regardless of build platform"
        assert info.compress_type == zipfile.ZIP_STORED


def test_additional_files_keep_their_path_relative_to_the_definition(tmp_path):
    """A WDL that does `import \"tasks/qc.wdl\"` only resolves after AWS unpacks
    if the archive preserves that relative layout."""
    main = _wdl(tmp_path)
    tasks = tmp_path / "tasks"; tasks.mkdir()
    qc = tasks / "qc.wdl"; qc.write_text("task qc { }\n")

    members = reg.resolve_zip_members(main, [qc])
    assert [name for name, _ in members] == ["main.wdl", "tasks/qc.wdl"]


def test_a_shadowed_member_is_refused(tmp_path):
    """Two sources landing on one archive name is a run that fails inside AWS
    for a reason invisible on this machine.

    The collision only arises for a file from OUTSIDE the definition's own
    directory, which enters at its bare basename. A same-named file *under* the
    definition's tree keeps its relative path and does not collide -- covered by
    test_additional_files_keep_their_path_relative_to_the_definition."""
    workflow_dir = tmp_path / "wf"; workflow_dir.mkdir()
    main = workflow_dir / "main.wdl"; main.write_text("version 1.0\n")

    outside = tmp_path / "elsewhere"; outside.mkdir()
    clash = outside / "main.wdl"; clash.write_text("different\n")

    with pytest.raises(ValueError) as exc:
        reg.resolve_zip_members(main, [clash])
    assert "main.wdl" in str(exc.value)


def test_an_oversized_archive_is_refused_before_it_is_written(tmp_path):
    main = _wdl(tmp_path)
    big = tmp_path / "big.wdl"
    big.write_text("x" * (reg._MAX_DEFINITION_ZIP_BYTES + 1))

    with pytest.raises(ValueError) as exc:
        reg.build_definition_zip(
            members=reg.resolve_zip_members(main, [big]), destination=tmp_path / "o.zip")
    assert "definitionUri" in str(exc.value) or "4" in str(exc.value)


# ---------------------------------------------------------------------------
# Name collisions -- AWS does not enforce uniqueness
# ---------------------------------------------------------------------------


def test_a_duplicate_workflow_name_is_refused_by_default():
    """HealthOmics happily creates a second workflow with the same name and a
    different id. Silently forking is worse than refusing."""
    client = FakeOmics(ListWorkflows={
        "items": [{"id": "111", "name": "my-wf", "status": "ACTIVE"}]})
    with pytest.raises(ValueError) as exc:
        reg.assert_workflow_name_is_free(client=client, name="my-wf", allow_duplicate=False)
    assert "--allow-duplicate-name" in str(exc.value)


def test_a_duplicate_name_is_permitted_with_the_override():
    client = FakeOmics(ListWorkflows={
        "items": [{"id": "111", "name": "my-wf", "status": "ACTIVE"}]})
    matches = reg.assert_workflow_name_is_free(
        client=client, name="my-wf", allow_duplicate=True)
    assert [m["id"] for m in matches] == ["111"]


def test_no_collision_is_never_reported_from_a_partial_read():
    """If the listing truncates before the whole account is read, 'no collision
    found' is an unproven claim -- so it must not be made."""
    pages = [{"items": [{"id": str(i), "name": "other"} for i in range(100)],
              "nextToken": "more"}] * 20

    class Truncating(FakeOmics):
        def __init__(self):
            super().__init__()
            self.n = 0

        def call(self, operation, **kwargs):
            self.calls.append((operation, kwargs))
            self.n += 1
            return pages[0]

    matches, truncated = reg.find_workflows_named(client=Truncating(), name="my-wf")
    assert matches == []
    assert truncated is True

    with pytest.raises(ValueError) as exc:
        reg.assert_workflow_name_is_free(
            client=Truncating(), name="my-wf", allow_duplicate=False)
    assert "could not be checked" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Creating, and what happens after
# ---------------------------------------------------------------------------


def test_the_create_request_uses_aws_field_names(tmp_path):
    main = _wdl(tmp_path)
    manifest = reg.build_definition_zip(
        members=reg.resolve_zip_members(main, []), destination=tmp_path / "d.zip")

    request = reg.build_create_workflow_request(
        name="my-wf", engine="WDL", zip_path=Path(manifest["path"]),
        request_id="tok", description="d", parameter_template={"g": {"description": "x"}})

    assert request["engine"] == "WDL"
    assert request["name"] == "my-wf"
    assert request["requestId"] == "tok"
    assert isinstance(request["definitionZip"], bytes)
    assert "parameterTemplate" in request


def test_registration_polls_until_the_workflow_settles():
    """The id is useless until the workflow is ACTIVE, and waiting costs
    nothing -- so unlike a billable run this needs no opt-in flag."""
    statuses = ["CREATING", "CREATING", "ACTIVE"]

    class Polling(FakeOmics):
        def __init__(self):
            super().__init__()
            self.n = 0

        def call(self, operation, **kwargs):
            self.calls.append((operation, kwargs))
            s = statuses[min(self.n, len(statuses) - 1)]
            self.n += 1
            return {"id": "999", "status": s, "type": "PRIVATE"}

    workflow = reg.poll_workflow_until_settled(
        client=Polling(), workflow_id="999", poll_seconds=0, timeout_seconds=60)
    assert workflow["status"] == "ACTIVE"


def test_a_failed_registration_settles_rather_than_hanging():
    """AWS validates the definition server-side; there is no lint API to catch
    it first. FAILED is a terminal answer, not something to keep polling."""
    client = FakeOmics(GetWorkflow={"id": "9", "status": "FAILED"})
    workflow = reg.poll_workflow_until_settled(
        client=client, workflow_id="9", poll_seconds=0, timeout_seconds=60)
    assert workflow["status"] == "FAILED"


def test_polling_gives_up_without_raising():
    """A workflow still CREATING after the ceiling is a warning, not a crash --
    the resource exists either way and the caller needs its id."""
    client = FakeOmics(GetWorkflow={"id": "9", "status": "CREATING"})
    workflow = reg.poll_workflow_until_settled(
        client=client, workflow_id="9", poll_seconds=0, timeout_seconds=0)
    assert workflow["status"] == "CREATING"
