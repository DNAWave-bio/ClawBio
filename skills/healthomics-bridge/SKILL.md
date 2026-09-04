---
name: healthomics-bridge
description: >-
  Submit, monitor and import AWS HealthOmics genomics runs through the boto3
  API directly, behind an allow-listed client, a fail-closed data-egress gate
  and an estimate-first cost gate.
license: MIT
metadata:
  version: 0.1.0
  author: Dmitry Shirokov
  domain: bioinformatics
  tags:
    - aws
    - healthomics
    - cloud
    - boto3
    - workflow-execution
    - provenance
  inputs:
    - name: params
      type: file
      format:
        - json
      description: Workflow parameters for a run submission
      required: false
    - name: run_id
      type: string
      format:
        - string
      description: An existing HealthOmics run identifier to report on
      required: false
  outputs:
    - name: report
      type: file
      format:
        - md
      description: Human-readable run report with task outcomes and provenance
    - name: result
      type: file
      format:
        - json
      description: Machine-readable envelope for downstream chaining
    - name: tasks
      type: file
      format:
        - csv
      description: Per-task table with status, resources and timings
  dependencies:
    python: ">=3.11"
    packages:
      - boto3>=1.34
  demo_data:
    - path: skills/healthomics-bridge/tests/fixtures/demo_run_bundle.json
      description: Synthetic API responses driving the fully offline demo
  endpoints:
    cli: python skills/healthomics-bridge/healthomics_bridge.py --run-status {run_id} --output {output_dir}
  openclaw:
    requires:
      env:
        - AWS_REGION
        - AWS_PROFILE
    always: false
    emoji: "🧬"
    homepage: https://github.com/ClawBio/ClawBio
    os:
      - darwin
      - linux
    install:
      - kind: uv
        command: uv pip install boto3
    trigger_keywords:
      - AWS HealthOmics
      - HealthOmics run
      - submit Ready2Run workflow
      - omics run status
      - start genomics workflow on AWS
---

# 🧬 AWS HealthOmics Bridge (boto3)

You are **HealthOmics Bridge**, a specialised ClawBio agent for running genomics
workflows on AWS HealthOmics through the API directly. You never author
workflows and you never manage AWS storage — you submit, watch, and import.

## Quick Start

**Right now, with nothing installed and no AWS account:**

```bash
uv run python skills/healthomics-bridge/healthomics_bridge.py --demo --output /tmp/ho
cat /tmp/ho/report.md
```

**With an AWS account** (`uv pip install boto3`), four modes:

| I want to… | Command |
|---|---|
| See my recent runs | `--list-runs --output out/` |
| See workflows (either type) | `--list-workflows [--workflow-type READY2RUN] --output out/` |
| Check on one run | `--run-status RUN_ID --output out/` |
| Submit a run | see **Submitting a run** — two steps, not one |

**Submitting is two steps on purpose**: run `--start-run` alone first — it builds
and prints the exact request and bills nothing — then add `--confirm-submit`
once it looks right. `--workflow-type` is required, because guessing it is how a
Ready2Run submission fails with a confusing not-found error.

## Trigger

**Fire this skill when the user says any of:**

- "submit this Ready2Run workflow"
- "start a HealthOmics run"
- "check my omics run"
- "list my HealthOmics runs"
- "tag this run for cost allocation"

**Do NOT fire when:**

- The user wants run *performance analysis*, a costed timeline, workflow
  linting, or a container reachability check. Those are not API calls — they
  are AWS's own analysis tooling (`amazon-omics-tools`), and this skill does
  not reimplement them.
- The user wants to run Nextflow locally — use `nfcore-rnaseq-wrapper`,
  `nfcore-sarek-wrapper` or `nfcore-scrnaseq-wrapper`.
- The user wants to move data to or from S3 — use `aws-s3-bridge`.
- The user wants a different cloud platform — use `flow-bio` or `illumina-bridge`.

## Why This Exists

- **Without it**: an agent pointed at boto3 has all 107 `omics` operations,
  no opinion about which are safe, and no record of what it did. `StartRun`
  spends real money and moves a genome into a cloud account; nothing in the
  SDK makes either fact visible beforehand.
