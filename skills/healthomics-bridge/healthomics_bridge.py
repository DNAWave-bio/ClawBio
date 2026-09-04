#!/usr/bin/env python3
"""healthomics-bridge — submit, monitor and import AWS HealthOmics runs via boto3.

Talks to the HealthOmics API directly, behind an allow-listed client, a
fail-closed egress gate and a cost gate on submission. The allowlist is the
point: boto3 exposes all 107 ``omics`` operations, and an agent handed that
surface can delete a run, cancel in-flight work, or mutate shared account
configuration as easily as it can list runs. A narrow set of operations is reachable
here; destruction and shared-config mutation are barred by name and no flag
unlocks them.

Two things the raw SDK does not do for you, which this skill does:

* **Show the request before it costs anything.** ``--start-run`` builds the
  exact ``StartRun`` payload, prices it where AWS publishes a flat fee, and
  submits only behind a second explicit confirmation.
* **Derive the idempotency token.** ``StartRun`` requires ``requestId`` and AWS
  deduplicates submissions that reuse one. Deriving it from the request's own
  content means an accidentally repeated command is a no-op rather than a
  second charge.

What it deliberately does NOT do: run-performance analysis, costed timelines,
workflow linting, container reachability. Those are not API calls — they are
AWS's own tooling (``amazon-omics-tools``, the Run Analyzer) — and this skill
reports what a run did rather than judging how well it did it.

Offline demo (no AWS account, no credentials, no network, no boto3 needed):

    uv run python skills/healthomics-bridge/healthomics_bridge.py --demo --output /tmp/ho
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from clawbio.common.checksums import sha256_file  # noqa: E402
from clawbio.common.report import (  # noqa: E402
    generate_report_footer,
    generate_report_header,
    write_result_json,
)
from clawbio.common.textio import write_text_lf  # noqa: E402
from healthomics_pricing import estimated_cost_line  # noqa: E402
import error_codes as _error_codes  # noqa: E402
import params_template as _params_template  # noqa: E402
import preflight as _preflight  # noqa: E402
import recommendations as _recommendations  # noqa: E402
import registration as _registration  # noqa: E402
import s3_client as _s3  # noqa: E402
from omics_client import (  # noqa: E402
    ALLOWED_OPERATIONS,
    PERMANENTLY_EXCLUDED,
    OmicsCallError,
    OmicsClient,
    OperationNotAllowed,
    build_boto_client,
)

SKILL_NAME = "healthomics-bridge"
SKILL_VERSION = "0.1.0"

_SKILL_DIR = Path(__file__).resolve().parent
_REL_SCRIPT = Path("skills") / _SKILL_DIR.name / Path(__file__).name
_DEMO_BUNDLE = _SKILL_DIR / "tests" / "fixtures" / "demo_run_bundle.json"

DISCLAIMER_MARKER = "not a medical device"

_TASK_FIELDS = ["taskId", "name", "status", "cpus", "memory", "startTime", "stopTime"]
_MAX_TASKS_TO_ENRICH = 25


class EgressRefused(RuntimeError):
    """A submission was attempted without acknowledging that data leaves the machine."""


class OmicsOperations:
    """Allow-listed wrapper over a boto3 omics client.

    The allowlist is checked BEFORE dispatch, so a refused operation never
    reaches AWS even when the underlying client is live.
    """

    def __init__(self, *, _boto: Any) -> None:
        self._boto = _boto

    def call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        if operation not in ALLOWED_OPERATIONS:
            reason = PERMANENTLY_EXCLUDED.get(operation)
            if reason:
                raise OperationNotAllowed(f"{operation} is refused: {reason}.")
            raise OperationNotAllowed(
                f"{operation} is not in this skill's allowlist. healthomics-bridge "
                f"may call only: {', '.join(sorted(ALLOWED_OPERATIONS))}."
            )
        # botocore exposes operations as snake_case methods; the allowlist is
        # kept in the API's own PascalCase so it reads the same as AWS's docs.
        method = "".join(
            "_" + c.lower() if c.isupper() else c for c in operation
        ).lstrip("_")
        return getattr(self._boto, method)(**kwargs)


def collect_remote_paths(params: dict[str, Any], output_uri: str | None) -> list[str]:
    """Every URI in the submission that would move data off this machine."""
    remote: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str) and "://" in value:
            remote.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk({k: v for k, v in params.items() if not str(k).startswith("_")})
    if output_uri:
        remote.append(output_uri)
    return sorted(set(remote))


def check_remote_inputs(
    params: dict[str, Any], output_uri: str | None, acknowledged: bool
) -> None:
    """Fail closed unless the caller has acknowledged data egress.

    Mirrors the nf-core wrappers' --allow-remote-inputs contract: the gate
    records an acknowledgement, not a data location.
    """
    remote = collect_remote_paths(params, output_uri)
    if not remote:
        return
    if not acknowledged:
        raise EgressRefused(
            "This submission reads or writes paths outside this machine:\n  "
            + "\n  ".join(remote)
            + "\nRe-run with --allow-remote-inputs to acknowledge that genomic "
            "data will be handled by AWS HealthOmics."
        )
    print(
        f"WARNING: --allow-remote-inputs is set; {len(remote)} path(s) will be read "
        "or written by AWS HealthOmics, so genomic data leaves the local machine: "
        + ", ".join(remote),
        file=sys.stderr,
    )


def derive_request_id(
    *,
    workflow_id: str,
    workflow_type: str,
    params: dict[str, Any],
    output_uri: str,
    role_arn: str,
    run_name: str,
) -> str:
    """A stable idempotency token derived from the submission itself.

    ``StartRun`` requires ``requestId``; AWS deduplicates submissions that
    reuse one. Deriving the token from the request's own content means
    re-running the identical command is a no-op at AWS rather than a second
    billable run — and changing any part of the submission correctly yields a
    new token, so a genuine resubmission is never suppressed.
    """
    payload = json.dumps(
        {
            "workflow_id": workflow_id,
            "workflow_type": workflow_type,
            "params": params,
            "output_uri": output_uri,
            "role_arn": role_arn,
            "run_name": run_name,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def build_start_run_request(
    *,
    workflow_id: str,
    workflow_type: str,
    params: dict[str, Any],
    output_uri: str,
    role_arn: str,
    run_name: str,
    request_id: str,
    storage_type: str | None = None,
    storage_capacity: int | None = None,
    cache_id: str | None = None,
    cache_behavior: str | None = None,
    run_group_id: str | None = None,
    workflow_version_name: str | None = None,
    tags: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble the exact StartRun payload, so it can be shown before it is sent.

    Keys are AWS's own camelCase field names, used directly — there is no
    translation layer, so a casing mismatch between this skill and the API
    cannot arise. Optional fields are omitted when unset so the
    API's own defaults apply (notably ``storageType``, which defaults to the
    preferred DYNAMIC).
    """
    request: dict[str, Any] = {
        "workflowId": workflow_id,
        "workflowType": workflow_type,
        "roleArn": role_arn,
        "name": run_name,
        "outputUri": output_uri,
        "parameters": {k: v for k, v in params.items() if not str(k).startswith("_")},
        "requestId": request_id,
    }
    optional = {
        "storageType": storage_type,
        "storageCapacity": storage_capacity,
        "cacheId": cache_id,
        "cacheBehavior": cache_behavior,
        "runGroupId": run_group_id,
        "workflowVersionName": workflow_version_name,
        "tags": tags,
    }
    request.update({k: v for k, v in optional.items() if v is not None})
    return request


_API_PAGE_MAX = 100  # AWS caps maxResults at 100 on ListRuns and ListWorkflows.


def list_all(
    *, client: OmicsClient, operation: str, limit: int, **filters: Any
) -> list[dict[str, Any]]:
    """Page through a listing until ``limit`` items or the results run out.

    AWS caps ``maxResults`` at 100 and returns a ``nextToken``. Sending
    ``maxResults=150`` does not fail — it silently returns 100, which is a
    wrong answer delivered confidently. Requesting more than the cap is also
    just rude to the API, so each page asks for at most what AWS allows.
    """
    items: list[dict[str, Any]] = []
    token: str | None = None
    while len(items) < limit:
        kwargs = dict(filters)
        kwargs["maxResults"] = min(_API_PAGE_MAX, limit - len(items))
        if token:
            kwargs["startingToken"] = token
        response = client.call(operation, **kwargs)
        page = list(response.get("items", []))
        items.extend(page)
        token = response.get("nextToken")
        if not token or not page:
            break
    return items[:limit]


_TERMINAL_RUN_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "DELETED"})


