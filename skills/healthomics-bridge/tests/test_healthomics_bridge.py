"""Offline tests for healthomics-bridge (boto3).

No test constructs a real boto3 client, reads credentials, or touches AWS.
Every call test substitutes the OmicsClient protocol.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_DIR / "healthomics_bridge.py"
# sys.path injection lives in tests/conftest.py.

import healthomics_bridge as bridge  # noqa: E402
from omics_client import ALLOWED_OPERATIONS, OperationNotAllowed  # noqa: E402


class FakeOmics:
    """Records calls; returns canned botocore-shaped responses."""

    def __init__(self, **responses: Any) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((operation, kwargs))
        if operation not in self.responses:
            raise AssertionError(f"Unexpected operation: {operation}")
        return self.responses[operation]


# ---------------------------------------------------------------------------
# The allowlist — enforced over botocore operation names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operation",
    ["DeleteRun", "DeleteWorkflow", "CancelRun", "DeleteRunGroup",
     "CreateSequenceStore", "StartReadSetImportJob", "UpdateRunGroup"],
)
def test_destructive_operations_are_refused(operation):
    """Refusal happens before the boto3 call is dispatched, so a refused
    operation never reaches AWS, so the blast radius is a property of the
    module rather than of the caller's discipline."""
    client = bridge.OmicsOperations(_boto=object())  # never used; refusal is first
    with pytest.raises(OperationNotAllowed) as exc:
        client.call(operation)
    assert operation in str(exc.value)


def test_allowlist_covers_only_what_this_skill_uses():
    assert ALLOWED_OPERATIONS == {
        "ListRuns", "GetRun", "ListRunTasks", "GetRunTask",
        "ListWorkflows", "GetWorkflow", "ListTagsForResource",
        "CreateWorkflow", "StartRun",
    }
    assert not any(op.startswith(("Delete", "Cancel", "Update")) for op in ALLOWED_OPERATIONS)


def test_list_modes_tabulate_what_they_listed(tmp_path):
    """A listing's items must reach the machine-readable table, in their own
    shape. Live testing caught --list-workflows writing an empty tasks.csv with
    run-task headers: the five real workflows it had just fetched existed only
    in report.md, and the one CSV in the bundle described a different entity
    entirely. A table nothing filled in is worse than no table -- a downstream
    reader sees a valid header and concludes there were zero results."""
    data = bridge.map_list_report(
        [{"id": "3768383", "name": "GATK-BP fq2bam", "status": "ACTIVE", "type": "READY2RUN"}],
        kind="workflows", region="us-east-1",
    )
    bridge.write_bundle(tmp_path, data, warn_before_overwrite=False)

    assert not (tmp_path / "tables" / "tasks.csv").exists(), (
        "a workflows listing must not emit a run-task table"
    )
    rows = (tmp_path / "tables" / "workflows.csv").read_text(encoding="utf-8").splitlines()
    assert rows[0].startswith("id,name,status,type")
    assert "3768383" in rows[1]
    assert "GATK-BP fq2bam" in rows[1]


def test_run_listing_tabulates_runs_not_tasks(tmp_path):
    data = bridge.map_list_report(
        [{"id": "7654321", "name": "demo", "status": "COMPLETED", "workflowId": "1"}],
        kind="runs", region="us-east-1",
    )
    bridge.write_bundle(tmp_path, data, warn_before_overwrite=False)

    assert not (tmp_path / "tables" / "tasks.csv").exists()
    assert "7654321" in (tmp_path / "tables" / "runs.csv").read_text(encoding="utf-8")


def test_every_allow_listed_operation_is_actually_reachable():
    """An allowlist entry no code path can invoke is not a permission, it is a
    lie about the skill's blast radius. An early draft allow-listed
    ``CreateWorkflow`` with no ``--register`` mode to reach it, and a comment
    claiming a ``--confirm-register`` gate that did not exist; the mode exists
    now, and this test is what keeps the allowlist and the CLI describing the
    same skill."""
    source = (
        (SKILL_DIR / "healthomics_bridge.py").read_text(encoding="utf-8")
        + (SKILL_DIR / "registration.py").read_text(encoding="utf-8")
    )
    for operation in ALLOWED_OPERATIONS:
        assert f'"{operation}"' in source, (
            f"{operation} is allow-listed but no code path calls it — either wire "
            f"it up or drop it from ALLOWED_OPERATIONS."
        )


