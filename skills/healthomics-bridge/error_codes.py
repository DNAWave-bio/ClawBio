"""Stable error codes for healthomics-bridge reports."""

from __future__ import annotations

import json
from typing import Any


ERROR_PATTERNS: tuple[tuple[str, tuple[tuple[str, ...], ...]], ...] = (
    ("EGRESS_REFUSED", (("allow-remote-inputs",), ("data will leave",), ("egress",))),
    ("MISSING_BOTO3", (("live healthomics calls need boto3",), ("live s3 transfers need boto3",))),
    ("S3_ACCESS_DENIED", (("s3 access denied",), ("accessdenied",), ("access denied",))),
    ("WORKFLOW_NOT_FOUND", (("workflow", "not found"), ("resourcenotfoundexception", "workflow"))),
    ("RUN_NOT_FOUND", (("run", "not found"), ("resourcenotfoundexception", "run"))),
    ("REGISTRATION_FAILED", (("failed to register",), ("workflow failed",))),
    ("PARAMS_INVALID", (("json",), ("parameters",), ("params",))),
    ("OUTPUT_UNAVAILABLE", (("no outputuri",), ("nothing to download",))),
    ("OPERATION_NOT_ALLOWED", (("not in this skill's allowlist",), ("is refused",))),
)


def error_code_for_exception(exc: BaseException) -> str:
    """Map an exception message to a stable, machine-readable code."""
    text = str(exc).lower()
    for code, alternatives in ERROR_PATTERNS:
        if any(all(needle in text for needle in needles) for needles in alternatives):
            return code
    if isinstance(exc, json.JSONDecodeError):
        return "PARAMS_INVALID"
    if isinstance(exc, OSError):
        return "IO_ERROR"
    return "HEALTHOMICS_BRIDGE_ERROR"


def error_payload(exc: BaseException, *, mode: str | None, region: str | None) -> dict[str, Any]:
    """Structured error payload for result.json."""
    return {
        "error_code": error_code_for_exception(exc),
        "message": str(exc),
        "mode": mode or "unknown",
        "region": region or "unknown",
    }