def wait_for_run(
    *, client: OmicsClient, run_id: str, poll_seconds: float = 30.0,
    timeout_seconds: float = 86_400.0,
) -> dict[str, Any]:
    """Poll ``GetRun`` until the run reaches a terminal state.

    Watching a run you started is not analysis, so it belongs here rather than
    a separate tool. Uses only ``GetRun``, already allow-listed, so this adds
    no reach — a caller who can start a run can already read its status.

    Raises ``TimeoutError`` rather than polling forever: an unbounded loop
    against a billing API is how a stuck run becomes a stuck terminal.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        run = client.call("GetRun", id=str(run_id))
        status = str(run.get("status", "")).upper()
        if status in _TERMINAL_RUN_STATES:
            return run
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Run {run_id} was {status or 'UNKNOWN'} after {timeout_seconds:.0f}s. "
                f"It is still running and still billing — this gave up watching, "
                f"it did not stop the run."
            )
        time.sleep(poll_seconds)


def normalise_storage_capacity(requested: int, *, announce: bool = False) -> int:
    """Round a STATIC capacity up to what AWS will actually allocate and bill.

    The rule is 1,200 GiB **or a multiple of 2,400 GiB** — not 1,200 chunks,
    which is the intuitive-but-wrong reading. AWS's own worked examples:
    5,000 rounds to 7,200 and 42,000 rounds to 43,200.

    Rounding rather than refusing, because AWS rounds anyway; the value this
    adds is telling the user before the invoice does.
    """
    if requested <= 1_200:
        allocated = 1_200
    else:
        allocated = 2_400 * math.ceil(requested / 2_400)
    if announce and allocated != requested:
        print(
            f"NOTE: --storage-capacity {requested} GiB is not an allocatable size. "
            f"AWS allocates and bills STATIC storage as 1,200 GiB or a multiple of "
            f"2,400 GiB, so this run will use {allocated} GiB.",
            file=sys.stderr,
        )
    return allocated


def _enrich_failed_tasks(*, client: OmicsClient, run_id: str, tasks: list[dict[str, Any]]) -> None:
    """Fetch the one field ListRunTasks omits: why a failed task failed.

    Mutates ``tasks`` in place. Scoped to FAILED/CANCELLED tasks, capped at
    ``_MAX_TASKS_TO_ENRICH``, and best-effort per task -- a permissions gap or
    a single GetRunTask failure must not sink an otherwise good report, the
    same posture as the tag lookup above it.
    """
    failed = [t for t in tasks if str(t.get("status", "")).upper() in {"FAILED", "CANCELLED"}]
    for task in failed[:_MAX_TASKS_TO_ENRICH]:
        task_id = task.get("taskId")
        if not task_id:
            continue
        try:
            detail = client.call("GetRunTask", id=str(run_id), taskId=str(task_id))
        except Exception:
            continue
        for field in ("statusMessage", "failureReason", "logStream"):
            if detail.get(field):
                task[field] = detail[field]


def run_output_prefix(output_uri: str, run_id: str) -> str:
    """The S3 prefix holding exactly this run's outputs.

    HealthOmics writes each run under ``<outputUri>/<runId>/``. Pointing at
    ``<outputUri>`` alone reaches every run the account ever wrote — the same
    rule the report's fetch command already encodes.
    """
    return f"{output_uri.rstrip('/')}/{str(run_id).strip()}/"


def upload_run_inputs(
    *, client: Any, sources: list[Path], destination: str,
    acknowledged: bool, confirmed: bool,
) -> dict[str, Any]:
    """Put a run's inputs in S3, behind the same two gates as a submission.

    ``acknowledged`` records that the user knows data leaves the machine;
    ``confirmed`` is the separate decision to actually transfer. Uploading puts
    a genome somewhere it was not before, which is exactly the consequence
    ``--allow-remote-inputs`` exists to make visible.
    """
    resolved = [Path(s).expanduser() for s in sources]
    if not acknowledged:
        raise EgressRefused(
            "This upload would copy local files to S3:\n  "
            + "\n  ".join(str(p) for p in resolved)
            + f"\n  -> {destination}"
            + "\nRe-run with --allow-remote-inputs to acknowledge that genomic "
            "data will leave this machine."
        )
    print(
        f"WARNING: --allow-remote-inputs is set; {len(resolved)} local file(s) "
        f"will be copied to {destination}, so genomic data leaves the local machine.",
        file=sys.stderr,
    )
    if not confirmed:
        print(
            "ESTIMATE ONLY: nothing was uploaded. Re-run with --confirm-upload "
            "to transfer.",
            file=sys.stderr,
        )
        return {
            "mode": "upload", "uploaded": False, "destination": destination,
            "sources": [str(p) for p in resolved], "uris": [], "n_uploaded": 0,
        }

    result = _s3.upload_files(client=client, sources=resolved, destination=destination)
    result.update({"mode": "upload", "uploaded": True,
                   "sources": [str(p) for p in resolved]})
    return result


def download_run_outputs(
    *, client: Any, output_uri: str, run_id: str, destination: Path,
    confirmed: bool,
) -> dict[str, Any]:
    """Bring one run's outputs back to this machine.

    One gate, not two. There is no egress acknowledgement because this moves
    data *toward* the user; gating both directions would make the flag
    reflexive, and a flag passed on every command stops carrying meaning on the
    command where it matters.
    """
    prefix_uri = run_output_prefix(output_uri, run_id)
    objects = _s3.list_objects(client=client, uri=prefix_uri)
    bucket, key_prefix = _s3.parse_s3_uri(prefix_uri)

    if not confirmed:
        total = sum(o["size"] for o in objects)
        print(
            f"ESTIMATE ONLY: {len(objects)} object(s), {total:,} bytes under "
            f"{prefix_uri}. Nothing was downloaded. Re-run with "
            f"--confirm-download to transfer (S3 egress is billable).",
            file=sys.stderr,
        )
        return {
            "mode": "download", "downloaded": False, "source": prefix_uri,
            "n_objects": len(objects), "n_bytes": total, "n_downloaded": 0,
            "objects": objects,
        }

    transferred = _s3.download_objects(
        client=client, bucket=bucket, objects=objects,
        key_prefix=key_prefix, destination=Path(destination))
    transferred.update({"mode": "download", "downloaded": True,
                        "source": prefix_uri, "n_objects": len(objects)})
    return transferred


def verify_run_outputs(
    *, client: Any, output_uri: str, run_id: str, depth: str,
    destination: Path | None, confirmed: bool,
) -> dict[str, Any]:
    """Say what a run actually produced, at one of two depths.

    ``manifest`` lists the run's output prefix and records each object's size
    and ETag. It moves no bytes and costs nothing.

    ``deep`` additionally downloads each object and computes a real SHA-256.
    That is a genuine checksum the repro bundle can stand behind, and it is the
    only mode that can prove an output actually exists — which matters because
    ``write_checksums`` silently skips paths that are missing and so cannot
    detect an output that never landed.
    """
    prefix_uri = run_output_prefix(output_uri, run_id)
    listed = _s3.list_objects(client=client, uri=prefix_uri)

    objects: list[dict[str, Any]] = []
    for item in listed:
        etag = _s3.describe_etag(item.get("etag", ""))
        objects.append({**item, **etag, "sha256": None})

    result: dict[str, Any] = {
        "depth": "manifest", "source": prefix_uri, "objects": objects,
        "n_objects": len(objects), "n_bytes": sum(o["size"] for o in objects),
        "n_missing": 0, "complete": True, "downloaded_to": None,
    }
    if depth != "deep":
        return result

    bucket, key_prefix = _s3.parse_s3_uri(prefix_uri)
    target = Path(destination) if destination else Path.cwd() / f"run-{run_id}"
    transferred = _s3.download_objects(
        client=client, bucket=bucket, objects=listed,
        key_prefix=key_prefix, destination=target)

    by_key = {d["key"]: d["path"] for d in transferred["downloaded_files"]}
    for entry in objects:
        path = by_key.get(entry["key"])
        if path and Path(path).is_file():
            entry["sha256"] = sha256_file(path)
            entry["local_path"] = path

    missing = [o["key"] for o in objects if not o["sha256"]]
    result.update({
        "depth": "deep",
        "downloaded_to": str(target),
        "n_missing": len(missing),
        "missing": missing,
        "complete": not missing,
        "failures": transferred["failures"],
    })
    return result


def register_run_workflow(
    *, client: OmicsClient, definition: Path, additional_files: list[Path],
    name: str, engine: str | None, description: str | None,
    parameter_template: dict[str, Any] | None, allow_duplicate: bool,
    confirmed: bool, output_dir: Path,
) -> dict[str, Any]:
    """Register a definition as a private workflow, gated by --confirm-register.

    The archive is built and checksummed even on a dry run: those bytes are the
    one piece of this operation the skill can pin honestly, having produced
    them itself, and seeing the digest before creating anything is the point of
    a dry run.
    """
    definition = Path(definition).expanduser().resolve()
    resolved_engine = _registration.resolve_engine(definition, engine)
    members = _registration.resolve_zip_members(definition, list(additional_files))
    manifest = _registration.build_definition_zip(
        members=members, destination=Path(output_dir) / "tables" / "workflow.zip")

    collisions = _registration.assert_workflow_name_is_free(
        client=client, name=name, allow_duplicate=allow_duplicate)

    request_id = hashlib.sha256(
        f"{name}:{resolved_engine}:{manifest['sha256']}".encode("utf-8")
    ).hexdigest()[:32]
    request = _registration.build_create_workflow_request(
        name=name, engine=resolved_engine, zip_path=Path(manifest["path"]),
        request_id=request_id, description=description,
        parameter_template=parameter_template)

    base: dict[str, Any] = {
        "mode": "register", "workflow_name": name, "engine": resolved_engine,
        "definition_path": str(definition), "zip": manifest,
        "name_collisions": collisions, "registered": False,
        "workflow_id": None, "workflow_status": None,
        "workflow_status_message": None,
    }

    if not confirmed:
        print(
            "DRY RUN: no workflow was created. Re-run with --confirm-register "
            "to create it.",
            file=sys.stderr,
        )
        return base

    created = _registration.register_workflow(client=client, request=request)
    workflow_id = str(created.get("id") or created.get("workflowId") or "")
    workflow = _registration.poll_workflow_until_settled(
        client=client, workflow_id=workflow_id)
    base.update({
        "registered": True, "workflow_id": workflow_id,
        "workflow_status": str(workflow.get("status", "")).upper(),
        # AWS's own explanation, not a guess. The same field this skill
        # already reads for a failed run and a failed task -- registration
        # discarded it after polling until a live FAILED registration exposed
        # that the report was printing a generic hint instead.
        "workflow_status_message": workflow.get("statusMessage"),
    })
    return base


def register_workflow_version(
    *, client: OmicsClient, workflow_id: str, definition: Path,
    additional_files: list[Path], version_name: str, description: str | None,
    parameter_template: dict[str, Any] | None, confirmed: bool, output_dir: Path,
) -> dict[str, Any]:
    """Add a version to an EXISTING workflow, rather than creating a new one.

    --start-run has always accepted --workflow-version-name; there was no path
    that could ever create the version it names. Reuses the same reproducible
    archive machinery as --register -- the same honesty about what the digest
    proves, and the same inability to lint before AWS validates server-side.
    """
    definition = Path(definition).expanduser().resolve()
    resolved_engine = _registration.resolve_engine(definition, None)
    members = _registration.resolve_zip_members(definition, list(additional_files))
    manifest = _registration.build_definition_zip(
        members=members,
        destination=Path(output_dir) / "tables" / f"workflow-version-{version_name}.zip")

    request_id = hashlib.sha256(
        f"{workflow_id}:{version_name}:{manifest['sha256']}".encode("utf-8")
    ).hexdigest()[:32]
    request: dict[str, Any] = {
        "workflowId": workflow_id, "versionName": version_name,
        "definitionZip": Path(manifest["path"]).read_bytes(), "requestId": request_id,
    }
    if description:
        request["description"] = description
    if parameter_template:
        request["parameterTemplate"] = parameter_template

    base: dict[str, Any] = {
        "mode": "register-version", "workflow_id": workflow_id,
        "version_name": version_name, "engine": resolved_engine,
        "definition_path": str(definition), "zip": manifest,
        "registered": False, "version_status": None, "version_status_message": None,
    }
    if not confirmed:
        print(
            "DRY RUN: no workflow version was created. Re-run with "
            "--confirm-register to create it.",
            file=sys.stderr,
        )
        return base

    client.call("CreateWorkflowVersion", **request)
    version = client.call(
        "GetWorkflowVersion", workflowId=workflow_id, versionName=version_name)
    base.update({
        "registered": True,
        "version_status": str(version.get("status", "")).upper(),
        "version_status_message": version.get("statusMessage"),
    })
    return base


def describe_run_group(*, client: OmicsClient, group_id: str) -> dict[str, Any]:
    """One run group's own detail -- concurrency and cost limits --
    so --start-run --run-group-id is not a blind reference to an id you
    listed but never actually looked at."""
    return client.call("GetRunGroup", id=str(group_id))


def describe_run_cache(*, client: OmicsClient, cache_id: str) -> dict[str, Any]:
    """One run cache's own detail, for the same reason as describe_run_group."""
    return client.call("GetRunCache", id=str(cache_id))


def tag_run(*, client: OmicsClient, run_id: str, tags: dict[str, str]) -> dict[str, Any]:
    """Set tags on a run after submission.

    ListTagsForResource had no write-side pair: tags were settable only at
    --start-run time, with no way back to correct or add them afterward.
    """
    run = client.call("GetRun", id=str(run_id))
    arn = run.get("arn")
    if not arn:
        raise OmicsCallError(f"Run {run_id} has no arn to tag.")
    client.call("TagResource", resourceArn=str(arn), tags=tags)
    return {"mode": "tag", "run_id": run_id, "tagged": tags, "arn": arn}


def list_run_tags(*, client: OmicsClient, run_id: str) -> dict[str, Any]:
    """Read one run's tags through the same resource ARN AWS mutates."""
    run = client.call("GetRun", id=str(run_id))
    arn = run.get("arn")
    if not arn:
        raise OmicsCallError(f"Run {run_id} has no arn to list tags.")
    tags = dict(client.call("ListTagsForResource", resourceArn=str(arn)).get("tags", {}))
    return {"mode": "tags", "run_id": run_id, "tags": tags, "arn": arn}


