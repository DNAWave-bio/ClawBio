"""Live tests against the real AWS HealthOmics API through boto3.

Double-gated and never run by default: they need boto3 installed, real AWS
credentials, and an explicit opt-in. They exist to verify the two things no
offline test can reach — that AWS's own response shapes still match what this
skill maps, and that ``StartRun``'s parameter contract has not drifted.

    CLAWBIO_RUN_LIVE_HEALTHOMICS=1 uv run --with boto3 pytest \\
        skills/healthomics-bridge/tests/test_live_integration.py -v

Every test here is read-only. Nothing in this file submits a run or creates a
resource, so running the whole file costs nothing. The one billable path
(``--start-run --confirm-submit``) is deliberately exercised by hand, not by a
test that could fire in a loop.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_DIR / "healthomics_bridge.py"
# sys.path injection lives in tests/conftest.py.

import healthomics_bridge as bridge  # noqa: E402
from omics_client import ALLOWED_OPERATIONS, build_boto_client  # noqa: E402


def _require_live_environment():
    if os.environ.get("CLAWBIO_RUN_LIVE_HEALTHOMICS") != "1":
        pytest.skip("Set CLAWBIO_RUN_LIVE_HEALTHOMICS=1 to run the live HealthOmics tests.")
    try:
        import boto3  # noqa: F401
    except ImportError:
        pytest.skip("boto3 is not installed; live HealthOmics calls cannot be made.")
    return build_boto_client(region=os.environ.get("AWS_REGION", "us-east-1"))


@pytest.mark.integration
@pytest.mark.network
def test_every_allow_listed_operation_exists_in_the_service_model():
    """Schema drift detector, and the cheapest test in the file — botocore ships
    the HealthOmics service model locally, so this needs no credentials and no
    network. If AWS renames or removes an operation this skill calls, the skill
    breaks at runtime; this fails at test time instead."""
    _require_live_environment()
    client = build_boto_client(region=os.environ.get("AWS_REGION", "us-east-1"))

    available = set(client.meta.service_model.operation_names)
    missing = ALLOWED_OPERATIONS - available
    assert not missing, f"allow-listed operations absent from the service model: {sorted(missing)}"


@pytest.mark.integration
@pytest.mark.network
def test_start_run_required_members_are_all_supplied():
    """Read StartRun's contract from botocore itself rather than trusting the
    docs: every member AWS marks required must be a key this skill emits, and
    every key it emits must be one the shape accepts."""
    client = _require_live_environment()

    shape = client.meta.service_model.operation_model("StartRun").input_shape
    required = set(shape.required_members)

    request = bridge.build_start_run_request(
        workflow_id="1", workflow_type="PRIVATE", params={}, output_uri="s3://b/o/",
        role_arn="arn:aws:iam::000000000000:role/r", run_name="n", request_id="t",
    )
    missing = required - set(request)
    assert not missing, f"StartRun requires members this skill never sends: {sorted(missing)}"

    # And nothing this skill sends may be absent from the shape, or AWS rejects
    # the whole call with a ParamValidationError before it does any work.
    unknown = set(request) - set(shape.members)
    assert not unknown, f"skill sends members StartRun does not accept: {sorted(unknown)}"


@pytest.mark.integration
@pytest.mark.network
def test_workflow_type_is_a_real_start_run_parameter():
    """Ready2Run submission rests on workflowType being a real StartRun
    parameter accepting READY2RUN. Assert it against AWS's own model rather
    than against the documentation."""
    client = _require_live_environment()

    shape = client.meta.service_model.operation_model("StartRun").input_shape
    assert "workflowType" in shape.members
    assert "READY2RUN" in shape.members["workflowType"].enum


@pytest.mark.integration
@pytest.mark.network
@pytest.mark.slow
def test_list_ready2run_workflows_against_the_live_api(tmp_path: Path):
    """Ready2Run workflows are account-independent, so this returns AWS's own
    catalogue on any account and exercises the real list path end to end."""
    _require_live_environment()

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--list-workflows", "--workflow-type", "READY2RUN",
         "--limit", "5", "--output", str(tmp_path)],
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, f"live listing failed: {result.stderr}"

    envelope = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert envelope["summary"]["demo"] is False
    assert envelope["summary"]["kind"] == "workflows"
    assert envelope["summary"]["n_items"] > 0, "AWS publishes Ready2Run workflows on every account"

    # Every item AWS returned must be the type we asked for, and must reach the
    # table. The live listing is what caught this bundle writing an empty
    # tasks.csv instead of the workflows it had just fetched.
    items = envelope["data"]["items"]
    assert {item["type"] for item in items} == {"READY2RUN"}
    table = (tmp_path / "tables" / "workflows.csv").read_text(encoding="utf-8")
    assert all(item["id"] in table for item in items)


@pytest.mark.integration
@pytest.mark.network
def test_get_workflow_needs_the_type_for_a_ready2run_id():
    """Pins the AWS behaviour that a fixture cannot express, and that an earlier
    draft of this skill got wrong: a Ready2Run workflow id does NOT resolve on
    its own. Without type=READY2RUN the API raises, which is why every live
    Ready2Run run reported its workflow as 'n/a'."""
    client = _require_live_environment()

    with pytest.raises(client.exceptions.ResourceNotFoundException):
        client.get_workflow(id="1830181")

    assert client.get_workflow(id="1830181", type="READY2RUN")["name"]


@pytest.mark.integration
@pytest.mark.network
@pytest.mark.slow
def test_a_ready2run_run_resolves_its_workflow_name(tmp_path: Path):
    """End-to-end guard for the same bug, through the skill rather than the API.
    Skips on an account with no Ready2Run run to look at."""
    _require_live_environment()

    subprocess.run(
        [sys.executable, str(SCRIPT), "--list-runs", "--limit", "20", "--output", str(tmp_path)],
        capture_output=True, text=True, timeout=300, check=True,
    )
    runs = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))["data"]["items"]
    if not runs:
        pytest.skip("no runs on this account to inspect")
    # ListRuns does not return workflowType -- only GetRun does -- so the type
    # cannot be filtered on here. Any run works: whatever its type, its workflow
    # must resolve, and it is the Ready2Run case that used to fail.
    run_id = runs[0]["id"]

    out = tmp_path / "status"
    subprocess.run(
        [sys.executable, str(SCRIPT), "--run-status", str(run_id), "--output", str(out)],
        capture_output=True, text=True, timeout=300, check=True,
    )
    workflow = json.loads((out / "result.json").read_text(encoding="utf-8"))["data"]["workflow"]
    assert workflow.get("name"), "Ready2Run workflow lookup returned nothing; type not passed?"


@pytest.mark.integration
@pytest.mark.network
@pytest.mark.slow
def test_live_output_contains_no_credential_material(tmp_path: Path):
    """A report is an artifact users paste into issues and chats. Whatever else
    a live response carries, none of it may be a secret."""
    _require_live_environment()

    subprocess.run(
        [sys.executable, str(SCRIPT), "--list-runs", "--limit", "5", "--output", str(tmp_path)],
        capture_output=True, text=True, timeout=300, check=True,
    )

    for path in tmp_path.rglob("*"):
        if not path.is_file():
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        for marker in ("aws_secret_access_key", "AWS_SECRET_ACCESS_KEY", "aws_session_token",
                       "AWS_SESSION_TOKEN", "ASIA", "BEGIN PRIVATE KEY"):
            assert marker not in raw, f"{marker} leaked into {path.name}"