# ---------------------------------------------------------------------------
# Submission surface: the parameters that decide what gets billed
# ---------------------------------------------------------------------------


def test_ready2run_submission_is_expressible():
    """Ready2Run is roughly half of what HealthOmics offers, and AWS resolves
    those ids only when told the type. workflowType must survive into the
    request or every Ready2Run submission fails as a bare not-found."""
    request = bridge.build_start_run_request(
        workflow_id="1830181", workflow_type="READY2RUN", params={"x": "y"},
        output_uri="s3://b/o/", role_arn="arn:aws:iam::000000000000:role/r",
        run_name="n", request_id="fixed-token",
    )
    assert request["workflowType"] == "READY2RUN"
    assert request["workflowId"] == "1830181"


def test_request_uses_the_api_field_names_verbatim():
    """The request is built in AWS's own camelCase with no translation layer,
    so a casing mismatch between this skill and the API cannot arise. A
    snake_case key would be silently dropped by botocore's validator."""
    request = bridge.build_start_run_request(
        workflow_id="1", workflow_type="PRIVATE", params={}, output_uri="s3://b/o/",
        role_arn="arn:aws:iam::000000000000:role/r", run_name="n",
        request_id="t",
    )
    for camel in ("workflowId", "roleArn", "outputUri", "requestId"):
        assert camel in request
    for snake in ("workflow_id", "role_arn", "output_uri", "request_id"):
        assert snake not in request


def test_run_tags_are_expressible():
    """Tags are how a run is attributed in Cost Explorer. They can only be set
    at submission time -- there is no API to add them to a run afterwards."""
    request = bridge.build_start_run_request(
        workflow_id="1", workflow_type="PRIVATE", params={}, output_uri="s3://b/o/",
        role_arn="arn:aws:iam::000000000000:role/r", run_name="n",
        request_id="t", tags={"team": "genomics"},
    )
    assert request["tags"] == {"team": "genomics"}


def test_request_id_is_stable_for_the_same_submission():
    """requestId is StartRun's idempotency token -- required by the API, and
    easy to supply carelessly. Deriving it from the submission's own content
    means an accidental re-run of the identical command is deduplicated by AWS
    rather than billed twice, which matters for a cost-gated skill."""
    kwargs = dict(
        workflow_id="1", workflow_type="PRIVATE", params={"a": 1},
        output_uri="s3://b/o/", role_arn="arn:aws:iam::000000000000:role/r",
        run_name="n",
    )
    first = bridge.derive_request_id(**kwargs)
    second = bridge.derive_request_id(**kwargs)
    assert first == second
    different = bridge.derive_request_id(**{**kwargs, "run_name": "other"})
    assert different != first


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def test_start_run_without_confirm_never_calls_aws():
    class Exploding:
        def call(self, operation, **kwargs):
            raise AssertionError(f"{operation} reached AWS on an unconfirmed submission")

    result = bridge.submit_run(
        client=Exploding(),
        request={"workflowId": "1", "roleArn": "r", "outputUri": "s3://b/o/"},
        confirmed=False,
    )
    assert result["submitted"] is False


def test_egress_gate_refuses_without_acknowledgement():
    with pytest.raises(bridge.EgressRefused):
        bridge.check_remote_inputs({"in": "s3://bucket/x.fastq"}, "s3://b/o/", False)


def test_egress_gate_passes_with_acknowledgement(capsys):
    bridge.check_remote_inputs({"in": "s3://bucket/x.fastq"}, "s3://b/o/", True)
    assert "s3://bucket/x.fastq" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Errors surface as exceptions, never as data
# ---------------------------------------------------------------------------


def test_a_botocore_error_propagates_rather_than_becoming_data():
    """botocore raises on an API error, so a rejected call cannot be mistaken
    for a successful one returning error-shaped data."""
    class Failing:
        def call(self, operation, **kwargs):
            raise RuntimeError("ResourceNotFoundException: Workflow 0 not found")

    with pytest.raises(RuntimeError, match="ResourceNotFoundException"):
        bridge.fetch_run_bundle(client=Failing(), run_id="0")


# ---------------------------------------------------------------------------
# Reporting and bundle
# ---------------------------------------------------------------------------


