"""Workflow search and recommendation helpers for healthomics-bridge."""

from __future__ import annotations

from typing import Any


TASK_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("variant calling", ("variant", "vcf", "gatk", "sarek", "germline", "somatic")),
    ("rna-seq", ("rna", "rnaseq", "transcript", "expression", "fastq")),
    ("protein structure", ("protein", "structure", "esmfold", "fold", "fasta")),
    ("alignment", ("align", "bam", "cram", "fastq", "fq2bam")),
    ("qc", ("qc", "quality", "fastqc", "multiqc")),
)


def _tokens(text: str) -> set[str]:
    return {t for t in text.lower().replace("_", " ").replace("-", " ").split() if t}


def workflow_score(workflow: dict[str, Any], query: str) -> int:
    haystack = " ".join(
        str(workflow.get(k, "")) for k in ("name", "description", "id", "type", "workflowType")
    )
    query_tokens = _tokens(query)
    hay_tokens = _tokens(haystack)
    score = 4 * len(query_tokens & hay_tokens)
    lower = haystack.lower()
    if query.lower() in lower:
        score += 10
    for _, words in TASK_KEYWORDS:
        if any(word in query.lower() for word in words) and any(word in lower for word in words):
            score += 6
    if str(workflow.get("status", "")).upper() == "ACTIVE":
        score += 2
    return score


def search_workflows(items: list[dict[str, Any]], query: str, *, limit: int) -> list[dict[str, Any]]:
    ranked = [
        {**item, "matchScore": workflow_score(item, query)}
        for item in items
        if workflow_score(item, query) > 0
    ]
    return sorted(ranked, key=lambda item: (-int(item["matchScore"]), str(item.get("name", ""))))[:limit]


def recommend_workflows(items: list[dict[str, Any]], task: str, *, limit: int) -> dict[str, Any]:
    matches = search_workflows(items, task, limit=limit)
    inferred = [
        label for label, words in TASK_KEYWORDS
        if any(word in task.lower() for word in words)
    ]
    return {
        "task": task,
        "inferred_domains": inferred or ["general"],
        "recommendations": matches,
        "n_recommendations": len(matches),
    }
