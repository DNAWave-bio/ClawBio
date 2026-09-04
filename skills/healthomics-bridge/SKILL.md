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

**With an AWS account** (`uv pip install boto3`), the whole loop:

| I want to… | Command |
|---|---|
| See my recent runs | `--list-runs --output out/` |
| See workflows (either type) | `--list-workflows [--workflow-type READY2RUN] --output out/` |
| Check readiness without acting | `--check --start-run WF --workflow-type PRIVATE --params p.json --output-uri s3://bucket/out/ --role-arn arn:... --run-name trial --output out/` |
| Search workflows | `--search-workflows "protein folding" [--workflow-type READY2RUN] --output out/` |
| Recommend workflows | `--recommend-workflow "call variants from FASTQ" --output out/` |
| Generate params skeleton | `--params-template WORKFLOW_ID [--workflow-type READY2RUN] --output out/` |
| Check on one run | `--run-status RUN_ID --output out/` |
| **Upload a run's inputs** | `--upload-inputs f1 f2 --to s3://bucket/in/ --allow-remote-inputs --confirm-upload` |
| **Register a WDL/CWL/Nextflow definition** | `--register main.wdl --workflow-name my-wf --confirm-register` |
| Submit a run | see **Submitting a run** — two steps, not one |
| **Download a run's outputs** | `--download-outputs RUN_ID --to ./results/ --confirm-download` |
| **Verify what a run produced** | `--run-status RUN_ID --verify-outputs [deep]` |
| Manage run tags | `--list-tags RUN_ID`, `--tag-run RUN_ID --tags '{"team":"genomics"}'`, `--sync-tags RUN_ID --tags '{"team":"genomics"}'` |

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
- "upload these FASTQs and run my WDL on them"

**Do NOT fire when:**

- The user wants run *performance analysis*, a costed timeline, workflow
  linting, or a container reachability check. Those are not API calls — they
  are AWS's own analysis tooling (`amazon-omics-tools`), and this skill does
  not reimplement them.
- The user wants to run Nextflow locally — use `nfcore-rnaseq-wrapper`,
  `nfcore-sarek-wrapper` or `nfcore-scrnaseq-wrapper`.
- The user wants a different cloud platform — use `flow-bio` or `illumina-bridge`.

## Why This Exists

- **Without it**: an agent pointed at boto3 has all 107 `omics` operations,
  no opinion about which are safe, and no record of what it did. `StartRun`
  spends real money and moves a genome into a cloud account; nothing in the
  SDK makes either fact visible beforehand.
- **With it**: 18 allow-listed operations of the 107 boto3 exposes, an
  egress acknowledgement the user gives out loud, an estimate-first cost gate,
  and an idempotency token so a repeated submission is deduplicated rather
  than billed twice.
- **The whole loop, not half of it**: inputs up, workflow registered, run
  submitted and watched, outputs down and checksummed — without dropping to the
  AWS CLI in the middle.
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
8. **Run I/O**: `--upload-inputs` puts a run's inputs in S3 behind both gates;
   `--download-outputs` brings that run's results back behind one.
9. **Registration**: `--register` packages a WDL, CWL or Nextflow definition
   into a reproducible archive and creates a private workflow, gated by
   `--confirm-register`.
10. **Output verification**: `--verify-outputs` records what a run actually
    produced — a cheap listing, or real SHA-256 checksums with `deep`.
11. **Preflight checks**: `--check` reports local/AWS readiness without creating
    resources, submitting runs or moving bytes.
12. **Workflow discovery**: `--search-workflows` and `--recommend-workflow`
    borrow the Galaxy/Bioconductor pattern: plain-language discovery before a
    user has memorised HealthOmics ids.
13. **Params scaffolding**: `--params-template` writes a starter
    `params.template.json` from a workflow's exposed parameter template.
14. **Downstream handoff**: verified or downloaded outputs produce
    `outputs.json` and `handoff.json` with suggested ClawBio next skills.
15. **Structured failures**: failed CLI runs write `result.json` with a stable
    `error_code` where the output directory is writable.
