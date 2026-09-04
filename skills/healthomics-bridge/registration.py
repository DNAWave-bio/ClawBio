"""Register a WDL / CWL / Nextflow definition as a private HealthOmics workflow.

Registration is cheap, reversible and squarely in this skill's own domain: a
run needs a workflow, and requiring the AWS CLI for that one step left a hole
in the middle of the lifecycle. It is gated behind ``--confirm-register`` for
the same reason ``StartRun`` is gated behind ``--confirm-submit`` -- it creates
a real resource that persists in the account until someone deletes it -- but it
bills nothing.

WHAT THIS CANNOT DO, STATED UP FRONT. There is no lint or validate operation
anywhere in the HealthOmics API (verified against botocore's service model), so
this cannot check a definition before creating it. AWS validates server-side
instead: the workflow is created, then settles on ACTIVE or FAILED. A FAILED
registration is reported as exactly that. Anything claiming to have validated
your WDL before upload would be inventing a capability the API does not have.

The one thing this CAN pin honestly is the definition archive, because it
builds those bytes itself rather than receiving them from AWS. That is why the
ZIP is byte-for-byte deterministic -- an sha256 over bytes whose content
depends on the local clock or platform would pin nothing.
"""

from __future__ import annotations

import time
import zipfile
from pathlib import Path
from typing import Any

from clawbio.common.checksums import sha256_file
from omics_client import OmicsClient

# CreateWorkflow's definitionZip ceiling. Larger definitions go through
# definitionUri (an S3 object) instead, which this skill does not wrap.
_MAX_DEFINITION_ZIP_BYTES = 4 * 1024 * 1024

# The earliest timestamp the ZIP format can represent. Fixed so the archive
# does not change every time the clock does.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

# Verified against botocore: CreateWorkflow's `engine` enum.
SUPPORTED_ENGINES = frozenset({"WDL", "CWL", "NEXTFLOW", "WDL_LENIENT"})

_ENGINE_BY_SUFFIX = {".wdl": "WDL", ".cwl": "CWL", ".nf": "NEXTFLOW"}

# A workflow that has settled: polling further tells you nothing new.
_TERMINAL_WORKFLOW_STATUSES = frozenset({"ACTIVE", "FAILED", "INACTIVE", "DELETED"})

_NAME_SCAN_MAX_PAGES = 5
_NAME_SCAN_PAGE_SIZE = 100


def infer_engine(definition: Path) -> str:
    """Map a definition's extension to a HealthOmics engine.

    Refuses rather than guessing: a wrong engine produces an AWS-side failure
    whose message says nothing about the real problem.
    """
    engine = _ENGINE_BY_SUFFIX.get(Path(definition).suffix.lower())
    if engine is None:
        raise ValueError(
            f"Cannot infer an engine from {Path(definition).name!r}. Expected one of "
            f"{', '.join(sorted(_ENGINE_BY_SUFFIX))}, or pass --engine explicitly "
            f"({', '.join(sorted(SUPPORTED_ENGINES))})."
        )
    return engine


def resolve_engine(definition: Path, explicit: str | None) -> str:
    """The explicit engine when given, otherwise the inferred one."""
    if explicit:
        engine = explicit.upper()
        if engine not in SUPPORTED_ENGINES:
            raise ValueError(
                f"{explicit!r} is not a HealthOmics engine. AWS accepts: "
                f"{', '.join(sorted(SUPPORTED_ENGINES))}."
            )
        return engine
    return infer_engine(definition)


def resolve_zip_members(
    definition: Path, additional_files: list[Path]
) -> list[tuple[str, Path]]:
    """Decide each file's name inside the archive, refusing collisions.

    Additional files that live under the definition's own directory keep their
    relative path, so a WDL's ``import "tasks/qc.wdl"`` still resolves once AWS
    unpacks the archive. Anything from elsewhere enters at its bare basename.
    """
    definition = Path(definition).expanduser().resolve()
    base_dir = definition.parent
    members: dict[str, Path] = {definition.name: definition}

    for extra in additional_files:
        extra = Path(extra).expanduser().resolve()
        try:
            archive_name = str(extra.relative_to(base_dir))
        except ValueError:
            archive_name = extra.name
        existing = members.get(archive_name)
        if existing is not None and existing != extra:
            raise ValueError(
                f"Two different files would both enter the archive as "
                f"{archive_name!r}: {existing} and {extra}. A shadowed task file "
                f"is a run that fails inside AWS for a reason invisible here."
            )
        members[archive_name] = extra

    return sorted(members.items())


