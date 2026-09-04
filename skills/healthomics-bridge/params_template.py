"""Parameter-template extraction for HealthOmics workflows."""

from __future__ import annotations

from typing import Any


def _default_for(spec: Any) -> Any:
    if isinstance(spec, dict):
        if "default" in spec:
            return spec["default"]
        typ = str(spec.get("type") or spec.get("primitiveType") or "").lower()
        if typ in {"int", "integer", "long"}:
            return 0
        if typ in {"float", "double"}:
            return 0.0
        if typ in {"boolean", "bool"}:
            return False
        if typ in {"array", "list"}:
            return []
        if typ in {"object", "map"}:
            return {}
    return ""


def skeleton_from_parameter_template(template: Any) -> dict[str, Any]:
    """Build a params.json skeleton from AWS's parameterTemplate shape."""
    if not isinstance(template, dict):
        return {}
    skeleton: dict[str, Any] = {}
    for name, spec in sorted(template.items()):
        skeleton[str(name)] = _default_for(spec)
    return skeleton


def workflow_params_payload(workflow: dict[str, Any]) -> dict[str, Any]:
    template = workflow.get("parameterTemplate") or workflow.get("parameters") or {}
    return {
        "workflow_id": workflow.get("id") or workflow.get("workflowId"),
        "workflow_name": workflow.get("name"),
        "workflow_type": workflow.get("type") or workflow.get("workflowType"),
        "parameter_template": template,
        "params": skeleton_from_parameter_template(template),
    }