16. **Tag governance**: `--list-tags`, `--tag-run`, `--untag-run` and
    `--sync-tags` make run cost/allocation metadata auditable after submission.

## Scope

**One skill, one task**: the lifecycle of a HealthOmics *run* — which includes
getting that run's inputs to S3, registering the workflow it executes, and
bringing its outputs back. It does not analyse run performance, lint
definitions, check containers, or manage sequence and reference stores.

It is **not a general S3 tool**: S3 access is confined to a run's own input and
output prefixes. It creates no buckets, sets no bucket or object policies, and
deletes nothing. Destruction, permission changes and shared-config mutation are
barred by name in `omics_client.py` and `s3_client.py`, and no flag unlocks
them.

## Input Formats

| Format | Required fields | Example |
|---|---|---|
| Run identifier | `--run-status` | `7654321` |
| Parameters `.json` | `--params` with `--start-run` | `{"greeting": "hi"}` |
| Run tags JSON | `--run-tags` with `--start-run` | `{"team":"genomics"}` |
| Desired run tags JSON | `--tags` with `--tag-run` / `--sync-tags` | `{"team":"genomics","project":"atlas"}` |

## Workflow

1. **Resolve mode (prescriptive)**: exactly one of `--demo`, `--list-runs`,
   `--list-workflows`, `--run-status`, `--start-run`, `--upload-inputs`,
   `--download-outputs`, `--register`, discovery, params, or tag modes.
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
| `--check` | preflight | Can stand alone or accompany a planned operation; exits before any live action |
| `--list-runs` | read-only | `--limit` bounds results |
| `--list-workflows` | read-only | `--workflow-type PRIVATE\|READY2RUN` filters |
| `--search-workflows QUERY` | read-only | Scores workflows by name, description, id and type |
| `--recommend-workflow TASK` | read-only | Heuristic task-to-workflow recommendation |
| `--params-template WORKFLOW_ID` | read-only | Writes `params.template.json`; accepts `--workflow-type` and `--workflow-version-name` |
| `--run-status RUN_ID` | read-only | Run, tasks and workflow in one report |
| `--start-run WORKFLOW_ID` | gated | Requires `--workflow-type`, `--params`, `--output-uri`, `--role-arn`, `--run-name`; `--allow-remote-inputs` + `--confirm-submit` to pass both gates |
| `--run-tags JSON` | `--start-run` | Per-run cost allocation |
| `--list-tags RUN_ID` | read-only | Reads run tags from AWS by resource ARN |
| `--tag-run RUN_ID --tags JSON` | metadata mutation | Sets or updates the supplied keys on an existing run |
| `--untag-run RUN_ID --tag-keys KEY...` | metadata mutation | Removes supplied tag keys |
| `--sync-tags RUN_ID --tags JSON` | metadata mutation | Converges the run's tags to the supplied JSON, setting changed keys and removing stale ones |
| `--storage-type` / `--storage-capacity` | `--start-run` | Omit type for AWS's preferred DYNAMIC; capacity requires STATIC |
| `--cache-id` / `--cache-behavior` | `--start-run` | Reuse task results from a run cache |
| `--run-group-id` | `--start-run` | Concurrency and cost caps |
| `--wait` | `--start-run`, `--run-status` | Poll to a terminal state; `--poll-interval`, `--wait-timeout-seconds` tune it |
| `--upload-inputs PATH...` | gated | Needs `--to s3://…`, `--allow-remote-inputs` **and** `--confirm-upload` |
| `--download-outputs RUN_ID` | gated | Needs `--to DIR` and `--confirm-download`; targets `<outputUri>/<runId>/` only |
| `--verify-outputs [manifest\|deep]` | `--run-status` | `manifest` lists sizes/ETags and moves nothing; `deep` downloads and hashes (needs `--confirm-download`) |
| `--register DEFINITION` | gated | Needs `--workflow-name` and `--confirm-register`; `--engine` inferred from `.wdl`/`.cwl`/`.nf` |
| `--additional-files`, `--description`, `--parameter-template`, `--allow-duplicate-name` | `--register` | Multi-file bundles and workflow metadata |
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
submission — not written by hand. [EXAMPLE.md](EXAMPLE.md) walks the whole loop
(upload → register → submit → verify → download) with real output from each
step.

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