- **With it**: eight allow-listed operations of the 107 boto3 exposes, an
  egress acknowledgement the user gives out loud, an estimate-first cost gate,
  and an idempotency token so a repeated submission is deduplicated rather
  than billed twice.
- **Why ClawBio**: the specification layer. AWS ships the API; this skill ships
  the constraint, the disclosure and the provenance.

## Core Capabilities

1. **Read-only inspection**: list runs, list workflows of either type, report
   one run with its tasks.
2. **Gated submission**: build the exact `StartRun` request, show it, and
   submit only after an explicit second confirmation.
3. **Ready2Run submission**: AWS's published catalogue, submitted by id and
   type — about half of what HealthOmics offers.
4. **Idempotent submission**: `requestId` is derived from the request's own
   content, so re-running an identical command is a no-op at AWS.
5. **Local import**: `report.md`, `result.json` and `tables/tasks.csv`, so a
   run becomes an ordinary ClawBio artifact.
6. **Watch to completion**: `--wait` polls a run you started, so submitting
   does not send you to the AWS CLI to see how it went.
7. **Priced estimates**: a Ready2Run estimate names its flat fee before you
   pass the cost gate.

## Scope

**One skill, one task**: the lifecycle of a HealthOmics *run*. It does not
author workflows, analyse run performance, lint definitions, check containers,
manage sequence or reference stores, or move data in or out of S3. It never
calls a destructive operation, changes a permission, or mutates shared account
configuration — those are barred by name in `omics_client.py` and no flag
unlocks them.

## Input Formats

| Format | Required fields | Example |
|---|---|---|
| Run identifier | `--run-status` | `7654321` |
| Parameters `.json` | `--params` with `--start-run` | `{"greeting": "hi"}` |
| Run tags JSON | `--run-tags` with `--start-run` | `{"team":"genomics"}` |

## Workflow

1. **Resolve mode (prescriptive)**: exactly one of `--demo`, `--list-runs`,
   `--list-workflows`, `--run-status`, `--start-run`.
2. **Short-circuit the demo (prescriptive)**: `--demo` returns before any
   region, credential, network path or boto3 import is touched.
3. **Gate egress (prescriptive)**: for `--start-run`, refuse without
   `--allow-remote-inputs`; with it, print every path the run will read or write.
4. **Gate cost (prescriptive)**: without `--confirm-submit`, build and report
   the request, write the bundle, and submit nothing.
5. **Allow-list every call (prescriptive)**: any operation outside
   `ALLOWED_OPERATIONS` is refused before it reaches AWS.
6. **Report (flexible)**: render the run, its tasks and its provenance; never
   omit a failed task.

## CLI Reference

| Flag | Mode / applies to | Notes |
|---|---|---|
| `--demo` | offline | Short-circuits everything; no boto3, no credentials |
| `--list-runs` | read-only | `--limit` bounds results |
| `--list-workflows` | read-only | `--workflow-type PRIVATE\|READY2RUN` filters |
| `--run-status RUN_ID` | read-only | Run, tasks and workflow in one report |
| `--start-run WORKFLOW_ID` | gated | Requires `--workflow-type`, `--params`, `--output-uri`, `--role-arn`, `--run-name`; `--allow-remote-inputs` + `--confirm-submit` to pass both gates |
| `--run-tags JSON` | `--start-run` | Per-run cost allocation |
| `--storage-type` / `--storage-capacity` | `--start-run` | Omit type for AWS's preferred DYNAMIC; capacity requires STATIC |
| `--cache-id` / `--cache-behavior` | `--start-run` | Reuse task results from a run cache |
| `--run-group-id` | `--start-run` | Concurrency and cost caps |
| `--wait` | `--start-run`, `--run-status` | Poll to a terminal state; `--poll-interval`, `--wait-timeout-seconds` tune it |
| `--region` / `--profile` | all live modes | Default from `AWS_REGION`/`AWS_PROFILE` |
| `--output` | all modes | Bundle destination |

## Demo

`--demo` replays synthetic API responses through the same mapping and bundling
path a live run uses. It demonstrates the report contract and the provenance
ceiling; it is not evidence that any AWS call works.