def sync_run_tags(
    *, client: OmicsClient, run_id: str, desired_tags: dict[str, str]
) -> dict[str, Any]:
    """Converge a run's tags to a desired JSON object.

    This is intentionally narrow: it reads current run tags, sets changed keys,
    and removes keys absent from the desired set. It never touches account-level
    tagging configuration.
    """
    run = client.call("GetRun", id=str(run_id))
    arn = run.get("arn")
    if not arn:
        raise OmicsCallError(f"Run {run_id} has no arn to sync tags.")
    current = dict(client.call("ListTagsForResource", resourceArn=str(arn)).get("tags", {}))
    to_set = {k: v for k, v in desired_tags.items() if current.get(k) != v}
    to_remove = sorted(set(current) - set(desired_tags))
    if to_set:
        client.call("TagResource", resourceArn=str(arn), tags=to_set)
    if to_remove:
        client.call("UntagResource", resourceArn=str(arn), tagKeys=to_remove)
    return {
        "mode": "sync-tags",
        "run_id": run_id,
        "arn": arn,
        "previous_tags": current,
        "desired_tags": desired_tags,
        "set": to_set,
        "removed": to_remove,
        "tags": desired_tags,
    }


def untag_run(*, client: OmicsClient, run_id: str, keys: list[str]) -> dict[str, Any]:
    """Remove tags from a run by key."""
    run = client.call("GetRun", id=str(run_id))
    arn = run.get("arn")
    if not arn:
        raise OmicsCallError(f"Run {run_id} has no arn to untag.")
    client.call("UntagResource", resourceArn=str(arn), tagKeys=keys)
    return {"mode": "untag", "run_id": run_id, "untagged": keys, "arn": arn}


def fetch_run_bundle(*, client: OmicsClient, run_id: str) -> dict[str, Any]:
    """Everything one run report needs, in as few calls as possible.

    Still one workflow lookup, with no try-PRIVATE-then-retry-READY2RUN dance —
    but not because the id self-resolves. ``GetWorkflow`` raises
    ``ResourceNotFoundException`` for a Ready2Run id unless told
    ``type=READY2RUN``; the run record carries ``workflowType``, so the type is
    already known before the call, and no try-one-then-the-other retry is
    needed.
    """
    run = client.call("GetRun", id=str(run_id))
    tasks_response = client.call("ListRunTasks", id=str(run_id))
    tasks = list(tasks_response.get("items", []))
    _enrich_failed_tasks(client=client, run_id=run_id, tasks=tasks)

    workflow: dict[str, Any] = {}
    workflow_id = run.get("workflowId")
    if workflow_id:
        lookup: dict[str, Any] = {"id": str(workflow_id)}
        # Omitted rather than guessed when the run does not say: AWS's own
        # default is the right answer, and a wrong guess is a not-found error
        # that reads like a bad id.
        workflow_type = run.get("workflowType")
        if workflow_type:
            lookup["type"] = workflow_type
        try:
            workflow = client.call("GetWorkflow", **lookup)
        except Exception:
            # A workflow this account can no longer see does not invalidate the
            # run report; it just means the workflow block stays empty. This
            # once also hid a real bug — the lookup failing for every Ready2Run
            # run — so the report names the workflow as unavailable rather than
            # quietly omitting it.
            workflow = {}

    # Tags are read back, not echoed from the submission. Setting run tags is
    # this skill's headline capability and it could not confirm its own work --
    # verifying required the AWS CLI. Read-only and best-effort: a missing
    # ListTagsForResource permission must not sink an otherwise good report.
    tags: dict[str, str] = {}
    arn = run.get("arn")
    if arn:
        try:
            tags = dict(client.call("ListTagsForResource", resourceArn=str(arn)).get("tags", {}))
        except Exception:
            tags = {}

    return {"run": run, "workflow": workflow, "tasks": tasks, "tags": tags}


def submit_run(
    *, client: OmicsClient, request: dict[str, Any], confirmed: bool
) -> dict[str, Any]:
    """Submit a run, but only past the cost gate.

    Without ``confirmed`` this returns the request untouched and calls nothing
    — the estimate-first contract, and the reason an
    unconfirmed --start-run bills nothing.
    """
    if not confirmed:
        return {"submitted": False, "request": request, "response": {}}
    response = client.call("StartRun", **request)
    return {"submitted": True, "request": request, "response": response}