## Provenance

- Transport: **boto3** (AWS HealthOmics API directly).
- Allow-listed operations: 18 of 107 available.

This run executed in AWS HealthOmics. Replaying it requires the same account,
execution role and container images.
```

## Output Structure

```text
output_directory/
├── report.md
├── result.json
├── outputs.json       # when outputs were verified or downloaded
├── handoff.json       # suggested downstream ClawBio skills for local outputs
├── params.template.json # --params-template only
├── tables/
│   └── tasks.csv        # --run-status; mode-specific CSVs otherwise
└── reproducibility/
    ├── commands.sh
    ├── environment.yml
    ├── replay_manifest.json
    └── checksums.sha256
```

The table is named for what the report is about: `tasks.csv` for a run,
`runs.csv` for `--list-runs`, `workflows.csv` for `--list-workflows`,
`outputs.csv` when `--verify-outputs` ran, `uploads.csv` / `downloads.csv` for
transfers, `checks.csv` for `--check`, `params-template.csv` for
`--params-template`, and `definition.csv` plus `workflow.zip` for `--register`.
Exactly one table is written, so check the mode rather than assuming
`tasks.csv` exists. `replay_manifest.json` records the mode, region, run id,
workflow id/version and request id where available; `commands.sh` checks that
manifest before replaying.

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
- `tests/test_live_integration.py` is opt-in
  (`CLAWBIO_RUN_LIVE_HEALTHOMICS=1`) and read-only. It checks the botocore
  service model for allowlisted operation drift, `StartRun` required members,
  and `workflowType` enum support.
- Real-account evidence in [EXAMPLE.md](EXAMPLE.md) covers a Ready2Run run, a
  private WDL registration/run, output verification, and failure-message
  surfacing without AWS CLI handoffs.

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
- **Building a container for a private workflow?** Two failures are near
  certain the first time — an Apple Silicon image HealthOmics cannot execute,
  and an ECR repository policy that does not admit the omics service principal.
  Both are documented with their exact fixes in
  [GOTCHAS.md](GOTCHAS.md#containers-for-private-workflows).
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
- **It does write to S3, within one run's own prefixes.** This replaces an
  earlier, narrower promise ("never writes to S3; holds no S3 credentials"),
  which stopped being true when upload, download and verification were added.
  The honest statement now: S3 access is confined to a run's inputs and
  outputs, gated the same way submission is, and barred by name from bucket
  creation, policy changes and deletion of anything.
- **Why S3 at all**: HealthOmics exposes zero output-listing operations —
  verified against botocore, not assumed. Without S3 access, "what did this run
  produce?" is unanswerable.
- **Deep verification is billable.** It downloads every output to hash it, and
  S3 egress costs money, so it needs `--confirm-download`. `manifest` moves no
  bytes and costs nothing.
- **Registration creates a resource that persists.** It bills nothing, but the
  workflow stays in the account until deleted — and this skill cannot delete
  it. The report prints the `aws omics delete-workflow` command instead.
- **Disclaimer**: ClawBio is a research and educational tool. It is not a
  medical device and does not provide clinical diagnoses. Consult a healthcare
  professional before making any medical decisions.

## Agent Boundary

The agent dispatches, explains the gates, and interprets the report. The skill
enforces the allowlist, calls AWS and writes the bundle. The agent must not
pass `--confirm-submit` without the user asking for a run, invent a cost, or
claim a run succeeded when `summary.run_status` says otherwise.

## Chaining Partners

- **`variant-annotation`**, **`rnaseq-de`**, **`scrna-orchestrator`** — the
  downstream skills a finished run feeds. `--download-outputs` is what makes
  that handoff possible: those skills read local files, and a run's results
  live in S3 until something brings them back.

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
