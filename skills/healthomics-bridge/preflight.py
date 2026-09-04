"""Read-only preflight checks for healthomics-bridge."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any


_ROLE_ARN_RE = re.compile(r"^arn:aws(-[a-z]+)?:iam::\d{12}:role/.+")
_ECR_RE = re.compile(r"\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/[^\s\"']+")


def _check(name: str, ok: bool, detail: str, severity: str = "error") -> dict[str, Any]:
    return {"name": name, "ok": ok, "severity": severity, "detail": detail}


def _walk_strings(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(_walk_strings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_walk_strings(item))
    return found


def collect_container_images(params: dict[str, Any]) -> list[str]:
    """Best-effort discovery of container references embedded in params."""
    images: set[str] = set()
    for text in _walk_strings(params):
        if _ECR_RE.search(text):
            images.add(_ECR_RE.search(text).group(0))  # type: ignore[union-attr]
        elif text.startswith(("docker://", "public.ecr.aws/", "quay.io/", "ghcr.io/")):
            images.add(text.removeprefix("docker://"))
    return sorted(images)


def load_params_file(path: Path | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read a params JSON file for preflight without hiding parse failures."""
    if path is None:
        return {}, _check("params_file", True, "No params file supplied.", "warning")
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        return {}, _check("params_file", False, f"Params file not found: {resolved}")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {}, _check("params_file", False, f"Params JSON is invalid: {exc}")
    if not isinstance(payload, dict):
        return {}, _check("params_file", False, "Params JSON must be an object.")
    return payload, _check("params_file", True, f"Read {resolved}.")


def run_preflight(args: Any, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run checks that do not mutate AWS state."""
    checks: list[dict[str, Any]] = []
    checks.append(
        _check(
            "boto3",
            importlib.util.find_spec("boto3") is not None,
            "boto3 import is available." if importlib.util.find_spec("boto3") else
            "boto3 is not installed; live modes need `uv pip install boto3`.",
            "warning",
        )
    )
    checks.append(_check("region", bool(getattr(args, "region", None)), f"Region: {getattr(args, 'region', None) or 'missing'}"))
    profile = getattr(args, "profile", None)
    checks.append(_check("profile", True, f"Profile: {profile or 'boto3 default chain'}", "warning"))

    local_params = params if params is not None else {}
    if getattr(args, "params", None):
        local_params, params_check = load_params_file(getattr(args, "params"))
        checks.append(params_check)

    if getattr(args, "start_run", None):
        checks.append(_check("workflow_id", True, f"Workflow id: {args.start_run}"))
        checks.append(_check("workflow_type", bool(getattr(args, "workflow_type", None)), "Workflow type supplied." if getattr(args, "workflow_type", None) else "Missing --workflow-type."))
        role_arn = getattr(args, "role_arn", "") or ""
        checks.append(_check("role_arn", bool(_ROLE_ARN_RE.match(role_arn)), "Role ARN shape is valid." if _ROLE_ARN_RE.match(role_arn) else "Role ARN does not look like arn:aws:iam::<account>:role/<name>."))
        output_uri = getattr(args, "output_uri", "") or ""
        checks.append(_check("output_uri", output_uri.startswith("s3://"), f"Output URI: {output_uri or 'missing'}"))
        remote_paths = sorted(
            {text for text in _walk_strings(local_params) if "://" in text}
            | ({output_uri} if output_uri else set())
        )
        checks.append(
            _check(
                "egress_acknowledgement",
                not remote_paths or bool(getattr(args, "allow_remote_inputs", False)),
                "Remote paths acknowledged." if getattr(args, "allow_remote_inputs", False) else
                f"Remote paths found but --allow-remote-inputs is absent: {', '.join(remote_paths[:5])}",
            )
        )
        images = collect_container_images(local_params)
        checks.append(
            _check(
                "container_images",
                True,
                "Container references found: " + ", ".join(images) if images else
                "No obvious container image references found in params.",
                "warning",
            )
        )

    if getattr(args, "register", None):
        definition = Path(args.register).expanduser()
        checks.append(_check("definition", definition.is_file(), f"Definition: {definition}"))
        checks.append(
            _check(
                "register_target",
                bool(getattr(args, "workflow_name", None) or (getattr(args, "workflow_id", None) and getattr(args, "new_version_name", None))),
                "Registration target is named.",
            )
        )

    failed = [c for c in checks if not c["ok"] and c["severity"] == "error"]
    warnings = [c for c in checks if not c["ok"] and c["severity"] == "warning"]
    return {
        "mode": "check",
        "ok": not failed,
        "checks": checks,
        "n_checks": len(checks),
        "n_failed": len(failed),
        "n_warnings": len(warnings),
    }
