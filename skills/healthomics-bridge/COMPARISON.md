# healthomics-bridge vs. ClawBio's other platform bridges

Every number below was measured against this repository and, where marked
**live**, against real cloud services — nothing here is recalled or assumed.
Test counts come from `pytest --co`; line counts exclude tests.

**Scope note**: this skill is deliberately self-contained (see `SKILL.md`). This
comparison is against ClawBio's *other* cloud-platform bridges —
`galaxy-bridge`, `flow-bio`, `illumina-bridge` — and, as a different category,
the local Nextflow wrappers. It does not compare against or reference any
other AWS HealthOmics skill.

## Summary table

| | **healthomics-bridge** | **galaxy-bridge** | **flow-bio** | **illumina-bridge** |
|---|---|---|---|---|
| **Platform** | AWS HealthOmics | usegalaxy.org | Flow.bio | Illumina DRAGEN/ICA |
| **Transport** | boto3 → AWS API | bioblend SDK | REST (`requests`) | local FS + optional REST |
| **Auth** | AWS profile / IAM role | `GALAXY_API_KEY` | user+pass → JWT | optional `ILLUMINA_ICA_API_KEY` |
| **Credential handling** | resolved by boto3, never read/stored/forwarded | in-process bioblend | in-process `requests.Session` | in-process (metadata only) |
| **Causes billing / state change** | **yes** | yes (creates a history) | yes (creates sample/execution) | no |
| **Egress gate** (explicit ack before data leaves) | ✅ `--allow-remote-inputs` | ❌ | ❌ | n/a — local-first |
| **Cost gate** (second confirmation to spend) | ✅ `--confirm-submit` | ❌ | ❌ | n/a |
| **Cost gate states the cost** | ✅ priced from a dated snapshot | n/a | n/a | n/a |
| **Operation allowlist** | ✅ 9 of 107 API operations, + 3 of ~90 S3 methods | n/a | n/a | n/a |
| **Errors** | botocore raises | raises | raises | raises |
| **`--demo` fully offline** | ✅ | ✅ | ❌ (live API call) | ✅ |
| **Live tests vs. real service** | ✅ 7, 2 need no network | ❌ | ❌ | ❌ |
| **Tests** | 110 (103 offline + 7 live) | 51 | 25 | 26 |
| **Python LOC** | 1,795 | 1,913 | 1,540 | 1,303 |

**The gate columns are the real story.** `healthomics-bridge` is the only one
of these four that (a) can spend real money and (b) makes both consequences —
data leaving the machine, and a charge — an explicit, separate confirmation.
`galaxy-bridge` and `flow-bio` create remote state (a Galaxy history, a Flow
sample) without an equivalent gate, because uploading a small file to a shared
academic instance and starting a $0.25–$20+ cloud compute run are different
risk classes. Neither is wrong for its own platform; the point is that
HealthOmics needed the stronger contract and got one.

## Data handling: where do the bytes go?

| | **healthomics-bridge** | **galaxy-bridge** | **flow-bio** | **illumina-bridge** |
|---|---|---|---|---|
| **Pattern** | reference passing + gated run I/O | proxy relay | proxy relay | local-only |
| **Where inputs live** | local disk → S3, or already in S3 | local disk | local disk | local disk |
| **Data through the skill's own process** | yes — a run's own inputs and outputs | yes — `bioblend` upload | yes — chunked upload | read-only, in place |
| **Holds storage credentials** | yes — S3, scoped to a run's prefixes | yes (API key) | yes (JWT) | no |
| **Uploads sample data** | **yes** — `--upload-inputs`, double-gated | **yes** | **yes** | no |
| **Downloads results** | **yes** — `--download-outputs`, gated | **yes** | no (metadata only) | n/a |
| **Can verify its own outputs** | **yes** — ETag manifest, or real SHA-256 with `--verify-outputs deep` | yes (downloaded) | partial | yes (local) |

`healthomics-bridge` used to hand AWS a URI and touch no bytes at all, which
is why its report once stated it could not checksum outputs. That changed when
run I/O was added, and for a reason worth naming: **HealthOmics exposes zero
output-listing operations**, so "what did this run produce?" is unanswerable
without S3. The skill now moves a run's own inputs and outputs and can hash
them — but only within that run's prefixes, and it still creates no buckets,
sets no policies and deletes nothing.