def test_run_report_shape():
    client = FakeOmics(
        GetRun={"id": "7", "status": "COMPLETED", "name": "r", "workflowId": "1",
                "workflowType": "READY2RUN", "outputUri": "s3://b/o/"},
        ListRunTasks={"items": [{"taskId": "t1", "name": "A", "status": "COMPLETED"}]},
        GetWorkflow={"id": "1", "name": "wf", "type": "READY2RUN"},
    )
    bundle = bridge.fetch_run_bundle(client=client, run_id="7")
    data = bridge.map_run_report(bundle, region="us-east-1")
    assert data["run_status"] == "COMPLETED"
    assert data["n_tasks"] == 1
    markdown = bridge._report_markdown(data)
    assert "COMPLETED" in markdown


def test_ready2run_workflow_lookup_passes_the_type_from_the_run():
    """GetWorkflow does NOT resolve a Ready2Run id on its own -- it raises
    ResourceNotFoundException unless told type=READY2RUN. An earlier draft here
    assumed the id self-resolved, and a fixture that answered regardless of type
    let that assumption pass while a live run reported the workflow as 'n/a'.

    The run record already carries workflowType, so one lookup still suffices --
    but only because the run tells us the type, not because the id is enough.
    This fake refuses the call the real API refuses."""
    class TypeAwareOmics(FakeOmics):
        def call(self, operation, **kwargs):
            if operation == "GetWorkflow" and kwargs.get("type") != "READY2RUN":
                raise RuntimeError("ResourceNotFoundException: Workflow 1830181 not found")
            return super().call(operation, **kwargs)

    client = TypeAwareOmics(
        GetRun={"id": "7", "status": "COMPLETED", "workflowId": "1830181",
                "workflowType": "READY2RUN"},
        ListRunTasks={"items": []},
        GetWorkflow={"id": "1830181", "name": "ESMFold", "type": "READY2RUN"},
    )
    bundle = bridge.fetch_run_bundle(client=client, run_id="7")

    lookups = [kw for op, kw in client.calls if op == "GetWorkflow"]
    assert len(lookups) == 1, "one lookup, no PRIVATE-then-READY2RUN retry"
    assert lookups[0].get("type") == "READY2RUN"
    assert bundle["workflow"]["name"] == "ESMFold", (
        "the workflow name must survive into the report, not render as n/a"
    )


def test_private_workflow_lookup_passes_its_type_too():
    client = FakeOmics(
        GetRun={"id": "7", "status": "COMPLETED", "workflowId": "1", "workflowType": "PRIVATE"},
        ListRunTasks={"items": []},
        GetWorkflow={"id": "1", "name": "my-wdl", "type": "PRIVATE"},
    )
    bundle = bridge.fetch_run_bundle(client=client, run_id="7")

    lookups = [kw for op, kw in client.calls if op == "GetWorkflow"]
    assert lookups[0].get("type") == "PRIVATE"
    assert bundle["workflow"]["name"] == "my-wdl"