def build_definition_zip(
    *, members: list[tuple[str, Path]], destination: Path
) -> dict[str, Any]:
    """Write a byte-for-byte reproducible definition archive.

    Four things are pinned, because each is otherwise a source of variance that
    would make the archive's sha256 meaningless as a claim:

    * ``ZIP_STORED`` -- no compression, so zlib's version does not matter
    * a fixed 1980 epoch, so the local clock does not matter
    * a fixed 0o644 mode and ``create_system = 3`` (unix), so the build
      platform does not matter
    * ``writestr`` rather than ``write``, which would embed the source mtime
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    total = sum(path.stat().st_size for _, path in members)
    if total > _MAX_DEFINITION_ZIP_BYTES:
        raise ValueError(
            f"Definition archive would be {total:,} bytes, over CreateWorkflow's "
            f"{_MAX_DEFINITION_ZIP_BYTES:,}-byte definitionZip limit. Trim "
            f"--additional-files, or register from S3 with definitionUri "
            f"(not wrapped by this skill)."
        )

    entries: list[dict[str, Any]] = []
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as zf:
        for archive_name, source in members:
            data = source.read_bytes()
            info = zipfile.ZipInfo(archive_name, date_time=_ZIP_EPOCH)
            info.external_attr = 0o644 << 16
            info.create_system = 3
            zf.writestr(info, data)
            entries.append(
                {
                    "archive_name": archive_name,
                    "source_path": str(source),
                    "n_bytes": len(data),
                    "sha256": sha256_file(source),
                }
            )

    return {
        "path": str(destination),
        "sha256": sha256_file(destination),
        "n_bytes": destination.stat().st_size,
        "compression": "stored",
        "members": entries,
    }


def find_workflows_named(
    *, client: OmicsClient, name: str,
    max_pages: int = _NAME_SCAN_MAX_PAGES, page_size: int = _NAME_SCAN_PAGE_SIZE,
) -> tuple[list[dict[str, Any]], bool]:
    """Private workflows already carrying this name, and whether the scan
    stopped early.

    The truncation flag is load-bearing: "no collision found" must never be
    reported from a partial read.
    """
    matches: list[dict[str, Any]] = []
    token: str | None = None
    for _ in range(max_pages):
        kwargs: dict[str, Any] = {"type": "PRIVATE", "maxResults": page_size}
        if token:
            kwargs["startingToken"] = token
        response = client.call("ListWorkflows", **kwargs)
        for item in response.get("items", []) or []:
            if item.get("name") == name:
                matches.append(item)
        token = response.get("nextToken")
        if not token:
            return matches, False
    return matches, True


def assert_workflow_name_is_free(
    *, client: OmicsClient, name: str, allow_duplicate: bool
) -> list[dict[str, Any]]:
    """Refuse a duplicate name unless the caller insists.

    HealthOmics does not enforce unique workflow names -- a second
    CreateWorkflow with the same name silently forks a second workflow with a
    different id, and every later "run my-wf" becomes ambiguous.
    """
    matches, truncated = find_workflows_named(client=client, name=name)
    if matches and not allow_duplicate:
        ids = ", ".join(str(m.get("id")) for m in matches)
        raise ValueError(
            f"A PRIVATE workflow named {name!r} already exists (id {ids}). "
            f"HealthOmics does not enforce unique names, so creating another "
            f"would silently fork it. Re-run with --allow-duplicate-name if that "
            f"is genuinely what you want."
        )
    if truncated and not matches and not allow_duplicate:
        raise ValueError(
            f"Whether {name!r} is already taken could not be checked: this "
            f"account has more private workflows than the name scan reads. "
            f"Re-run with --allow-duplicate-name to proceed anyway."
        )
    return matches


def build_create_workflow_request(
    *, name: str, engine: str, zip_path: Path, request_id: str,
    description: str | None = None, parameter_template: dict[str, Any] | None = None,
    tags: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble the exact CreateWorkflow payload, so it can be shown before it
    is sent. Keys are AWS's own camelCase, used directly."""
    request: dict[str, Any] = {
        "name": name,
        "engine": engine,
        "definitionZip": Path(zip_path).read_bytes(),
        "requestId": request_id,
    }
    optional = {
        "description": description,
        "parameterTemplate": parameter_template,
        "tags": tags,
    }
    request.update({k: v for k, v in optional.items() if v})
    return request


def poll_workflow_until_settled(
    *, client: OmicsClient, workflow_id: str,
    poll_seconds: float = 5.0, timeout_seconds: float = 300.0,
) -> dict[str, Any]:
    """Poll GetWorkflow until the workflow settles.

    Unconditional -- there is no ``--wait`` to opt into -- because unlike a
    billable run, waiting here costs nothing and the workflow id is useless
    until it resolves. On timeout this warns and returns the last snapshot
    rather than raising: the resource exists either way, and the caller needs
    its id to do anything about it.
    """
    deadline = time.monotonic() + timeout_seconds
    last_status = ""
    while True:
        workflow = client.call("GetWorkflow", id=str(workflow_id), type="PRIVATE")
        status = str(workflow.get("status", "")).upper()
        if status != last_status:
            print(
                f"[{time.strftime('%H:%M:%S')}] workflow {workflow_id}: {status}",
                file=__import__("sys").stderr,
            )
            last_status = status
        if status in _TERMINAL_WORKFLOW_STATUSES:
            return workflow
        if time.monotonic() >= deadline:
            print(
                f"WARNING: workflow {workflow_id} is still {status} after "
                f"{timeout_seconds:.0f}s. It exists; check it with "
                f"`aws omics get-workflow --id {workflow_id}`.",
                file=__import__("sys").stderr,
            )
            return workflow
        time.sleep(poll_seconds)


def register_workflow(
    *, client: OmicsClient, request: dict[str, Any]
) -> dict[str, Any]:
    """Create the workflow. Only ever reached past --confirm-register."""
    return client.call("CreateWorkflow", **request)