def map_run_report(
    bundle: dict[str, Any],
    *,
    region: str,
    start_run_request: dict[str, Any] | None = None,
    submitted: bool = False,
    demo: bool = False,
    verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Turn API payloads into this skill's own reported shape."""
    run = bundle.get("run") or {}
    workflow = bundle.get("workflow") or {}
    tasks = list(bundle.get("tasks") or [])

    failed = [t for t in tasks if str(t.get("status", "")).upper() in {"FAILED", "CANCELLED"}]
    completed = [t for t in tasks if str(t.get("status", "")).upper() == "COMPLETED"]

    return {
        "mode": "run",
        "region": region,
        "transport": "boto3",
        "run": run,
        "workflow": workflow,
        "tasks": tasks,
        "tags": dict(bundle.get("tags") or {}),
        "verification": verification,
        "n_tasks": len(tasks),
        "n_completed": len(completed),
        "n_failed": len(failed),
        "run_status": run.get("status", "UNKNOWN"),
        "submitted": submitted,
        "start_run_request": start_run_request,
        "demo": demo,
    }


def map_list_report(
    items: list[dict[str, Any]], *, kind: str, region: str, demo: bool = False
) -> dict[str, Any]:
    """Turn a --list-runs / --list-workflows result into the reported shape."""
    return {
        "mode": kind,
        "region": region,
        "transport": "boto3",
        "items": items,
        "n_items": len(items),
        "run": {}, "workflow": {}, "tasks": [], "tags": {}, "verification": None,
        "run_status": "N/A",
        "n_tasks": 0, "n_completed": 0, "n_failed": 0,
        "submitted": False, "start_run_request": None, "demo": demo,
    }


def map_check_report(preflight_result: dict[str, Any], *, region: str) -> dict[str, Any]:
    """Turn preflight checks into the shared reported shape."""
    data = _pad_transfer_report(
        {
            **preflight_result,
            "region": region,
            "items": list(preflight_result.get("checks") or []),
            "n_items": int(preflight_result.get("n_checks", 0)),
        },
        region,
    )
    data["mode"] = "check"
    return data


def map_workflow_search_report(
    *, query: str, items: list[dict[str, Any]], kind: str, region: str
) -> dict[str, Any]:
    """Search/recommendation reports are workflow listings with scores."""
    data = map_list_report(items, kind=kind, region=region)
    data["query"] = query
    return data


def map_params_template_report(payload: dict[str, Any], *, region: str) -> dict[str, Any]:
    """Parameter-template report shape."""
    params = payload.get("params") or {}
    template = payload.get("parameter_template") or {}
    rows = [
        {
            "name": name,
            "default": json.dumps(default, sort_keys=True),
            "template": json.dumps(template.get(name, {}), sort_keys=True),
        }
        for name, default in sorted(params.items())
    ]
    return _pad_transfer_report(
        {
            "mode": "params-template",
            "workflow_id": payload.get("workflow_id"),
            "workflow_name": payload.get("workflow_name"),
            "workflow_type": payload.get("workflow_type"),
            "parameter_template": template,
            "params_template": params,
            "items": rows,
            "n_items": len(rows),
        },
        region,
    )


def _transfer_markdown(data: dict[str, Any]) -> str:
    """Report for the modes that move bytes or create a workflow."""
    mode = data["mode"]
    titles = {
        "upload": "AWS HealthOmics — Input Upload",
        "download": "AWS HealthOmics — Output Download",
        "register": "AWS HealthOmics — Workflow Registration",
    }
    acted = {"upload": data.get("uploaded"), "download": data.get("downloaded"),
             "register": data.get("registered")}[mode]
    header = generate_report_header(
        title=titles[mode],
        skill_name=SKILL_NAME,
        skill_version=SKILL_VERSION,
        extra_metadata={
            "Mode": f"{mode.title()}" + ("" if acted else " — DRY RUN"),
            "Region": data["region"],
        },
    )
    lines = [header, ""]

    if mode == "upload":
        lines += ["## Upload", ""]
        lines.append(f"**Destination**: `{data['destination']}`")
        if acted:
            lines.append(
                f"\n{data['n_uploaded']} file(s), {data['n_bytes']:,} bytes uploaded."
            )
            lines += ["", "| Source | S3 URI | Bytes |", "|---|---|---|"]
            for entry in data.get("uploaded_files", []):
                lines.append(
                    f"| `{entry['source']}` | `{entry['uri']}` | {entry['n_bytes']:,} |"
                )
            lines.append(
                "\nPass these URIs to `--start-run --params`; this skill does not "
                "write them into a params file for you."
            )
        else:
            lines.append("\n**Nothing was uploaded.** Files that would be sent:")
            lines += [""] + [f"- `{p}`" for p in data.get("sources", [])]
            lines.append("\nRe-run with `--confirm-upload` to transfer.")

    elif mode == "download":
        lines += ["## Download", ""]
        lines.append(f"**Source**: `{data['source']}`")
        if acted:
            lines.append(
                f"\n{data['n_downloaded']} of {data['n_objects']} object(s) written "
                f"to `{data['destination']}`."
            )
            if data.get("failures"):
                lines += ["", "### Failed", ""]
                for failure in data["failures"]:
                    lines.append(f"- `{failure['key']}` — {failure['error']}")
        else:
            lines.append(
                f"\n**Nothing was downloaded.** {data['n_objects']} object(s), "
                f"{data['n_bytes']:,} bytes are available. Re-run with "
                f"`--confirm-download` to transfer — S3 egress is billable."
            )

    else:  # register
        zip_manifest = data.get("zip") or {}
        lines += ["## Workflow definition", ""]
        lines.append(f"- **Name**: `{data['workflow_name']}`")
        lines.append(f"- **Engine**: {data['engine']}")
        lines.append(f"- **Definition**: `{data['definition_path']}`")
        lines.append(
            f"- **Archive**: {zip_manifest.get('n_bytes', 0):,} bytes, "
            f"`{str(zip_manifest.get('sha256', ''))[:16]}…` "
            f"({zip_manifest.get('compression', 'stored')})"
        )
        lines += ["", "| Archive member | Bytes | sha256 |", "|---|---|---|"]
        for member in zip_manifest.get("members", []):
            lines.append(
                f"| `{member['archive_name']}` | {member['n_bytes']:,} | "
                f"`{member['sha256'][:12]}…` |"
            )
        lines.append(
            "\nThe archive digest is reproducible: the same inputs always produce "
            "the same bytes, so it pins exactly what was uploaded."
        )
        if acted:
            status = data.get("workflow_status")
            lines += ["", f"## Workflow created — status `{status}`", ""]
            lines.append(f"- **Workflow id**: `{data['workflow_id']}`")
            if status == "FAILED":
                reason = data.get("workflow_status_message")
                lines.append("\n**This workflow failed to register and cannot be run.**")
                if reason:
                    lines.append(f"\nAWS's own reason: {reason}")
                else:
                    lines.append(
                        "\nAWS validates the definition server-side — there is no "
                        "lint API to catch this earlier — so check that the "
                        "entrypoint filename matches what the engine expects."
                    )
            else:
                lines.append(
                    f"\nRun it with:\n\n```bash\n--start-run {data['workflow_id']} "
                    f"--workflow-type PRIVATE --params params.json \\\n"
                    f"  --output-uri s3://<bucket>/output/ --role-arn <role> \\\n"
                    f"  --run-name <name> --allow-remote-inputs --confirm-submit\n```"
                )
            lines.append(
                f"\nThis skill cannot delete a workflow — that is barred by "
                f"consequence, not by omission. Remove it with:\n\n```bash\n"
                f"aws omics delete-workflow --id {data['workflow_id']} "
                f"--region {data['region']}\n```"
            )
        else:
            lines += ["", "## Nothing was created.", ""]
            lines.append(
                "Re-run with `--confirm-register` to create this workflow. "
                "Registration bills nothing; running the workflow does."
            )

    lines += ["", "## Provenance", ""] + _provenance_lines(data)
    lines += ["", generate_report_footer().strip(), ""]
    return "\n".join(lines)


def _tag_markdown(data: dict[str, Any]) -> str:
    """Report for tag read/write modes."""
    labels = {
        "tag": "Tagged",
        "untag": "Untagged",
        "tags": "Tags",
        "sync-tags": "Synced Tags",
    }
    verb = labels[data["mode"]]
    header = generate_report_header(
        title=f"AWS HealthOmics — Run {verb}",
        skill_name=SKILL_NAME, skill_version=SKILL_VERSION,
        extra_metadata={"Mode": verb, "Region": data["region"]},
    )
    lines = [header, "", f"## {verb}", ""]
    lines.append(f"**Run**: `{data['run_id']}` (`{data['arn']}`)")
    if data["mode"] == "tag":
        rendered = ", ".join(f"`{k}={v}`" for k, v in sorted(data["tagged"].items()))
        lines.append(f"\nSet: {rendered}")
    elif data["mode"] == "untag":
        lines.append(f"\nRemoved: {', '.join(f'`{k}`' for k in data['untagged'])}")
    elif data["mode"] == "sync-tags":
        if data.get("set"):
            rendered = ", ".join(f"`{k}={v}`" for k, v in sorted(data["set"].items()))
            lines.append(f"\nSet/updated: {rendered}")
        if data.get("removed"):
            lines.append(f"\nRemoved: {', '.join(f'`{k}`' for k in data['removed'])}")
        if not data.get("set") and not data.get("removed"):
            lines.append("\nNo changes were needed.")
    tags = data.get("tags") or data.get("desired_tags") or data.get("tagged") or {}
    if tags:
        lines += ["", "| Key | Value |", "|---|---|"]
        for key, value in sorted(tags.items()):
            lines.append(f"| `{key}` | `{value}` |")
    lines += ["", generate_report_footer().strip(), ""]
    return "\n".join(lines)


def _register_version_markdown(data: dict[str, Any]) -> str:
    """Report for adding a version to an existing workflow."""
    acted = data.get("registered")
    header = generate_report_header(
        title="AWS HealthOmics — Workflow Version",
        skill_name=SKILL_NAME, skill_version=SKILL_VERSION,
        extra_metadata={
            "Mode": "Register version" + ("" if acted else " — DRY RUN"),
            "Region": data["region"],
        },
    )
    zip_manifest = data.get("zip") or {}
    lines = [header, "", "## Version definition", ""]
    lines.append(f"- **Workflow id**: `{data['workflow_id']}`")
    lines.append(f"- **Version name**: `{data['version_name']}`")
    lines.append(f"- **Engine**: {data['engine']}")
    lines.append(
        f"- **Archive**: {zip_manifest.get('n_bytes', 0):,} bytes, "
        f"`{str(zip_manifest.get('sha256', ''))[:16]}…`"
    )
    if acted:
        status = data.get("version_status")
        lines += ["", f"## Version created — status `{status}`", ""]
        if status == "FAILED":
            reason = data.get("version_status_message")
            lines.append("\n**This version failed to register.**")
            if reason:
                lines.append(f"\nAWS's own reason: {reason}")
        else:
            lines.append(
                f"\nRun it with `--start-run {data['workflow_id']} "
                f"--workflow-version-name {data['version_name']} ...`"
            )
    else:
        lines += ["", "## Nothing was created.", ""]
        lines.append("Re-run with `--confirm-register` to create this version.")
    lines += ["", generate_report_footer().strip(), ""]
    return "\n".join(lines)


def _check_markdown(data: dict[str, Any]) -> str:
    """Report for --check."""
    header = generate_report_header(
        title="AWS HealthOmics — Preflight",
        skill_name=SKILL_NAME,
        skill_version=SKILL_VERSION,
        extra_metadata={"Mode": "Preflight", "Region": data["region"]},
    )
    lines = [header, "", "## Checks", ""]
    lines.append(
        f"{data.get('n_checks', 0)} check(s): "
        f"{data.get('n_failed', 0)} failed, {data.get('n_warnings', 0)} warning(s)."
    )
    lines += ["", "| Check | Result | Severity | Detail |", "|---|---|---|---|"]
    for check in data.get("checks", []):
        result = "PASS" if check.get("ok") else "FAIL"
        lines.append(
            f"| `{check.get('name', '')}` | {result} | "
            f"{check.get('severity', '')} | {check.get('detail', '')} |"
        )
    lines += ["", "## Provenance", ""] + _provenance_lines(data)
    lines += ["", generate_report_footer().strip(), ""]
    return "\n".join(lines)


def _workflow_search_markdown(data: dict[str, Any]) -> str:
    """Report for workflow search and recommendation."""
    title = (
        "AWS HealthOmics — Workflow Recommendations"
        if data["mode"] == "workflow-recommendations"
        else "AWS HealthOmics — Workflow Search"
    )
    header = generate_report_header(
        title=title,
        skill_name=SKILL_NAME,
        skill_version=SKILL_VERSION,
        extra_metadata={"Mode": data["mode"], "Region": data["region"]},
    )
    lines = [header, "", f"## Query", "", f"`{data.get('query', '')}`", ""]
    lines.append(f"{data.get('n_items', 0)} workflow(s) matched.")
    lines += ["", "| Score | Id | Name | Status | Type |", "|---|---|---|---|---|"]
    for item in data.get("items", []):
        lines.append(
            f"| {item.get('matchScore', '')} | `{item.get('id', 'n/a')}` | "
            f"{item.get('name', 'n/a')} | {item.get('status', 'n/a')} | "
            f"{item.get('type', item.get('workflowType', 'n/a'))} |"
        )
    if data.get("items"):
        first = data["items"][0]
        lines += [
            "",
            "## Next step",
            "",
            "Generate a params skeleton before submitting:",
            "",
            "```bash",
            f"--params-template {first.get('id', '<workflow-id>')} "
            f"--workflow-type {first.get('type', first.get('workflowType', 'PRIVATE'))}",
            "```",
        ]
    lines += ["", "## Provenance", ""] + _provenance_lines(data)
    lines += ["", generate_report_footer().strip(), ""]
    return "\n".join(lines)


def _params_template_markdown(data: dict[str, Any]) -> str:
    """Report for --params-template."""
    header = generate_report_header(
        title="AWS HealthOmics — Params Template",
        skill_name=SKILL_NAME,
        skill_version=SKILL_VERSION,
        extra_metadata={"Mode": "Params template", "Region": data["region"]},
    )
    lines = [header, "", "## Workflow", ""]
    lines += [
        f"- **Workflow id**: `{data.get('workflow_id', 'n/a')}`",
        f"- **Workflow name**: {data.get('workflow_name', 'n/a')}",
        f"- **Workflow type**: {data.get('workflow_type', 'n/a')}",
    ]
    lines += ["", "## Parameters", ""]
    if data.get("items"):
        lines += ["| Name | Default |", "|---|---|"]
        for item in data["items"]:
            lines.append(f"| `{item['name']}` | `{item['default']}` |")
    else:
        lines.append("No parameter template was exposed for this workflow.")
    lines += [
        "",
        "A writable skeleton is in `params.template.json`; use it as the starting params file and fill in real S3/local values before `--start-run`.",
        "",
        "## Provenance",
        "",
    ] + _provenance_lines(data)
    lines += ["", generate_report_footer().strip(), ""]
    return "\n".join(lines)


def _verification_lines(data: dict[str, Any]) -> list[str]:
    """Render what the run actually produced, without overstating it.

    The ETag distinction is the whole point of this section. For a single-part
    upload an ETag is the object's MD5; for a multipart upload it is the MD5 of
    the concatenated part MD5s and hashes nothing recomputable from the file.
    Genomic outputs are routinely multipart, so calling either one "the
    checksum" would put a guarantee in the bundle that does not hold.
    """
    verification = data.get("verification")
    if not verification:
        return []

    deep = verification.get("depth") == "deep"
    lines = ["", "## Outputs", ""]
    lines.append(
        f"{verification['n_objects']} object(s), "
        f"{verification['n_bytes']:,} bytes under `{verification['source']}`."
    )

    if deep:
        if verification.get("complete"):
            downloaded_to = verification.get("downloaded_to", "the requested destination")
            lines.append(
                f"\nEvery listed object was downloaded to "
                f"`{downloaded_to}` and hashed. The SHA-256 values "
                f"below are real checksums of the bytes on disk."
            )
        else:
            lines.append(
                f"\n**{verification['n_missing']} listed object(s) could not be "
                f"retrieved**, so this verification is incomplete: "
                + ", ".join(f"`{k}`" for k in verification.get("missing", [])[:5])
            )
    else:
        lines.append(
            "\nListing only — no bytes were transferred. **The ETag column is "
            "not a checksum**: it equals the object's MD5 only for a single-part "
            "upload, and for a multipart upload it is the MD5 of the part MD5s "
            "with a `-N` suffix, which cannot be recomputed from the file. Use "
            "`--verify-outputs deep` for real SHA-256 checksums."
        )

    header = "| Key | Bytes | ETag | MD5? |" + (" SHA-256 |" if deep else "")
    divider = "|---|---|---|---|" + ("---|" if deep else "")
    lines += ["", header, divider]
    for entry in verification["objects"][:50]:
        row = (
            f"| `{entry['key']}` | {entry['size']:,} | `{entry['etag']}` | "
            f"{'yes' if entry.get('is_md5') else 'no (multipart)'} |"
        )
        if deep:
            digest = entry.get("sha256")
            row += f" `{digest[:16]}…`|" if digest else " **missing** |"
        lines.append(row)
    if len(verification["objects"]) > 50:
        lines.append(f"\n…and {len(verification['objects']) - 50} more.")

    return lines


def _provenance_lines(data: dict[str, Any]) -> list[str]:
    """The honest ceiling for work that happened in someone else's account."""
    if data["demo"]:
        ceiling = (
            "This report replays a synthetic fixture. No AWS call was made, and "
            "nothing here describes an actual account, run or workflow."
        )
    elif data.get("mode") != "run" or not data["run"].get("id"):
        ceiling = (
            "No AWS HealthOmics run executed as part of this report. Any "
            "identifiers above describe an unsent request or a query result, "
            "not a run that took place."
        )
    else:
        verification = data.get("verification") or {}
        if verification.get("depth") == "deep" and verification.get("complete"):
            outputs = (
                f"Every one of the {verification['n_objects']} output object(s) was "
                f"downloaded and hashed; the sha256 values in tables/outputs.csv are "
                f"real checksums of those bytes, computed here rather than reported "
                f"by AWS."
            )
        elif verification.get("depth") == "deep":
            outputs = (
                f"Output verification is INCOMPLETE: {verification['n_missing']} of "
                f"{verification['n_objects']} listed object(s) could not be "
                f"retrieved, so the checksums below cover only part of the run."
            )
        elif verification.get("depth") == "manifest":
            outputs = (
                f"The {verification['n_objects']} output object(s) were listed but "
                f"not fetched, so no checksum in this bundle covers their bytes — an "
                f"ETag is not one. Use `--verify-outputs deep` for real sha256s."
            )
        else:
            outputs = (
                "No checksum in this bundle covers the run's outputs, which remain "
                "in S3 and were not read. Add `--verify-outputs` to record what the "
                "run produced."
            )
        ceiling = (
            "This run executed in AWS HealthOmics. Replaying it requires the same "
            "account, execution role and container images. The identifiers here "
            f"pin what the run WAS. {outputs}"
        )
    return [
        "- Transport: **boto3** (AWS HealthOmics API directly).",
        f"- Allow-listed operations: {len(ALLOWED_OPERATIONS)} of 107 available.",
        "",
        ceiling,
    ]


_LIST_MODES = {
    "runs",
    "workflows",
    "run-groups",
    "run-caches",
    "workflow-versions",
    "workflow-search",
    "workflow-recommendations",
}


def _report_markdown(data: dict[str, Any]) -> str:
    if data.get("mode") == "check":
        return _check_markdown(data)
    if data.get("mode") in {"workflow-search", "workflow-recommendations"}:
        return _workflow_search_markdown(data)
    if data.get("mode") == "params-template":
        return _params_template_markdown(data)
    if data.get("mode") in _LIST_MODES:
        return _list_markdown(data)
    if data.get("mode") in {"upload", "download", "register"}:
        return _transfer_markdown(data)
    if data.get("mode") in {"tag", "untag", "tags", "sync-tags"}:
        return _tag_markdown(data)
    if data.get("mode") == "register-version":
        return _register_version_markdown(data)

    run = data["run"]
    workflow = data["workflow"]
    header = generate_report_header(
        title="AWS HealthOmics Run Report",
        skill_name=SKILL_NAME,
        skill_version=SKILL_VERSION,
        extra_metadata={
            "Mode": "Synthetic offline demo" if data["demo"] else "Live AWS HealthOmics (boto3)",
            "Region": data["region"],
            "Run status": str(data["run_status"]),
        },
    )
    lines = [header, "", "## Run", ""]
    lines += [
        f"- **Run id**: `{run.get('id', 'n/a')}`",
        f"- **Run name**: {run.get('name', 'n/a')}",
        f"- **Status**: **{data['run_status']}**",
        f"- **Workflow**: `{workflow.get('name', 'n/a')}` "
        f"(`{workflow.get('id', run.get('workflowId', 'n/a'))}`, "
        f"{workflow.get('type', run.get('workflowType', 'n/a'))})",
        f"- **Output URI**: `{run.get('outputUri', 'n/a')}`",
    ]
    if data.get("tags"):
        rendered = ", ".join(f"`{k}={v}`" for k, v in sorted(data["tags"].items()))
        lines.append(f"- **Tags**: {rendered}")
    # Read back rather than echoed: this is what AWS holds, which is the only
    # way to confirm the tags this skill set actually landed.
    if run.get("statusMessage"):
        lines.append(f"- **Status message**: {run['statusMessage']}")

    lines += ["", "## Tasks", ""]
    lines.append(
        f"{data['n_tasks']} task(s): {data['n_completed']} completed, {data['n_failed']} failed."
    )
    if data["n_failed"]:
        lines += ["", "### Failed tasks", ""]
        for task in data["tasks"]:
            if str(task.get("status", "")).upper() not in {"FAILED", "CANCELLED"}:
                continue
            lines.append(
                f"- **{task.get('name', task.get('taskId', 'unknown'))}** "
                f"(`{task.get('taskId', 'n/a')}`) — {task.get('status')}"
            )
            # The reason is the whole point of reading this section. AWS puts
            # it on the task, and omitting it sent users to the console for the
            # one fact they came for.
            reason = task.get("statusMessage") or task.get("failureReason")
            if reason:
                lines.append(f"  - {reason}")
            # Fetched by _enrich_failed_tasks and previously discarded: the log
            # location is the next thing a user reading this section needs.
            log_stream = task.get("logStream")
            if log_stream:
                group, _, stream = log_stream.partition(":log-stream:")
                group_name = group.split(":log-group:")[-1] if ":log-group:" in group else group
                lines.append(f"  - Logs: `{log_stream}`")
                lines.append(
                    f"    ```bash\n    aws logs get-log-events "
                    f"--log-group-name {group_name} --log-stream-name {stream}\n    ```"
                )

    if data.get("start_run_request") is not None:
        lines += ["", "## Submission", ""]
        verb = "Submitted" if data["submitted"] else "NOT submitted (estimate only)"
        lines.append(f"**{verb}.** The exact request:")
        lines += ["", "```json", json.dumps(data["start_run_request"], indent=2), "```"]
        cost = estimated_cost_line(str(data["start_run_request"].get("workflowId", "")))
        if cost and data["start_run_request"].get("workflowType") == "READY2RUN":
            lines += ["", f"**Estimated cost**: {cost}"]
        if not data["submitted"]:
            lines.append(
                "\nNo run was started and nothing was billed. Re-run with "
                "`--confirm-submit` to submit this request."
            )

    output_uri = run.get("outputUri")
    if output_uri:
        # HealthOmics writes each run under <outputUri>/<runId>/. Pointing the
        # command at <outputUri> alone pulls every run this account ever wrote
        # into one directory -- and this is the line users copy verbatim.
        run_id = str(run.get("id", "")).strip()
        prefix = f"{output_uri.rstrip('/')}/{run_id}/" if run_id else output_uri
        lines += [
            "", "## Fetching the outputs", "",
            "Outputs stay in S3. Bring this run's outputs down with:",
            "", "```bash",
            f"--download-outputs {run_id or '<run-id>'} --to ./run-{run_id or 'outputs'}/ "
            f"--confirm-download",
            "```",
            "", "or with the AWS CLI directly:", "", "```bash",
            f"aws s3 cp --recursive {prefix} ./run-{run_id or 'outputs'}/",
            "```",
        ]

    lines += _verification_lines(data)
    lines += ["", "## Provenance", ""] + _provenance_lines(data)
    lines += ["", generate_report_footer().strip(), ""]
    return "\n".join(lines)


_LIST_TITLES = {
    "runs": "AWS HealthOmics Runs",
    "workflows": "AWS HealthOmics Workflows",
    "run-groups": "AWS HealthOmics Run Groups",
    "run-caches": "AWS HealthOmics Run Caches",
    "workflow-versions": "AWS HealthOmics Workflow Versions",
    "workflow-search": "AWS HealthOmics Workflow Search",
    "workflow-recommendations": "AWS HealthOmics Workflow Recommendations",
}


def _list_markdown(data: dict[str, Any]) -> str:
    title = _LIST_TITLES[data["mode"]]
    header = generate_report_header(
        title=title,
        skill_name=SKILL_NAME,
        skill_version=SKILL_VERSION,
        extra_metadata={
            "Mode": "Synthetic offline demo" if data["demo"] else "Live AWS HealthOmics (boto3)",
            "Region": data["region"],
        },
    )
    lines = [header, "", f"## {title}", "", f"{data['n_items']} item(s).", ""]
    lines += ["| Id | Name | Status | Type |", "|---|---|---|---|"]
    for item in data["items"]:
        lines.append(
            f"| `{item.get('id', 'n/a')}` | {item.get('name', 'n/a')} | "
            f"{item.get('status', 'n/a')} | "
            f"{item.get('type', item.get('workflowType', 'n/a'))} |"
        )
    lines += ["", "## Provenance", ""] + _provenance_lines(data)
    lines += ["", generate_report_footer().strip(), ""]
    return "\n".join(lines)


# Per-entity table columns. A listing and a run report describe different
# things, so they get different tables rather than one shape pretending to fit
# both -- see _write_table.
_RUN_FIELDS = ("id", "name", "status", "workflowId", "creationTime", "stopTime")
_WORKFLOW_FIELDS = ("id", "name", "status", "type", "creationTime")
_WORKFLOW_SEARCH_FIELDS = ("id", "name", "status", "type", "matchScore", "creationTime")
_RUN_GROUP_FIELDS = ("id", "name", "maxCpus", "maxRuns", "maxDuration")
_RUN_CACHE_FIELDS = ("id", "name", "status", "cacheS3Uri")
_WORKFLOW_VERSION_FIELDS = ("workflowId", "versionName", "status", "creationTime")
_CHECK_FIELDS = ("name", "ok", "severity", "detail")
_PARAMETER_FIELDS = ("name", "default", "template")
_TAG_FIELDS = ("key", "value")


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({f: row.get(f, "") for f in fields})
    return path


def _write_tasks_csv(output_dir: Path, tasks: list[dict[str, Any]]) -> Path:
    return _write_csv(output_dir / "tables" / "tasks.csv", _TASK_FIELDS, tasks)


def _pad_transfer_report(data: dict[str, Any], region: str) -> dict[str, Any]:
    """Fit an upload / download / register result into the shared report shape.

    One ``write_bundle`` handles every mode, so each mode fills the same keys
    rather than growing a parallel writer per operation.
    """
    padded = {
        "region": region, "transport": "boto3", "demo": False,
        "run": {}, "workflow": {}, "tasks": [], "tags": {}, "items": [],
        "n_items": 0, "run_status": "N/A", "n_tasks": 0, "n_completed": 0,
        "n_failed": 0, "submitted": False, "start_run_request": None,
        "verification": None,
    }
    padded.update(data)
    return padded


_OUTPUT_FIELDS = ("key", "size", "etag", "is_md5", "sha256", "last_modified")
_UPLOAD_FIELDS = ("source", "key", "uri", "n_bytes")
_ZIP_MEMBER_FIELDS = ("archive_name", "source_path", "n_bytes", "sha256")


def _write_table(output_dir: Path, data: dict[str, Any]) -> Path:
    """Write the table for whatever this report is actually about.

    A --list-workflows bundle used to carry an empty tasks.csv: run-task headers
    over zero rows, while the workflows it had just fetched appeared only in
    report.md. That is worse than omitting the file, because a downstream reader
    sees a well-formed header and concludes the query returned nothing.
    """
    mode = data.get("mode")
    if mode == "runs":
        return _write_csv(output_dir / "tables" / "runs.csv", _RUN_FIELDS, data["items"])
    if mode == "workflows":
        return _write_csv(
            output_dir / "tables" / "workflows.csv", _WORKFLOW_FIELDS, data["items"]
        )
    if mode in {"workflow-search", "workflow-recommendations"}:
        return _write_csv(
            output_dir / "tables" / "workflows.csv", _WORKFLOW_SEARCH_FIELDS,
            data["items"],
        )
    if mode == "run-groups":
        return _write_csv(output_dir / "tables" / "run-groups.csv",
                          _RUN_GROUP_FIELDS, data["items"])
    if mode == "run-caches":
        return _write_csv(output_dir / "tables" / "run-caches.csv",
                          _RUN_CACHE_FIELDS, data["items"])
    if mode == "workflow-versions":
        return _write_csv(output_dir / "tables" / "workflow-versions.csv",
                          _WORKFLOW_VERSION_FIELDS, data["items"])
    if mode == "upload":
        return _write_csv(output_dir / "tables" / "uploads.csv", _UPLOAD_FIELDS,
                          data.get("uploaded_files", []))
    if mode == "download":
        return _write_csv(output_dir / "tables" / "downloads.csv",
                          ("key", "path", "etag"), data.get("downloaded_files", []))
    if mode in {"register", "register-version"}:
        return _write_csv(
            output_dir / "tables" / "definition.csv", _ZIP_MEMBER_FIELDS,
            (data.get("zip") or {}).get("members", []),
        )
    if mode == "check":
        return _write_csv(output_dir / "tables" / "checks.csv", _CHECK_FIELDS,
                          data.get("checks", []))
    if mode == "params-template":
        return _write_csv(output_dir / "tables" / "params-template.csv",
                          _PARAMETER_FIELDS, data.get("items", []))
    if mode in {"tag", "untag", "tags", "sync-tags"}:
        tags = data.get("tags") or data.get("desired_tags") or data.get("tagged") or {}
        rows = [{"key": key, "value": value} for key, value in sorted(tags.items())]
        if mode == "untag":
            rows = [{"key": key, "value": ""} for key in data.get("untagged", [])]
        return _write_csv(output_dir / "tables" / "tags.csv", _TAG_FIELDS, rows)
    if data.get("verification"):
        return _write_csv(
            output_dir / "tables" / "outputs.csv", _OUTPUT_FIELDS,
            data["verification"]["objects"],
        )
    return _write_tasks_csv(output_dir, data["tasks"])


def _warn_before_overwrite(output_dir: Path) -> None:
    if output_dir.exists() and any(output_dir.iterdir()):
        print(
            f"WARNING: {output_dir} already exists and is not empty; files may be overwritten.",
            file=sys.stderr,
        )


def _replay_args(mode: str | None, data: dict[str, Any]) -> list[Any]:
    """The flags that reproduce this bundle's own mode.

    Every non-run mode used to fall through to ``["--run-status", ""]`` --
    upload, download, register, and the newer tag/untag/register-version modes
    all replay a run-status query for a run that was never named. Real
    bundles from this session (an upload, a registration) both shipped that
    broken command. One switch per mode, so a new mode that forgets to extend
    this fails loudly (KeyError) rather than silently inheriting the wrong
    replay.
    """
    if mode in _LIST_MODES:
        flags = {
            "runs": ["--list-runs"],
            "workflows": ["--list-workflows"],
            "run-groups": ["--list-run-groups"],
            "run-caches": ["--list-run-caches"],
            "workflow-versions": [
                "--list-workflow-versions",
                data.get("workflow_id")
                or (data.get("items") or [{}])[0].get("workflowId")
                or "",
            ],
            "workflow-search": ["--search-workflows", str(data.get("query", ""))],
            "workflow-recommendations": [
                "--recommend-workflow",
                str(data.get("query", "")),
            ],
        }[mode]
        return flags
    if mode == "check":
        return ["--check"]
    if mode == "params-template":
        return [
            "--params-template",
            str(data.get("workflow_id", "")),
            *_workflow_type_arg(data),
        ]
    if mode == "upload":
        return ["--upload-inputs", *data.get("sources", []), "--to", data["destination"]]
    if mode == "download":
        return ["--download-outputs", str(data.get("run_id", "")), "--to", data["destination"]]
    if mode == "register":
        return ["--register", data["definition_path"], "--workflow-name", data["workflow_name"]]
    if mode == "register-version":
        return ["--register", data["definition_path"], "--workflow-id", data["workflow_id"],
                "--new-version-name", data["version_name"]]
    if mode == "tag":
        return ["--tag-run", str(data["run_id"]), "--tags",
                json.dumps(data.get("tagged", {}), sort_keys=True)]
    if mode == "untag":
        return ["--untag-run", str(data["run_id"]), "--tag-keys",
                *list(data.get("untagged", []))]
    if mode == "tags":
        return ["--list-tags", str(data["run_id"])]
    if mode == "sync-tags":
        return ["--sync-tags", str(data["run_id"]), "--tags",
                json.dumps(data.get("desired_tags", {}), sort_keys=True)]
    return ["--run-status", str(data["run"].get("id", ""))]


def _workflow_type_arg(data: dict[str, Any]) -> list[str]:
    workflow_type = data.get("workflow_type") or data.get("workflow", {}).get("type")
    return ["--workflow-type", str(workflow_type)] if workflow_type else []


def _replay_preflight_lines(data: dict[str, Any]) -> list[str]:
    """Minimal replay guard shipped in commands.sh."""
    mode = data.get("mode") or "run"
    return [
        'MANIFEST="$OUTPUT_DIR/reproducibility/replay_manifest.json"',
        'if [ ! -f "$MANIFEST" ]; then',
        '  echo "Missing replay manifest: $MANIFEST" >&2',
        "  exit 1",
        "fi",
        f"if ! grep -q '\"mode\": \"{mode}\"' \"$MANIFEST\"; then",
        f'  echo "Replay manifest does not describe mode {mode}" >&2',
        "  exit 1",
        "fi",
    ]


def _handoff_for_path(path: str) -> list[str]:
    lower = path.lower()
    if lower.endswith((".vcf", ".vcf.gz", ".bcf")):
        return ["variant-annotation", "vcf-annotator", "pharmgx-reporter"]
    if lower.endswith((".h5ad", ".loom", ".mtx", ".mtx.gz")):
        return ["scrna-orchestrator", "scrna-embedding"]
    if lower.endswith((".counts.tsv", ".counts.csv", ".tsv", ".csv")):
        return ["rnaseq-de", "proteomics-de"]
    if lower.endswith((".bam", ".cram")):
        return ["seq-wrangler", "multiqc-reporter"]
    if lower.endswith((".html", ".zip")):
        return ["multiqc-reporter"]
    return []


def _write_extra_artifacts(output_dir: Path, data: dict[str, Any]) -> list[Path]:
    """Write richer machine-readable artifacts beside the standard contract."""
    written: list[Path] = []
    repro_dir = output_dir / "reproducibility"
    repro_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "mode": data.get("mode") or "run",
        "region": data.get("region"),
        "run_id": data.get("run", {}).get("id") or data.get("run_id"),
        "workflow_id": data.get("workflow_id") or data.get("run", {}).get("workflowId"),
        "workflow_version_name": data.get("version_name") or data.get("workflow_version_name"),
        "request_id": data.get("start_run_request", {}).get("requestId")
        if isinstance(data.get("start_run_request"), dict) else None,
        "params_sha256": hashlib.sha256(
            json.dumps(
                data.get("start_run_request", {}).get("parameters", {}),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        if isinstance(data.get("start_run_request"), dict) else None,
    }
    manifest_path = repro_dir / "replay_manifest.json"
    write_text_lf(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    written.append(manifest_path)

    verification = data.get("verification") or {}
    outputs_payload: dict[str, Any] | None = None
    if verification.get("objects") is not None:
        outputs_payload = {
            "source": verification.get("source"),
            "depth": verification.get("depth"),
            "objects": verification.get("objects", []),
            "complete": verification.get("complete"),
        }
    elif data.get("mode") == "download":
        outputs_payload = {
            "source": data.get("source"),
            "destination": data.get("destination"),
            "objects": data.get("downloaded_files", []),
            "complete": not data.get("failures"),
        }
    if outputs_payload is not None:
        path = output_dir / "outputs.json"
        write_text_lf(path, json.dumps(outputs_payload, indent=2, default=str) + "\n")
        written.append(path)

        handoff_items = []
        for entry in outputs_payload.get("objects", []):
            local_path = entry.get("local_path") or entry.get("path")
            if not local_path:
                continue
            partners = _handoff_for_path(str(local_path))
            if partners:
                handoff_items.append({"path": local_path, "suggested_skills": partners})
        handoff = {
            "source": outputs_payload.get("source"),
            "items": handoff_items,
            "n_items": len(handoff_items),
        }
        handoff_path = output_dir / "handoff.json"
        write_text_lf(handoff_path, json.dumps(handoff, indent=2, default=str) + "\n")
        written.append(handoff_path)

    if data.get("mode") == "params-template":
        path = output_dir / "params.template.json"
        write_text_lf(
            path,
            json.dumps(data.get("params_template", {}), indent=2, sort_keys=True) + "\n",
        )
        written.append(path)

    return written


def write_bundle(
    output_dir: Path, data: dict[str, Any], *, warn_before_overwrite: bool = True
) -> dict[str, Any]:
    """Write the full ClawBio output contract."""
    from clawbio.common.reproducibility import (
        ReproCommand,
        ReproPath,
        write_checksums,
        write_environment_yml,
        write_portable_commands_sh,
    )

    if warn_before_overwrite:
        _warn_before_overwrite(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    report_path = output_dir / "report.md"
    write_text_lf(report_path, _report_markdown(data))

    mode = data.get("mode")
    table_path = _write_table(output_dir, data)
    extra_paths = _write_extra_artifacts(output_dir, data)
    args: list[Any] = _replay_args(mode, data)
    if data["demo"]:
        args = ["--demo"]
    args += ["--output", ReproPath(output_dir, anchor="output_dir")]

    commands_path = write_portable_commands_sh(
        output_dir,
        ReproCommand(
            script_path=_REL_SCRIPT,
            args=args,
            comment=(
                "Replays the local reporting step. A live replay additionally requires "
                "the same AWS account and execution role."
            ),
            preflight=_replay_preflight_lines(data),
        ),
        repo_root=_PROJECT_ROOT,
    )
    env_path = write_environment_yml(
        output_dir,
        env_name="clawbio-healthomics-bridge",
        pip_deps=["boto3>=1.34"],
        conda_deps=[],
        python_version="3.11",
    )

    if mode in _LIST_MODES:
        summary = {
            "kind": mode, "n_items": data["n_items"],
            "region": data["region"], "demo": data["demo"],
        }
        status = "LISTED"
    elif mode == "check":
        summary = {
            "kind": "check",
            "ok": bool(data.get("ok")),
            "n_checks": data.get("n_checks", 0),
            "n_failed": data.get("n_failed", 0),
            "n_warnings": data.get("n_warnings", 0),
            "region": data["region"],
            "demo": data["demo"],
        }
        status = "OK" if data.get("ok") else "FAILED"
    elif mode == "params-template":
        summary = {
            "kind": "params-template",
            "workflow_id": data.get("workflow_id"),
            "n_parameters": data.get("n_items", 0),
            "region": data["region"],
            "demo": data["demo"],
        }
        status = "TEMPLATE"
    elif mode in {"tag", "untag", "tags", "sync-tags"}:
        summary = {
            "kind": mode,
            "run_id": data.get("run_id"),
            "n_tags": len(
                data.get("tags")
                or data.get("desired_tags")
                or data.get("tagged")
                or data.get("untagged")
                or {}
            ),
            "region": data["region"],
            "demo": data["demo"],
        }
        status = "TAGGED" if mode in {"tag", "sync-tags"} else "TAGS"
    else:
        summary = {
            "run_id": data["run"].get("id"),
            "run_status": data["run_status"],
            "n_tasks": data["n_tasks"],
            "n_failed_tasks": data["n_failed"],
            "submitted": data["submitted"],
            "region": data["region"],
            "demo": data["demo"],
        }
        status = str(data["run_status"])

    result_path = write_result_json(
        output_dir=output_dir,
        skill=SKILL_NAME,
        version=SKILL_VERSION,
        summary=summary,
        data=data,
        datasets={"AWS HealthOmics": "synthetic offline fixture" if data["demo"] else "live account"},
        status=status,
        # Exit 0 means the skill produced a truthful report, not that the run
        # succeeded. The run's outcome is in `status`.
        ok=True,
    )
    write_checksums(
        [report_path, result_path, table_path, commands_path, env_path, *extra_paths],
        output_dir,
        anchor=output_dir,
    )
    return json.loads(Path(result_path).read_text(encoding="utf-8"))


def run_demo(output_dir: Path) -> dict[str, Any]:
    """Deterministic offline demo: no AWS account, no credentials, no boto3."""
    bundle = json.loads(_DEMO_BUNDLE.read_text(encoding="utf-8"))
    data = map_run_report(bundle, region="us-east-1", demo=True)
    return write_bundle(output_dir, data)


def _run_live(args: argparse.Namespace, output_dir: Path) -> dict[str, Any]:
    _warn_before_overwrite(output_dir)

    if args.check:
        params = {}
        if args.params:
            params, _ = _preflight.load_params_file(args.params)
        data = map_check_report(
            _preflight.run_preflight(args, params=params),
            region=args.region,
        )
        return write_bundle(output_dir, data, warn_before_overwrite=False)

    start_run_request: dict[str, Any] | None = None
    if args.start_run:
        params = json.loads(Path(args.params).read_text(encoding="utf-8"))
        check_remote_inputs(params, args.output_uri, args.allow_remote_inputs)
        request_id = derive_request_id(
            workflow_id=args.start_run, workflow_type=args.workflow_type,
            params=params, output_uri=args.output_uri,
            role_arn=args.role_arn, run_name=args.run_name,
        )
        start_run_request = build_start_run_request(
            workflow_id=args.start_run, workflow_type=args.workflow_type,
            params=params, output_uri=args.output_uri, role_arn=args.role_arn,
            run_name=args.run_name, request_id=request_id,
            storage_type=args.storage_type,
            storage_capacity=(
                normalise_storage_capacity(args.storage_capacity, announce=True)
                if args.storage_capacity else None
            ),
            cache_id=args.cache_id, cache_behavior=args.cache_behavior,
            run_group_id=args.run_group_id,
            workflow_version_name=args.workflow_version_name,
            tags=json.loads(args.run_tags) if args.run_tags else None,
        )
        if not args.confirm_submit:
            print(
                "ESTIMATE ONLY: no run was submitted and nothing was billed. "
                "Re-run with --confirm-submit to start this run.",
                file=sys.stderr,
            )
            data = map_run_report(
                {"run": {}, "workflow": {}, "tasks": []}, region=args.region,
                start_run_request=start_run_request, submitted=False,
            )
            return write_bundle(output_dir, data, warn_before_overwrite=False)

    # S3-only modes never construct an omics client: a transfer has nothing to
    # ask HealthOmics about.
    if args.upload_inputs:
        s3 = _s3.S3Operations(_boto=_s3.build_s3_client(args.region, args.profile))
        data = upload_run_inputs(
            client=s3, sources=list(args.upload_inputs), destination=args.to,
            acknowledged=args.allow_remote_inputs, confirmed=args.confirm_upload)
        return write_bundle(output_dir, _pad_transfer_report(data, args.region),
                            warn_before_overwrite=False)

    client = OmicsOperations(_boto=build_boto_client(args.region, args.profile))

    if args.search_workflows:
        filters: dict[str, Any] = {"type": args.workflow_type} if args.workflow_type else {}
        items = list_all(
            client=client,
            operation="ListWorkflows",
            limit=max(args.limit, 100),
            **filters,
        )
        matches = _recommendations.search_workflows(
            items, args.search_workflows, limit=args.limit
        )
        data = map_workflow_search_report(
            query=args.search_workflows,
            items=matches,
            kind="workflow-search",
            region=args.region,
        )
        return write_bundle(output_dir, data, warn_before_overwrite=False)

    if args.recommend_workflow:
        filters = {"type": args.workflow_type} if args.workflow_type else {}
        items = list_all(
            client=client,
            operation="ListWorkflows",
            limit=max(args.limit, 100),
            **filters,
        )
        recommendation = _recommendations.recommend_workflows(
            items, args.recommend_workflow, limit=args.limit
        )
        data = map_workflow_search_report(
            query=args.recommend_workflow,
            items=recommendation["recommendations"],
            kind="workflow-recommendations",
            region=args.region,
        )
        data["inferred_domains"] = recommendation["inferred_domains"]
        return write_bundle(output_dir, data, warn_before_overwrite=False)

    if args.params_template:
        lookup: dict[str, Any] = {"id": str(args.params_template)}
        if args.workflow_type:
            lookup["type"] = args.workflow_type
        if args.workflow_version_name:
            workflow = client.call(
                "GetWorkflowVersion",
                workflowId=str(args.params_template),
                versionName=args.workflow_version_name,
            )
        else:
            workflow = client.call("GetWorkflow", **lookup)
        data = map_params_template_report(
            _params_template.workflow_params_payload(workflow),
            region=args.region,
        )
        if not data.get("workflow_id"):
            data["workflow_id"] = args.params_template
        return write_bundle(output_dir, data, warn_before_overwrite=False)

    if args.download_outputs:
        run = client.call("GetRun", id=str(args.download_outputs))
        output_uri = run.get("outputUri")
        if not output_uri:
            raise OmicsCallError(
                f"Run {args.download_outputs} reports no outputUri, so there is "
                f"nothing to download."
            )
        s3 = _s3.S3Operations(_boto=_s3.build_s3_client(args.region, args.profile))
        data = download_run_outputs(
            client=s3, output_uri=output_uri, run_id=str(args.download_outputs),
            destination=Path(args.to), confirmed=args.confirm_download)
        return write_bundle(output_dir, _pad_transfer_report(data, args.region),
                            warn_before_overwrite=False)

    if args.register:
        template = (
            json.loads(args.parameter_template.read_text(encoding="utf-8"))
            if args.parameter_template else None
        )
        if args.workflow_id:
            data = register_workflow_version(
                client=client, workflow_id=args.workflow_id,
                definition=Path(args.register), additional_files=list(args.additional_files),
                version_name=args.new_version_name, description=args.description,
                parameter_template=template, confirmed=args.confirm_register,
                output_dir=output_dir)
        else:
            data = register_run_workflow(
                client=client, definition=Path(args.register),
                additional_files=list(args.additional_files), name=args.workflow_name,
                engine=args.engine, description=args.description,
                parameter_template=template, allow_duplicate=args.allow_duplicate_name,
                confirmed=args.confirm_register, output_dir=output_dir)
        return write_bundle(output_dir, _pad_transfer_report(data, args.region),
                            warn_before_overwrite=False)

    if args.list_run_groups:
        items = list_all(client=client, operation="ListRunGroups", limit=args.limit)
        data = map_list_report(items, kind="run-groups", region=args.region)
        return write_bundle(output_dir, data, warn_before_overwrite=False)

    if args.list_run_caches:
        items = list_all(client=client, operation="ListRunCaches", limit=args.limit)
        data = map_list_report(items, kind="run-caches", region=args.region)
        return write_bundle(output_dir, data, warn_before_overwrite=False)

    if args.list_workflow_versions:
        items = list_all(client=client, operation="ListWorkflowVersions",
                         limit=args.limit, workflowId=args.list_workflow_versions)
        data = map_list_report(items, kind="workflow-versions", region=args.region)
        data["workflow_id"] = args.list_workflow_versions
        return write_bundle(output_dir, data, warn_before_overwrite=False)

    if args.describe_run_group:
        group = describe_run_group(client=client, group_id=args.describe_run_group)
        data = map_list_report([group], kind="run-groups", region=args.region)
        return write_bundle(output_dir, data, warn_before_overwrite=False)

    if args.describe_run_cache:
        cache = describe_run_cache(client=client, cache_id=args.describe_run_cache)
        data = map_list_report([cache], kind="run-caches", region=args.region)
        return write_bundle(output_dir, data, warn_before_overwrite=False)

    if args.tag_run:
        tags = json.loads(args.tags)
        data = tag_run(client=client, run_id=args.tag_run, tags=tags)
        return write_bundle(output_dir, _pad_transfer_report(data, args.region),
                            warn_before_overwrite=False)

    if args.untag_run:
        data = untag_run(client=client, run_id=args.untag_run, keys=list(args.tag_keys))
        return write_bundle(output_dir, _pad_transfer_report(data, args.region),
                            warn_before_overwrite=False)

    if args.list_tags:
        data = list_run_tags(client=client, run_id=args.list_tags)
        return write_bundle(output_dir, _pad_transfer_report(data, args.region),
                            warn_before_overwrite=False)

    if args.sync_tags:
        tags = json.loads(args.tags)
        data = sync_run_tags(client=client, run_id=args.sync_tags, desired_tags=tags)
        return write_bundle(output_dir, _pad_transfer_report(data, args.region),
                            warn_before_overwrite=False)

    if args.list_runs:
        items = list_all(client=client, operation="ListRuns", limit=args.limit)
        data = map_list_report(items, kind="runs", region=args.region)
        return write_bundle(output_dir, data, warn_before_overwrite=False)

    if args.list_workflows:
        filters: dict[str, Any] = {"type": args.workflow_type} if args.workflow_type else {}
        items = list_all(
            client=client, operation="ListWorkflows", limit=args.limit, **filters
        )
        data = map_list_report(items, kind="workflows", region=args.region)
        return write_bundle(output_dir, data, warn_before_overwrite=False)

    if args.start_run:
        result = submit_run(client=client, request=start_run_request, confirmed=True)
        run_id = str(result["response"].get("id", ""))
    else:
        run_id = args.run_status

    if args.wait:
        print(
            f"Waiting for run {run_id} to reach a terminal state "
            f"(polling every {args.poll_interval:.0f}s). The run keeps billing "
            f"while this waits; Ctrl-C stops watching, not the run.",
            file=sys.stderr,
        )
        wait_for_run(
            client=client, run_id=run_id,
            poll_seconds=args.poll_interval,
            timeout_seconds=args.wait_timeout_seconds,
        )

    bundle = fetch_run_bundle(client=client, run_id=run_id)

    verification = None
    if args.verify_outputs:
        output_uri = (bundle.get("run") or {}).get("outputUri")
        if output_uri:
            s3 = _s3.S3Operations(_boto=_s3.build_s3_client(args.region, args.profile))
            verification = verify_run_outputs(
                client=s3, output_uri=output_uri, run_id=run_id,
                depth=args.verify_outputs,
                destination=Path(args.to) if args.to else None,
                confirmed=args.confirm_download)
        else:
            print(
                f"WARNING: run {run_id} reports no outputUri; nothing to verify.",
                file=sys.stderr,
            )

    data = map_run_report(
        bundle, region=args.region, start_run_request=start_run_request,
        submitted=bool(args.start_run), verification=verification,
    )
    return write_bundle(output_dir, data, warn_before_overwrite=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="healthomics_bridge.py",
        description=(
            "Submit, monitor and import AWS HealthOmics runs via boto3, behind an "
            "allow-listed client, an egress gate and a cost gate."
        ),
    )
    parser.add_argument("--demo", action="store_true", help="Offline demo; no AWS account needed")
    parser.add_argument("--check", action="store_true",
                        help="Run read-only preflight checks and exit before any live action")
    parser.add_argument(
        "--output", type=Path, default=Path("output/healthomics"), help="Output directory"
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list-runs", action="store_true", help="List recent runs (read-only)")
    mode.add_argument("--list-workflows", action="store_true", help="List workflows (read-only)")
    mode.add_argument("--run-status", metavar="RUN_ID", help="Report one run (read-only)")
    mode.add_argument("--start-run", metavar="WORKFLOW_ID", help="Submit a run (gated)")
    mode.add_argument("--upload-inputs", nargs="+", type=Path, metavar="PATH",
                      help="Upload a run's input files to S3 (gated)")
    mode.add_argument("--download-outputs", metavar="RUN_ID",
                      help="Download one run's outputs from S3 (gated)")
    mode.add_argument("--register", metavar="DEFINITION",
                      help="Register a WDL/CWL/Nextflow definition as a private "
                           "workflow (gated). Dry-run without --confirm-register. "
                           "With --workflow-id instead of --workflow-name, adds a "
                           "version to an existing workflow.")
    mode.add_argument("--list-run-groups", action="store_true",
                      help="List run groups referenced by --run-group-id (read-only)")
    mode.add_argument("--list-run-caches", action="store_true",
                      help="List run caches referenced by --cache-id (read-only)")
    mode.add_argument("--describe-run-group", metavar="GROUP_ID",
                      help="One run group's own detail (read-only)")
    mode.add_argument("--describe-run-cache", metavar="CACHE_ID",
                      help="One run cache's own detail (read-only)")
    mode.add_argument("--list-workflow-versions", metavar="WORKFLOW_ID",
                      help="List a workflow's versions (read-only)")
    mode.add_argument("--search-workflows", metavar="QUERY",
                      help="Search workflows by name, description, id and type")
    mode.add_argument("--recommend-workflow", metavar="TASK",
                      help="Recommend workflows for a plain-English task")
    mode.add_argument("--params-template", metavar="WORKFLOW_ID",
                      help="Write a starter params.template.json for a workflow")
    mode.add_argument("--tag-run", metavar="RUN_ID",
                      help="Set tags on an existing run (gated). Needs --tags.")
    mode.add_argument("--untag-run", metavar="RUN_ID",
                      help="Remove tags from a run by key (gated). Needs --tag-keys.")
    mode.add_argument("--list-tags", metavar="RUN_ID",
                      help="List tags on a run (read-only)")
    mode.add_argument("--sync-tags", metavar="RUN_ID",
                      help="Converge a run's tags to --tags JSON")

    parser.add_argument(
        "--workflow-type", choices=["PRIVATE", "READY2RUN"],
        help=(
            "Workflow type. REQUIRED with --start-run: AWS needs it to resolve a "
            "Ready2Run workflow, and being explicit avoids the not-found error a "
            "missing type produces. Also filters --list-workflows."
        ),
    )
    parser.add_argument("--params", type=Path, help="JSON parameters file (with --start-run)")
    parser.add_argument("--output-uri", help="S3 URI for run outputs (with --start-run)")
    parser.add_argument("--role-arn", help="HealthOmics execution role ARN (with --start-run)")
    parser.add_argument("--run-name", help="Run name (with --start-run)")
    parser.add_argument("--to", help="Destination: an s3:// URI for --upload-inputs, "
                                     "a local directory for --download-outputs")
    parser.add_argument("--confirm-upload", action="store_true",
                        help="Actually upload. Without it --upload-inputs is a dry run.")
    parser.add_argument("--confirm-download", action="store_true",
                        help="Actually download. S3 egress is billable.")
    parser.add_argument(
        "--verify-outputs", nargs="?", const="manifest", default=None,
        choices=["manifest", "deep"],
        help="With --run-status: record what the run produced. 'manifest' lists "
             "sizes and ETags and moves no bytes; 'deep' downloads and computes "
             "real SHA-256 checksums (needs --confirm-download).",
    )
    parser.add_argument("--workflow-name", help="Name for the new workflow (with --register)")
    parser.add_argument("--engine", choices=sorted(_registration.SUPPORTED_ENGINES),
                        help="Workflow engine. Inferred from the definition's "
                             "extension (.wdl/.cwl/.nf) when omitted.")
    parser.add_argument("--additional-files", nargs="+", type=Path, default=[],
                        help="Extra files for a multi-file WDL/CWL bundle")
    parser.add_argument("--description", help="Workflow description (with --register)")
    parser.add_argument("--parameter-template", type=Path,
                        help="JSON parameter template (with --register)")
    parser.add_argument("--allow-duplicate-name", action="store_true",
                        help="Register even though a workflow of this name exists")
    parser.add_argument("--confirm-register", action="store_true",
                        help="Actually create the workflow (or version). Without "
                             "it --register is a dry run.")
    parser.add_argument("--workflow-id", help="Existing workflow id to add a "
                                              "version to (with --register)")
    parser.add_argument("--new-version-name",
                        help="Version name to create (with --register --workflow-id)")
    parser.add_argument("--tags", help="JSON tags to set (with --tag-run or --sync-tags)")
    parser.add_argument("--tag-keys", nargs="+", help="Tag keys to remove (with --untag-run)")
    parser.add_argument("--storage-type", choices=["STATIC", "DYNAMIC"],
                        help="Omit to take AWS's preferred DYNAMIC default")
    parser.add_argument("--storage-capacity", type=int,
                        help="Run storage in GiB; only meaningful with --storage-type STATIC")
    parser.add_argument("--cache-id", help="Run cache to reuse task results from")
    parser.add_argument("--cache-behavior", choices=["CACHE_ALWAYS", "CACHE_ON_FAILURE"])
    parser.add_argument("--run-group-id", help="Run group, for concurrency and cost caps")
    parser.add_argument("--workflow-version-name", help="Pin the workflow version to run")
    parser.add_argument(
        "--run-tags", metavar="JSON",
        help=(
            "Cost-allocation tags for the RUN as JSON, e.g. '{\"team\":\"genomics\"}'. "
            "Per-run cost allocation, applied at submission time."
        ),
    )

    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    parser.add_argument("--limit", type=int, default=25, help="Max results for list modes")
    parser.add_argument(
        "--wait", action="store_true",
        help="Poll until the run reaches a terminal state before reporting. "
             "Watching does not stop billing, and Ctrl-C stops watching, not the run.",
    )
    parser.add_argument(
        "--poll-interval", type=float, default=30.0,
        help="Seconds between --wait polls (default: 30)",
    )
    parser.add_argument(
        "--wait-timeout-seconds", type=float, default=86_400.0,
        help="Give up watching after this long (default: 24h). The run continues.",
    )
    parser.add_argument(
        "--allow-remote-inputs", action="store_true",
        help="Acknowledge that submitting a run sends genomic data to AWS",
    )
    parser.add_argument(
        "--confirm-submit", action="store_true",
        help="Actually submit. Without it, --start-run only estimates and bills nothing.",
    )
    return parser


def _selected_mode(args: argparse.Namespace) -> str:
    for attr, label in (
        ("check", "check"),
        ("list_runs", "runs"),
        ("list_workflows", "workflows"),
        ("run_status", "run"),
        ("start_run", "start-run"),
        ("upload_inputs", "upload"),
        ("download_outputs", "download"),
        ("register", "register"),
        ("list_run_groups", "run-groups"),
        ("list_run_caches", "run-caches"),
        ("describe_run_group", "run-group"),
        ("describe_run_cache", "run-cache"),
        ("list_workflow_versions", "workflow-versions"),
        ("search_workflows", "workflow-search"),
        ("recommend_workflow", "workflow-recommendations"),
        ("params_template", "params-template"),
        ("tag_run", "tag"),
        ("untag_run", "untag"),
        ("list_tags", "tags"),
        ("sync_tags", "sync-tags"),
    ):
        if getattr(args, attr, None):
            return label
    return "unknown"


def _write_error_bundle(output_dir: Path, args: argparse.Namespace, exc: BaseException) -> None:
    """Best-effort error report with a stable machine-readable code."""
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = _error_codes.error_payload(
        exc,
        mode=_selected_mode(args),
        region=getattr(args, "region", None),
    )
    report = generate_report_header(
        title="AWS HealthOmics — Error",
        skill_name=SKILL_NAME,
        skill_version=SKILL_VERSION,
        extra_metadata={
            "Mode": payload["mode"],
            "Region": payload["region"],
            "Error code": payload["error_code"],
        },
    )
    report += (
        f"## Error\n\n`{payload['error_code']}`\n\n{payload['message']}\n\n"
        + generate_report_footer()
    )
    write_text_lf(output_dir / "report.md", report)
    write_result_json(
        output_dir=output_dir,
        skill=SKILL_NAME,
        version=SKILL_VERSION,
        summary={
            "kind": "error",
            "error_code": payload["error_code"],
            "mode": payload["mode"],
            "region": payload["region"],
        },
        data=payload,
        datasets={"AWS HealthOmics": "not reached or failed"},
        status=payload["error_code"],
        ok=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    output_dir = args.output.expanduser().resolve()

    if args.demo:
        result = run_demo(output_dir)
        print(json.dumps(result["summary"], indent=2))
        return 0

    if not any([args.check, args.list_runs, args.list_workflows, args.run_status, args.start_run,
                args.upload_inputs, args.download_outputs, args.register,
                args.list_run_groups, args.list_run_caches, args.describe_run_group,
                args.describe_run_cache, args.list_workflow_versions,
                args.search_workflows, args.recommend_workflow, args.params_template,
                args.tag_run, args.untag_run, args.list_tags, args.sync_tags]):
        parser.error(
            "choose one of --demo, --check, --list-runs, --list-workflows, --run-status, "
            "--start-run, --upload-inputs, --download-outputs, --register, "
            "--list-run-groups, --list-run-caches, --describe-run-group, "
            "--describe-run-cache, --list-workflow-versions, --search-workflows, "
            "--recommend-workflow, --params-template, --tag-run, --untag-run, "
            "--list-tags or --sync-tags"
        )

    # Mode-scoped flags rejected outside their mode, so a misplaced flag is a
    # loud error rather than a silently ignored one.
    for flag, value, owner in (
        ("--workflow-name", args.workflow_name, "--register"),
        ("--engine", args.engine, "--register"),
        ("--additional-files", args.additional_files, "--register"),
        ("--description", args.description, "--register"),
        ("--parameter-template", args.parameter_template, "--register"),
        ("--allow-duplicate-name", args.allow_duplicate_name, "--register"),
        ("--confirm-register", args.confirm_register, "--register"),
        ("--workflow-id", args.workflow_id, "--register"),
        ("--new-version-name", args.new_version_name, "--register"),
        ("--confirm-upload", args.confirm_upload, "--upload-inputs"),
        ("--tag-keys", args.tag_keys, "--untag-run"),
    ):
        owned = {"--register": args.register, "--upload-inputs": args.upload_inputs,
                 "--tag-run": args.tag_run, "--untag-run": args.untag_run}[owner]
        if value and not owned:
            parser.error(f"{flag} only applies to {owner}")

    if args.tags and not (args.tag_run or args.sync_tags):
        parser.error("--tags only applies to --tag-run or --sync-tags")
    if args.tag_run and not args.tags:
        parser.error("--tag-run requires --tags '{\"key\":\"value\"}'")
    if args.sync_tags and not args.tags:
        parser.error("--sync-tags requires --tags '{\"key\":\"value\"}'")
    if args.untag_run and not args.tag_keys:
        parser.error("--untag-run requires --tag-keys KEY [KEY ...]")

    if args.upload_inputs:
        if not args.to:
            parser.error("--upload-inputs requires --to s3://bucket/prefix/")
        missing_sources = [str(p) for p in args.upload_inputs if not p.expanduser().is_file()]
        if missing_sources:
            parser.error(f"input file not found: {', '.join(missing_sources)}")

    if args.download_outputs and not args.to:
        parser.error("--download-outputs requires --to <local-directory>")

    if args.verify_outputs and not args.run_status:
        parser.error("--verify-outputs only applies to --run-status")

    if args.verify_outputs == "deep" and not args.confirm_download:
        parser.error(
            "--verify-outputs deep downloads every output to hash it, and S3 "
            "egress is billable. Add --confirm-download, or use "
            "--verify-outputs manifest which moves no bytes."
        )

    if args.register:
        if args.workflow_id:
            if args.workflow_name:
                parser.error("--register takes --workflow-name (new workflow) or "
                             "--workflow-id (new version of an existing one), not both")
            if not args.new_version_name:
                parser.error("--register --workflow-id requires --new-version-name")
        elif not args.workflow_name:
            parser.error("--register requires --workflow-name, or --workflow-id "
                         "plus --new-version-name to version an existing workflow")
        if not Path(args.register).expanduser().is_file():
            parser.error(f"definition not found: {args.register}")
        for extra in args.additional_files:
            if not extra.expanduser().is_file():
                parser.error(f"additional file not found: {extra}")
        if args.parameter_template and not args.parameter_template.expanduser().is_file():
            parser.error(f"parameter template not found: {args.parameter_template}")

    if args.start_run:
        missing = [
            flag for flag, value in (
                ("--params", args.params), ("--output-uri", args.output_uri),
                ("--role-arn", args.role_arn), ("--run-name", args.run_name),
                # Required rather than defaulted: guessing PRIVATE would make a
                # Ready2Run submission fail with a bare not-found error, which
                # is exactly the confusion this skill exists to avoid.
                ("--workflow-type", args.workflow_type),
            ) if not value
        ]
        if missing:
            parser.error(f"--start-run requires {', '.join(missing)}")
        if not args.params.exists():
            parser.error(f"params file not found: {args.params}")
        if args.storage_capacity is not None and args.storage_type != "STATIC":
            parser.error(
                "--storage-capacity only applies to STATIC run storage; add "
                "--storage-type STATIC, or drop it and take the DYNAMIC default"
            )
        if args.cache_behavior and not args.cache_id:
            parser.error("--cache-behavior requires --cache-id")

    try:
        result = _run_live(args, output_dir)
    except Exception as exc:
        try:
            _write_error_bundle(output_dir, args, exc)
        except OSError:
            pass
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