## Algorithm / Methodology

1. Resolve the mode and enforce the gates before any client is constructed.
2. Build the boto3 client lazily, so the demo needs no cloud dependency.
3. Refuse any operation outside the allowlist before dispatch.
4. `GetRun` → `ListRunTasks` → `GetWorkflow` for the run under inspection.
   Failed/cancelled tasks each get a follow-up `GetRunTask` (capped at 25),
   because `ListRunTasks` does not carry a task's own failure reason.
5. Partition tasks into completed and failed/cancelled.
6. Write the output contract and checksum it.

**Key parameters**: `--limit` (default 25) bounds list modes; `requestId` is
derived, not supplied.

## Example Queries

- "Submit the ESMFold Ready2Run workflow against these inputs."
- "What happened to run 7049640?"

## Example Output

Captured verbatim from `--run-status` against a real, completed Ready2Run
submission — not written by hand. This is the same run cited in Validation
Evidence below.

```markdown
# AWS HealthOmics Run Report

**Mode**: Live AWS HealthOmics (boto3)
**Region**: us-east-1
**Run status**: COMPLETED

## Run

- **Run id**: `7049640`
- **Run name**: clawbio-boto3-r2r-verify
- **Status**: **COMPLETED**
- **Workflow**: `ESMFold for up to 800 residues` (`1830181`, READY2RUN)
- **Output URI**: `s3://my-bucket/output/`
- **Tags**: `purpose=boto3-live-verification`, `skill=healthomics-bridge`

## Tasks

2 task(s): 2 completed, 0 failed.

## Fetching the outputs

Outputs stay in S3; this skill holds no S3 credentials. Download them with
`aws-s3-bridge`, or directly:

    aws s3 cp --recursive s3://my-bucket/output/7049640/ ./run-7049640/

## Provenance

- Transport: **boto3** (AWS HealthOmics API directly).
- Allow-listed operations: 8 of 107 available.

This run executed in AWS HealthOmics. Replaying it requires the same account,
execution role and container images.
```

## Output Structure

```text
output_directory/
├── report.md
├── result.json
├── tables/
│   └── tasks.csv        # --run-status; runs.csv / workflows.csv for list modes
└── reproducibility/
    ├── commands.sh
    ├── environment.yml
    └── checksums.sha256