def test_demo_is_offline_and_writes_the_documented_tree(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--demo", "--output", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    for expected in ("report.md", "result.json", "tables/tasks.csv"):
        assert (tmp_path / expected).exists(), f"missing {expected}"


def test_cli_start_run_requires_workflow_type():
    with pytest.raises(SystemExit):
        bridge.main(["--start-run", "1", "--params", "/dev/null",
                     "--output-uri", "s3://b/o/", "--role-arn", "r", "--run-name", "n"])


# ---------------------------------------------------------------------------
# Telling the truth about the outputs, the cost, and how much was listed
# ---------------------------------------------------------------------------


def test_fetch_command_points_at_this_run_not_every_run(tmp_path):
    """The report prints a command users copy verbatim. It used to say
    `aws s3 cp --recursive s3://bucket/output/`, which pulls every run this
    account ever wrote into one directory. HealthOmics writes each run under
    <outputUri>/<runId>/, so the command must name the run."""
    data = bridge.map_run_report(
        {"run": {"id": "7654321", "status": "COMPLETED",
                 "outputUri": "s3://bucket/output/"}, "workflow": {}, "tasks": []},
        region="us-east-1",
    )
    bridge.write_bundle(tmp_path, data, warn_before_overwrite=False)
    report = (tmp_path / "report.md").read_text(encoding="utf-8")

    assert "s3://bucket/output/7654321/" in report
    assert "cp --recursive s3://bucket/output/ " not in report


def test_a_ready2run_estimate_states_the_price(tmp_path):
    """--confirm-submit asks the user to authorise a charge. A cost gate that
    cannot say what the charge is, is ceremony. Ready2Run fees are flat and
    published, so the estimate can name a real number."""
    request = bridge.build_start_run_request(
        workflow_id="1830181", workflow_type="READY2RUN", params={},
        output_uri="s3://b/o/", role_arn="arn:aws:iam::000000000000:role/r",
        run_name="n", request_id="t",
    )
    data = bridge.map_run_report(
        {"run": {}, "workflow": {}, "tasks": []},
        region="us-east-1", start_run_request=request, submitted=False,
    )
    bridge.write_bundle(tmp_path, data, warn_before_overwrite=False)
    report = (tmp_path / "report.md").read_text(encoding="utf-8")

    assert "$0.25" in report, "the snapshotted Ready2Run flat fee must be shown"
    assert "not a live quote" in report, "and must not be presented as a guarantee"


def test_a_private_workflow_estimate_declines_to_invent_a_price(tmp_path):
    """A private workflow bills per-second compute. No static table can price
    it, and a made-up number is worse than none."""
    request = bridge.build_start_run_request(
        workflow_id="9999999", workflow_type="PRIVATE", params={},
        output_uri="s3://b/o/", role_arn="arn:aws:iam::000000000000:role/r",
        run_name="n", request_id="t",
    )
    data = bridge.map_run_report(
        {"run": {}, "workflow": {}, "tasks": []},
        region="us-east-1", start_run_request=request, submitted=False,
    )
    bridge.write_bundle(tmp_path, data, warn_before_overwrite=False)
    report = (tmp_path / "report.md").read_text(encoding="utf-8")

    assert "$" not in report.split("## Submission")[1].split("##")[0]


def test_listing_pages_past_the_hundred_item_api_cap():
    """AWS caps maxResults at 100 and returns a nextToken. Asking for 150 and
    silently receiving 100 is a wrong answer delivered confidently."""
    pages = [
        {"items": [{"id": str(i)} for i in range(100)], "nextToken": "page2"},
        {"items": [{"id": str(i)} for i in range(100, 150)]},
    ]
    calls: list[dict] = []

    class Paging:
        def call(self, operation, **kwargs):
            calls.append(kwargs)
            return pages[len(calls) - 1]

    items = bridge.list_all(client=Paging(), operation="ListRuns", limit=150)

    assert len(items) == 150
    assert calls[0]["maxResults"] == 100, "never ask AWS for more than it allows"
    assert calls[1].get("startingToken") == "page2" or calls[1].get("nextToken") == "page2"


def test_listing_stops_at_the_requested_limit():
    class Paging:
        def call(self, operation, **kwargs):
            return {"items": [{"id": str(i)} for i in range(100)], "nextToken": "more"}

    assert len(bridge.list_all(client=Paging(), operation="ListRuns", limit=25)) == 25


# ---------------------------------------------------------------------------
# Closing the submit -> watch -> verify loop
# ---------------------------------------------------------------------------


def test_wait_polls_until_the_run_reaches_a_terminal_state():
    """Submitting through this skill used to mean dropping to the AWS CLI to
    watch the run it had just started. Watching your own run is not analysis."""
    statuses = ["PENDING", "STARTING", "RUNNING", "COMPLETED"]

    class Polling:
        def __init__(self): self.n = 0
        def call(self, operation, **kwargs):
            if operation == "GetRun":
                s = statuses[min(self.n, len(statuses) - 1)]; self.n += 1
                return {"id": "7", "status": s}
            return {"items": []}

    run = bridge.wait_for_run(client=Polling(), run_id="7", poll_seconds=0, timeout_seconds=60)
    assert run["status"] == "COMPLETED"


def test_wait_gives_up_rather_than_polling_forever():
    class Stuck:
        def call(self, operation, **kwargs):
            return {"id": "7", "status": "RUNNING"}

    with pytest.raises(TimeoutError):
        bridge.wait_for_run(client=Stuck(), run_id="7", poll_seconds=0, timeout_seconds=0)


def test_run_report_reads_back_the_tags_it_set(tmp_path):
    """This skill's headline capability is setting run tags. It could not read
    them back, so verifying its own work needed the AWS CLI."""
    client = FakeOmics(
        GetRun={"id": "7", "status": "COMPLETED", "arn": "arn:aws:omics:us-east-1:0:run/7"},
        ListRunTasks={"items": []},
        ListTagsForResource={"tags": {"team": "genomics"}},
    )
    bundle = bridge.fetch_run_bundle(client=client, run_id="7")
    assert bundle["tags"] == {"team": "genomics"}

    data = bridge.map_run_report(bundle, region="us-east-1")
    bridge.write_bundle(tmp_path, data, warn_before_overwrite=False)
    assert "genomics" in (tmp_path / "report.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# A failed run must explain itself
# ---------------------------------------------------------------------------


def test_a_failed_run_surfaces_why(tmp_path):
    """The moment a user most needs this tool is the moment it said least."""
    data = bridge.map_run_report(
        {
            "run": {"id": "7", "status": "FAILED",
                    "statusMessage": "INVALID_ECR_IMAGE_URI: repository not found"},
            "workflow": {},
            "tasks": [{"taskId": "t1", "name": "AlignTask", "status": "FAILED",
                       "statusMessage": "OutOfMemoryError: container killed"}],
        },
        region="us-east-1",
    )
    bridge.write_bundle(tmp_path, data, warn_before_overwrite=False)
    report = (tmp_path / "report.md").read_text(encoding="utf-8")

    assert "INVALID_ECR_IMAGE_URI" in report, "the run's own failure reason"
    assert "OutOfMemoryError" in report, "and the failing task's"


def test_a_failed_task_gets_enriched_with_its_own_reason():
    """ListRunTasks does not return statusMessage or failureReason -- only
    GetRunTask does. A live private-workflow run proved this: the run-level
    statusMessage came through, but the 'Failed tasks' section named the task
    with no reason at all, even though AWS's GetRunTask held one
    (failureReason: RUN_TASK_FAILED) the whole time.

    Enrichment is scoped to failed tasks only. A large successful run must not
    pay for N extra calls to learn nothing new."""
    client = FakeOmics(
        GetRun={"id": "7", "status": "FAILED"},
        ListRunTasks={"items": [
            {"taskId": "t1", "name": "Echo", "status": "FAILED"},
            {"taskId": "t2", "name": "Setup", "status": "COMPLETED"},
        ]},
        GetRunTask={"failureReason": "RUN_TASK_FAILED",
                    "statusMessage": "exec /bin/bash: exec format error"},
    )
    bundle = bridge.fetch_run_bundle(client=client, run_id="7")

    failed = next(t for t in bundle["tasks"] if t["taskId"] == "t1")
    assert failed["failureReason"] == "RUN_TASK_FAILED"
    assert failed["statusMessage"] == "exec /bin/bash: exec format error"

    ok = next(t for t in bundle["tasks"] if t["taskId"] == "t2")
    assert "failureReason" not in ok, "a completed task needs no extra call"

    get_run_task_calls = [kw for op, kw in client.calls if op == "GetRunTask"]
    assert len(get_run_task_calls) == 1
    assert get_run_task_calls[0] == {"id": "7", "taskId": "t1"}


def test_task_enrichment_is_best_effort():
    """A permissions gap or a task GetRunTask itself fails on must not sink an
    otherwise good report -- the same posture as the tag lookup."""
    class FlakyOnGetRunTask(FakeOmics):
        def call(self, operation, **kwargs):
            if operation == "GetRunTask":
                raise RuntimeError("AccessDeniedException")
            return super().call(operation, **kwargs)

    client = FlakyOnGetRunTask(
        GetRun={"id": "7", "status": "FAILED"},
        ListRunTasks={"items": [{"taskId": "t1", "name": "Echo", "status": "FAILED"}]},
    )
    bundle = bridge.fetch_run_bundle(client=client, run_id="7")
    assert bundle["tasks"][0]["status"] == "FAILED"


def test_task_enrichment_is_capped():
    """A run with many failed tasks should not turn one report into dozens of
    extra API calls; the cap keeps the enrichment worth its cost."""
    tasks = [{"taskId": f"t{i}", "name": f"Task{i}", "status": "FAILED"} for i in range(30)]
    client = FakeOmics(
        GetRun={"id": "7", "status": "FAILED"},
        ListRunTasks={"items": tasks},
        GetRunTask={"failureReason": "RUN_TASK_FAILED", "statusMessage": "x"},
    )
    bridge.fetch_run_bundle(client=client, run_id="7")
    get_run_task_calls = [kw for op, kw in client.calls if op == "GetRunTask"]
    assert len(get_run_task_calls) == bridge._MAX_TASKS_TO_ENRICH


# ---------------------------------------------------------------------------
# Robustness against the API's own limits
# ---------------------------------------------------------------------------


def test_client_configures_retries_rather_than_inheriting_defaults():
    """A polling skill against a throttling API should say how it retries."""
    source = (SKILL_DIR / "omics_client.py").read_text(encoding="utf-8")
    assert "adaptive" in source
    assert "max_attempts" in source


@pytest.mark.parametrize(
    "asked,billed",
    [(1200, 1200), (1, 1200), (2400, 2400), (5000, 7200), (42000, 43200), (2401, 4800)],
)
def test_static_storage_rounds_to_what_aws_will_actually_bill(asked, billed):
    """STATIC capacity is 1,200 GiB or a multiple of 2,400, rounded up. AWS's
    own worked examples: 5000 -> 7200 and 42000 -> 43200. Asking for 5000 and
    being billed 7200 is a surprise this skill can remove."""
    assert bridge.normalise_storage_capacity(asked) == billed


def test_storage_rounding_is_announced_not_silent(capsys):
    bridge.normalise_storage_capacity(5000, announce=True)
    err = capsys.readouterr().err
    assert "5000" in err and "7200" in err


# ---------------------------------------------------------------------------
# SKILL.md conformance
# ---------------------------------------------------------------------------


def test_skill_metadata_conforms_to_clawbio_template():
    content = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    front = yaml.safe_load(content.split("---", 2)[1])
    assert front["name"] == SKILL_DIR.name
    assert re.fullmatch(r"\d+\.\d+\.\d+", str(front["metadata"]["version"]))
    assert len(front["metadata"]["openclaw"]["trigger_keywords"]) >= 3
    for heading in (
        "## Trigger", "## Scope", "## Workflow", "## Example Output",
        "## Output Structure", "## Gotchas", "## Safety", "## Agent Boundary",
        "## Dependencies", "## Maintenance",
    ):
        assert heading in content, f"missing required section: {heading}"
    assert content.count("You will want to") >= 3
    assert len(content.splitlines()) < 500


def test_skill_md_declares_the_egress_and_cost_gates():
    content = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "--allow-remote-inputs" in content
    assert "--confirm-submit" in content


# ---------------------------------------------------------------------------
# Closing the loop: data in, data out, and verifying what came back
# ---------------------------------------------------------------------------


class FakeS3Ops:
    """Recording stand-in for the allow-listed S3 client."""

    def __init__(self, **responses: Any) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, **kwargs: Any) -> Any:
        self.calls.append((method, kwargs))
        if method not in self.responses:
            raise AssertionError(f"Unexpected S3 method: {method}")
        value = self.responses[method]
        return value(**kwargs) if callable(value) else value


def test_upload_refuses_without_the_egress_acknowledgement(tmp_path):
    """Uploading puts a genome somewhere it was not before. Same fail-closed
    contract as --start-run, and for the same reason."""
    src = tmp_path / "reads.fastq"; src.write_text("@r\nACGT\n+\n!!!!\n")
    s3 = FakeS3Ops(upload_file=None)

    with pytest.raises(bridge.EgressRefused):
        bridge.upload_run_inputs(
            client=s3, sources=[src], destination="s3://bucket/in/",
            acknowledged=False, confirmed=True)
    assert s3.calls == [], "nothing may reach S3 before the gate passes"


def test_upload_without_confirmation_transfers_nothing(tmp_path):
    src = tmp_path / "reads.fastq"; src.write_text("x")
    s3 = FakeS3Ops(upload_file=None)

    result = bridge.upload_run_inputs(
        client=s3, sources=[src], destination="s3://bucket/in/",
        acknowledged=True, confirmed=False)

    assert result["uploaded"] is False
    assert s3.calls == [], "an unconfirmed upload is a dry run"


def test_a_confirmed_upload_transfers_and_reports_the_uris(tmp_path):
    src = tmp_path / "reads.fastq"; src.write_text("x")
    s3 = FakeS3Ops(upload_file=None)

    result = bridge.upload_run_inputs(
        client=s3, sources=[src], destination="s3://bucket/in/",
        acknowledged=True, confirmed=True)

    assert result["uploaded"] is True
    assert result["uris"] == ["s3://bucket/in/reads.fastq"]
    assert [c[0] for c in s3.calls] == ["upload_file"]
    # The per-file detail must survive alongside the boolean status. An earlier
    # draft used "uploaded" for both and the boolean silently clobbered the
    # list, emptying the uploads table while every assertion above still passed.
    assert [f["key"] for f in result["uploaded_files"]] == ["in/reads.fastq"]


def test_download_needs_no_egress_gate_only_confirmation(tmp_path):
    """Downloading brings data TO the machine. Gating both directions makes the
    flag reflexive, and a flag passed on every command stops carrying meaning
    on the one command where it matters."""
    objects = [{"key": "out/7/a.txt", "size": 1, "etag": "e"}]
    s3 = FakeS3Ops(
        list_objects_v2={"Contents": [{"Key": "out/7/a.txt", "Size": 1, "ETag": '"e"'}]},
        download_file=lambda Bucket, Key, Filename: Path(Filename).write_text("d"),
    )

    dry = bridge.download_run_outputs(
        client=s3, output_uri="s3://bucket/out/", run_id="7",
        destination=tmp_path, confirmed=False)
    assert dry["downloaded"] is False

    wet = bridge.download_run_outputs(
        client=s3, output_uri="s3://bucket/out/", run_id="7",
        destination=tmp_path, confirmed=True)
    assert wet["downloaded"] is True
    assert wet["n_downloaded"] == 1
    assert objects  # layout asserted in test_s3_client


def test_download_targets_only_this_runs_prefix():
    """<outputUri>/<runId>/ -- pointing at outputUri alone pulls every run the
    account ever wrote."""
    s3 = FakeS3Ops(list_objects_v2={"Contents": []})
    bridge.download_run_outputs(
        client=s3, output_uri="s3://bucket/out/", run_id="7049640",
        destination=Path("/tmp/x"), confirmed=False)
    assert s3.calls[0][1]["Prefix"] == "out/7049640/"


def test_manifest_verification_records_etags_without_downloading(tmp_path):
    """Cheap by design: a listing, no egress, no bytes moved."""
    s3 = FakeS3Ops(list_objects_v2={"Contents": [
        {"Key": "out/7/a.txt", "Size": 10, "ETag": '"abc"'},
        {"Key": "out/7/b.bam", "Size": 99, "ETag": '"def-4"'},
    ]})

    result = bridge.verify_run_outputs(
        client=s3, output_uri="s3://bucket/out/", run_id="7",
        depth="manifest", destination=None, confirmed=False)

    assert result["depth"] == "manifest"
    assert result["n_objects"] == 2
    assert "download_file" not in [c[0] for c in s3.calls]
    multi = next(o for o in result["objects"] if o["key"].endswith(".bam"))
    assert multi["is_md5"] is False, "a -N ETag is not an md5 of the object"


def test_a_manifest_never_calls_an_etag_a_checksum(tmp_path):
    """The repro bundle must not carry a guarantee that does not hold."""
    s3 = FakeS3Ops(list_objects_v2={"Contents": [
        {"Key": "out/7/b.bam", "Size": 99, "ETag": '"def-4"'}]})
    data = bridge.map_run_report(
        {"run": {"id": "7", "status": "COMPLETED", "outputUri": "s3://bucket/out/"},
         "workflow": {}, "tasks": [], "tags": {}},
        region="us-east-1",
        verification=bridge.verify_run_outputs(
            client=s3, output_uri="s3://bucket/out/", run_id="7",
            depth="manifest", destination=None, confirmed=False),
    )
    bridge.write_bundle(tmp_path, data, warn_before_overwrite=False)
    report = (tmp_path / "report.md").read_text(encoding="utf-8")

    assert "ETag" in report
    assert "not a checksum" in report.lower() or "not an md5" in report.lower()


def test_deep_verification_hashes_what_it_downloaded(tmp_path):
    def _download(Bucket, Key, Filename):  # noqa: N803
        Path(Filename).parent.mkdir(parents=True, exist_ok=True)
        Path(Filename).write_text("payload")

    s3 = FakeS3Ops(
        list_objects_v2={"Contents": [{"Key": "out/7/a.txt", "Size": 7, "ETag": '"e"'}]},
        download_file=_download,
    )
    result = bridge.verify_run_outputs(
        client=s3, output_uri="s3://bucket/out/", run_id="7",
        depth="deep", destination=tmp_path, confirmed=True)

    assert result["depth"] == "deep"
    assert result["objects"][0]["sha256"], "deep mode must produce a real hash"
    assert result["n_missing"] == 0


def test_deep_verification_reports_an_output_that_never_landed(tmp_path):
    """write_checksums silently skips missing files, so it cannot detect a
    missing output on its own. This comparison is the point of deep mode."""
    def _download(Bucket, Key, Filename):  # noqa: N803
        raise RuntimeError("NoSuchKey")

    s3 = FakeS3Ops(
        list_objects_v2={"Contents": [{"Key": "out/7/a.txt", "Size": 7, "ETag": '"e"'}]},
        download_file=_download,
    )
    result = bridge.verify_run_outputs(
        client=s3, output_uri="s3://bucket/out/", run_id="7",
        depth="deep", destination=tmp_path, confirmed=True)

    assert result["n_missing"] == 1
    assert result["complete"] is False


def test_deep_verification_refuses_without_download_confirmation(tmp_path):
    """Deep mode downloads every output; that is real egress and real money."""
    s3 = FakeS3Ops(list_objects_v2={"Contents": []})
    with pytest.raises(SystemExit):
        bridge.main(["--run-status", "7", "--verify-outputs", "deep",
                     "--output", str(tmp_path)])


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_without_confirmation_creates_nothing(tmp_path):
    wdl = tmp_path / "main.wdl"; wdl.write_text("version 1.0\nworkflow W {}\n")
    client = FakeOmics(ListWorkflows={"items": []})

    result = bridge.register_run_workflow(
        client=client, definition=wdl, additional_files=[], name="my-wf",
        engine=None, description=None, parameter_template=None,
        allow_duplicate=False, confirmed=False, output_dir=tmp_path)

    assert result["registered"] is False
    assert "CreateWorkflow" not in [op for op, _ in client.calls]
    assert result["zip"]["sha256"], "the archive is built and pinned even on a dry run"


def test_register_requires_a_workflow_name(tmp_path):
    wdl = tmp_path / "main.wdl"; wdl.write_text("version 1.0\n")
    with pytest.raises(SystemExit):
        bridge.main(["--register", str(wdl), "--output", str(tmp_path)])


def test_register_only_flags_are_rejected_outside_register_mode(tmp_path):
    with pytest.raises(SystemExit):
        bridge.main(["--list-runs", "--workflow-name", "x", "--output", str(tmp_path)])


def test_provenance_stops_claiming_outputs_are_unchecked_once_they_are(tmp_path):
    """The ceiling has to track what actually happened. Asserting 'no checksum
    covers the outputs' under a deep verification that just hashed every one of
    them would be false in the direction that flatters the skill."""
    verified = {
        "depth": "deep", "source": "s3://b/out/7/", "n_objects": 1,
        "n_bytes": 7, "n_missing": 0, "complete": True,
        "downloaded_to": str(tmp_path / "dl"),
        "objects": [{"key": "out/7/a.txt", "size": 7, "etag": "e", "is_md5": True,
                     "sha256": "a" * 64}],
    }
    data = bridge.map_run_report(
        {"run": {"id": "7", "status": "COMPLETED", "outputUri": "s3://b/out/"},
         "workflow": {}, "tasks": [], "tags": {}},
        region="us-east-1", verification=verified,
    )
    bridge.write_bundle(tmp_path, data, warn_before_overwrite=False)
    report = (tmp_path / "report.md").read_text(encoding="utf-8")

    assert "holds no S3 credentials" not in report
    assert "no checksum in this bundle covers" not in report
    assert "sha256" in report.lower()


def test_provenance_still_admits_unchecked_outputs_when_they_are(tmp_path):
    data = bridge.map_run_report(
        {"run": {"id": "7", "status": "COMPLETED", "outputUri": "s3://b/out/"},
         "workflow": {}, "tasks": [], "tags": {}},
        region="us-east-1", verification=None,
    )
    bridge.write_bundle(tmp_path, data, warn_before_overwrite=False)
    report = (tmp_path / "report.md").read_text(encoding="utf-8")

    assert "--verify-outputs" in report, "and it must say how to change that"