The distinction that survives is *scope*, not abstinence. `galaxy-bridge` and
`flow-bio` are pipes for whatever you hand them; this skill's storage access is
bounded by one run's input and output paths, which is what keeps a
credential-holding skill honest about its blast radius.

## This skill's own flow

```mermaid
flowchart TD
    CLI["CLI — 8 modes"] --> MODE{mode?}
    MODE -->|--demo| FIX["replay JSON fixture<br/>no boto3, no creds, no network"]
    MODE -->|read-only| PAGE["list_all — pages nextToken<br/>AWS caps maxResults at 100"]
    MODE -->|--start-run| G1{"--allow-remote-inputs?"}
    G1 -->|no| R1["REFUSE — prints every<br/>S3 path first"]
    G1 -->|yes| EST["build exact request<br/>+ Ready2Run flat-fee estimate<br/>+ round STATIC storage"]
    EST --> G2{"--confirm-submit?"}
    G2 -->|no| STOP["estimate only<br/>client never constructed<br/>bills nothing"]
    G2 -->|yes| CALL
    PAGE --> CALL["OmicsOperations.call"]
    CALL --> AL{"op in ALLOWED_OPERATIONS?<br/>9 of 107"}
    AL -->|no| R3["OperationNotAllowed<br/>never reaches AWS"]
    AL -->|yes| BOTO["boto3 omics client<br/>adaptive retries, 10 attempts"]
    BOTO --> AWS["AWS HealthOmics API"]
    AWS -->|error| R4["botocore raises<br/>no in-band error to detect"]
    AWS -->|ok| WAIT{"--wait?"}
    WAIT -->|yes| POLL["poll GetRun to terminal<br/>failed tasks enriched via GetRunTask"]
    WAIT -->|no| BUNDLE
    POLL --> BUNDLE["report.md + result.json<br/>+ runs/workflows/tasks.csv<br/>+ tags read back from AWS"]
    FIX --> BUNDLE

    style R1 fill:#c62828,color:#fff
    style R3 fill:#c62828,color:#fff
    style R4 fill:#c62828,color:#fff
    style BUNDLE fill:#2e7d32,color:#fff
```

## A different category: the local Nextflow wrappers

`nfcore-rnaseq-wrapper`, `nfcore-sarek-wrapper` and `nfcore-scrnaseq-wrapper`
aren't really comparable line-for-line — they run Nextflow itself (locally or
against whatever backend Nextflow is configured for), not a single hosted
platform's API, and their `--allow-remote-inputs` gate is a **pure policy
check that performs no I/O**: it inspects and permits or refuses URIs that get
passed to Nextflow verbatim, which stages them. Worth naming for one reason:
`healthomics-bridge` and the nf-core wrappers independently converged on the
same flag name and the same fail-closed default for the same problem — sending
genomic data somewhere the user didn't explicitly say to send it. That
convergence, not the numeric comparison, is the useful data point.

## Evidence this table rests on

Every claim about `healthomics-bridge` above traces to a real, live-executed
run, not to its own tests:

- **Ready2Run**: ESMFold submitted with `--start-run --workflow-type
  READY2RUN`, completed, produced a real predicted structure (`pLDDT 74.6`)
  and a real cost ($0.25, the flat Ready2Run fee).
- **Private workflow**: a minimal WDL registered, containerised for the
  correct architecture, submitted with `--workflow-type PRIVATE --wait`,
  completed, and its S3 output verified to contain the exact string passed in
  as a parameter.
- **A rejected submission**: deliberately pointed at an input the execution
  role could not read. Failed before billing, as an exception — not as data.
- **Two real failures, both diagnosed through this skill's own report**: a
  container built for the wrong CPU architecture, and a missing ECR
  repository policy. Both are documented in [GOTCHAS.md](GOTCHAS.md) with the
  exact fix.
- **The full loop, live, through this skill alone**: local file uploaded to S3,
  WDL registered as a private workflow (`ACTIVE`), run submitted and watched to
  `COMPLETED`, outputs downloaded and hashed. An independent `shasum -a 256`
  matched the SHA-256 the skill reported, and the downloaded file held the
  exact string passed in as a parameter. No AWS CLI call in any step.