```

The table is named for what the report is about: `tasks.csv` for a run,
`runs.csv` for `--list-runs`, `workflows.csv` for `--list-workflows`. Exactly
one is written, so check the mode rather than assuming `tasks.csv` exists.

## Dependencies

**Required**: Python >=3.11, the ClawBio base install. **Live modes only**:
`boto3>=1.34`, imported lazily with an actionable error — the offline demo runs
without it. **AWS**: credentials resolved by boto3 from your own profile,
environment or instance role; this skill never reads, stores or forwards them.

## Validation Evidence

- The offline demo asserts the full output contract on every run.
- The allowlist is covered by tests proving destructive operations
  (`DeleteRun`, `CancelRun`, `UpdateRunGroup`, …) are refused before any
  boto3 call is dispatched.
- Both gates are covered by tests asserting `StartRun` is never reached.
- `requestId` derivation is tested for stability and for changing when the
  submission changes.
- **Exercised against a live AWS account.**
  `tests/test_live_integration.py` (opt-in via `CLAWBIO_RUN_LIVE_HEALTHOMICS=1`)
  reads `StartRun`'s required members and `workflowType`'s enum out of
  botocore's own service model, so an AWS contract change fails at test time
  rather than at submission time. It also lists Ready2Run workflows live and
  asserts nothing credential-shaped reaches the bundle. Every test in that file
  is read-only and costs nothing.
- **One real Ready2Run run submitted end to end** (ESMFold, fixed price
  $0.25). AWS recorded `workflowType: READY2RUN` and both run tags on the run
  resource, confirmed against AWS rather than inferred from the service model.
- **One real PRIVATE workflow registered and submitted end to end**: a minimal
  WDL, containerised, submitted with `--start-run --workflow-type PRIVATE
  --wait`, reached `COMPLETED`, and its output file was read back from S3
  containing the exact string passed in as a parameter. Two real failures were
  hit and fixed along the way (see Gotchas: architecture mismatch, ECR
  repository policy) — both surfaced through this skill's own reporting, not
  through the AWS CLI or console.
- **A rejected submission raises rather than returning data.** A deliberate
  first attempt with an input the execution role could not read produced a
  `ValidationException` that propagated as an exception and billed nothing —
  botocore raises, so a rejected submission cannot be mistaken for data.
- **A FAILED run's own reason, and its failing task's reason, both confirmed
  live.** The private-workflow run above failed twice before it succeeded;
  both failures rendered their real AWS-supplied message in `report.md`,
  including the task-level `GetRunTask` enrichment this section exists to
  document.

## Gotchas

- **You will want to omit `--workflow-type` and let it default. Do not — it is
  required here on purpose.** AWS resolves a Ready2Run workflow id only when
  told the type; guessing PRIVATE produces a bare `ResourceNotFoundException`
  that reads like a typo'd id. Being explicit converts a confusing failure into
  an impossible one.
- **You will want to treat exit code 0 as "the run succeeded". Do not.** Exit 0
  means the skill produced a truthful report; the run's own outcome is in
  `summary.run_status`/`summary.n_failed_tasks`.
- **You will want to reach for this skill for cost or efficiency analysis. It
  has none.** Run-performance analysis, a costed timeline, workflow linting and
  container reachability are not API calls — they are AWS's own tooling
  (`amazon-omics-tools`, the Run Analyzer). This skill reports what a run *did*;
  it does not judge how well it did it.
- **You will want to point a Ready2Run workflow at your own S3 input. Check the
  execution role first.** The submission fails with a bare
  `ValidationException: S3 access denied` naming the *input path*, which reads
  like the object is missing — it is the role that cannot read it. AWS's
  console-generated `OmicsWorkflow-*` role grants `GetObject` only on
  `s3://omics-<region>/*` plus `PutObject` on your chosen output prefix, so
  every Ready2Run workflow ships a readable sample input at
  `s3://omics-<region>/sample-inputs/<workflowId>/`. Use that to prove the path
  works before widening any policy.
- **`requestId` makes a repeat submission a no-op, not a new run.** Re-running
  the identical command returns the original run rather than starting a second
  one. Change the run name to genuinely submit again.
- **Ready2Run runs come back `storageType: STATIC`, whatever you omit.** A
  private workflow left to AWS's default reports DYNAMIC; a Ready2Run run
  reports STATIC because its capacity is fixed by the published workflow. Do
  not read that as this skill having sent a storage type — it sends none.
- **You will want to read `--wait` as a safety feature. It is not.** Watching
  a run does not stop it billing, and Ctrl-C stops the watching, not the run.
  `--wait-timeout-seconds` gives up watching after 24h by default and says so;
  the run carries on.
- **`--storage-capacity` is rounded up, not honoured.** STATIC capacity is
  1,200 GiB or a multiple of 2,400 — not 1,200 chunks, which is the intuitive
  and wrong reading. Ask for 5,000 and AWS allocates and bills 7,200; the skill
  now says so before submission rather than letting the invoice explain it.
- **A cost estimate is a snapshot, not a quote.** Ready2Run fees come from a
  dated table captured from AWS's Pricing API, because the estimate path opens
  no connection by design. Private workflows bill per-second compute and get no
  estimate at all — no number is the honest answer there.
- **This skill can start a billable run but cannot stop one.** `CancelRun` is
  barred by name as a destructive operation. Stop a runaway run with
  `aws omics cancel-run --id <id>`.
- **You will want to build a private workflow's container on Apple Silicon and
  push it straight to ECR. Don't — HealthOmics only runs `linux/amd64`.** A
  Mac-native build fails every task with `exec /bin/bash: exec format error`,
  and that error surfaces from *inside the container*, well past this skill's
  own gates — the request looked correct, `--confirm-submit` succeeded, and
  the run still failed. Always `docker buildx build --platform linux/amd64`.
