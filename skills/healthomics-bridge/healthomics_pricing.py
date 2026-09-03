"""Ready2Run pricing snapshot, so the cost gate can name the cost.

`--start-run` without `--confirm-submit` asks the user to authorise a charge.
A gate that cannot state the charge is ceremony, so this exists to put a real
number in front of that decision.

It is a **point-in-time snapshot, not a live lookup**. The estimate path is
deliberately client-free -- it builds and prints the request without opening a
connection -- so it cannot call AWS's Pricing API at estimate time without
giving that up. Treat every figure as indicative and verify in the console
before a decision that matters.

Only Ready2Run is priced here. A private workflow bills per-second compute and
no static table can price it; `estimated_cost_line` returns None for those, and
the caller omits the line rather than inventing a number.

Captured 2026-09-01, us-east-1, via:
    aws pricing get-products --service-code AmazonOmics --region us-east-1 \\
      --filters Type=TERM_MATCH,Field=regionCode,Value=us-east-1 \\
                Type=TERM_MATCH,Field=workflowType,Value=Ready2Run

Names were cross-referenced against a live ListAHOWorkflows(workflow_type=
READY2RUN) call to map each name to its workflow id — the Pricing API
identifies workflows by name, HealthOmics identifies them by id, and the two
never appear in the same response. 36 of 36 names matched an id exactly with
no ambiguity.

Every figure is the flat per-successful-run fee — AWS's own pricing page
states this is "the same flat fee ... regardless of run time" — so this
table does not model storage or any other separately-billed component, and
does not apply to a run that fails partway through (billing behaviour for a
failed run is unconfirmed; see SKILL.md Gotchas).
"""

from __future__ import annotations

from typing import Any

PRICING_SNAPSHOT_DATE = "2026-09-01"
PRICING_SNAPSHOT_REGION = "us-east-1"

# workflow_id -> (USD flat fee, workflow name at capture time)
READY2RUN_PRICING_USD: dict[str, tuple[float, str]] = {
    "1830181": (0.25, "ESMFold for up to 800 residues"),
    "4484039": (0.57, "Sentieon Germline FASTQ WES for up to 100x"),
    "4137328": (0.84, "Bases2Fastq for 2x75"),
    "2647398": (0.88, "NVIDIA Parabricks FQ2BAM WGS for up to 5X"),
    "2009847": (0.98, "Sentieon Germline BAM WES for up to 300x"),
    "5232234": (1.10, "scRNAseq with KallistoBUStools"),
    "8434454": (1.15, "NVIDIA Parabricks Germline HaplotypeCaller WGS for up to 5X"),
    "4523502": (1.39, "NVIDIA Parabricks BAM2FQ2BAM WGS for up to 5X"),
    "7866315": (1.56, "scRNAseq with Salmon Alevin-fry"),
    "9224188": (1.71, "Sentieon Germline BAM WGS for up to 32x"),
    "8422905": (1.78, "Sentieon LongRead for ONT"),
    "1617262": (2.00, "Ultima Genomics DeepVariant for up to 40x"),
    "1993486": (2.31, "NVIDIA Parabricks Germline DeepVariant WGS for up to 5X"),
    "1578479": (2.57, "Bases2Fastq for 2x300"),
    "2374431": (2.62, "Bases2Fastq for 2x150"),
    "2450177": (3.00, "Sentieon Germline FASTQ WES for up to 300x"),
    "9701407": (3.50, "NVIDIA Parabricks Somatic Mutect2 WGS for up to 50X"),
    "4414139": (4.00, "Sentieon Germline FASTQ WGS for up to 32x"),
    "1305211": (4.50, "Sentieon Somatic WES"),
    "5454617": (4.58, "GATK-BP Germline bam2vcf for 30x genome"),
    "5562080": (4.68, "GATK-BP Somatic WES bam2vcf"),
    "2174942": (5.31, "scRNAseq with STARsolo"),
    "4974161": (5.68, "NVIDIA Parabricks FQ2BAM WGS for up to 30X"),
    "4885129": (6.00, "AlphaFold for up to 600 residues"),
    "3021525": (6.50, "NVIDIA Parabricks Germline HaplotypeCaller WGS for up to 30X"),
    "6914655": (7.50, "Sentieon LongRead for PacBio HiFi"),
    "3768383": (8.00, "GATK-BP fq2bam"),
    "3412776": (8.05, "Sentieon Somatic WGS"),
    "5221318": (8.84, "NVIDIA Parabricks BAM2FQ2BAM WGS for up to 30X"),
    "6094971": (9.00, "AlphaFold for 601-1200 residues"),
    "8211545": (9.60, "NVIDIA Parabricks FQ2BAM WGS for up to 50X"),
    "9500764": (10.00, "GATK-BP Germline fq2vcf for 30x genome"),
    "7709200": (10.50, "NVIDIA Parabricks Germline HaplotypeCaller WGS for up to 50X"),
    "7330987": (11.12, "NVIDIA Parabricks Germline DeepVariant WGS for up to 30X"),
    "7112412": (14.74, "NVIDIA Parabricks BAM2FQ2BAM WGS for up to 50X"),
    "3585800": (18.76, "NVIDIA Parabricks Germline DeepVariant WGS for up to 50X"),
}


def estimated_cost_line(workflow_id: str) -> str | None:
    """One human-readable line for a Ready2Run workflow's snapshotted price.

    Returns None for anything not in the snapshot — a PRIVATE workflow (which
    bills per-second compute, not a flat fee, and can't be estimated from a
    static table at all), or a Ready2Run workflow added or renamed since
    capture. None is the honest answer in both cases; the caller should omit
    the cost line entirely rather than guess.
    """
    entry = READY2RUN_PRICING_USD.get(workflow_id)
    if entry is None:
        return None
    price, name = entry
    return (
        f'${price:.2f} flat fee for "{name}" (Ready2Run, {PRICING_SNAPSHOT_REGION}, '
        f"pricing snapshot from {PRICING_SNAPSHOT_DATE} — this is not a live "
        "quote; verify in the AWS console before relying on it)."
    )