- **A private submission can fail with `Unable to access image URI... Ensure
  the ECR private repository... has granted access for the omics service
  principle`, even though your execution role already has `ecr:BatchGetImage`
  on that repository.** IAM role permissions and the ECR *repository policy*
  are two different grants — HealthOmics needs both. The role lets the
  identity call ECR; the repository policy lets `omics.amazonaws.com` itself
  reach the repository at all:

  ```json
  {
    "Version": "2012-10-17",
    "Statement": [{
      "Sid": "AllowHealthOmicsPull",
      "Effect": "Allow",
      "Principal": {"Service": "omics.amazonaws.com"},
      "Action": ["ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage",
                 "ecr:BatchCheckLayerAvailability"]
    }]
  }
  ```

  `aws ecr set-repository-policy --repository-name <repo> --policy-text file://policy.json`
  — a permission change, so it is not something this skill does or ever will;
  see Safety. Verify the image is both correct-architecture and
  correctly-granted *before* `--confirm-submit`, not after a failed run has
  already billed for compute that never ran the workload:
  `aws ecr describe-images` for the manifest's architecture, and
  `aws ecr get-repository-policy` for the grant.
- **A task's own failure reason needs a second call — `ListRunTasks` alone
  does not carry it.** `GetRun`'s `statusMessage` and `ListRunTasks`'
  per-task status are not the same information: the run-level message often
  says only "see the failing task," and without a follow-up `GetRunTask` call
  the "Failed tasks" section could name a task but never say why it failed.
  This skill makes that follow-up automatically, for failed tasks only, up to
  `_MAX_TASKS_TO_ENRICH` (25) per report.

## Safety

- **This skill sends genomic data to AWS.** That is its purpose, not a side
  effect. `--start-run` is refused without `--allow-remote-inputs`, which
  prints every path the run will read or write.
- **Cost**: HealthOmics runs are billable. `--start-run` estimates by default
  and submits only with `--confirm-submit`.
- **Credentials**: never read, stored or forwarded. boto3 resolves them from
  your own profile, environment or instance role.
- **Gated by consequence, not by verb**: destruction (`Delete*`, `CancelRun`)
  and shared-config mutation (`Update*`, run group/cache creation) are barred
  outright by name in `PERMANENTLY_EXCLUDED` — no flag unlocks them. Spending
  money (`StartRun`) needs `--confirm-submit`.
- **Never writes to S3.** It holds no S3 credentials; run outputs stay in S3
  and the report prints the command to fetch them.
- **Disclaimer**: ClawBio is a research and educational tool. It is not a
  medical device and does not provide clinical diagnoses. Consult a healthcare
  professional before making any medical decisions.

## Agent Boundary

The agent dispatches, explains the gates, and interprets the report. The skill
enforces the allowlist, calls AWS and writes the bundle. The agent must not
pass `--confirm-submit` without the user asking for a run, invent a cost, or
claim a run succeeded when `summary.run_status` says otherwise.

## Chaining Partners

- **`aws-s3-bridge`** — upload inputs before a run, download outputs after.
- **`variant-annotation`**, **`rnaseq-de`**, **`scrna-orchestrator`** — the
  downstream skills a finished run feeds, once its outputs are local.

## Maintenance

- **Review cadence**: on boto3 releases that change the `omics` service model.
- **Staleness signals**: a new HealthOmics API operation worth allow-listing;
  a change to `StartRun`'s required members or to `workflowType`'s enum; a
  Ready2Run price change (`healthomics_pricing.py` is a dated snapshot).
- **Deprecation**: retire if AWS discontinues HealthOmics.

## Citations

- [AWS HealthOmics](https://aws.amazon.com/healthomics/) — the service this skill drives.
- [boto3 omics client](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/omics.html) — the transport.
- [Run storage types](https://docs.aws.amazon.com/omics/latest/dev/workflows-run-types.html) — source for the STATIC capacity rounding rule.
- [COMPARISON.md](COMPARISON.md) — measured comparison against ClawBio's other platform-bridge skills.

---

*ClawBio is a research and educational tool. It is not a medical device and does
not provide clinical diagnoses. Consult a healthcare professional before making
any medical decisions.*
