# A worked run, start to finish

Every block below is **real output**, captured from commands actually executed
against AWS while building this skill — not written by hand. Account ids,
bucket names and run/workflow ids are genericized; nothing else is edited.

The run this traces: a small WDL that echoes a string, uploaded, registered,
submitted, watched and verified. It cost cents. A real pipeline differs only in
scale.

---

## 0. Before anything: the offline demo

Needs no AWS account, no credentials, no network, and not even boto3.

```bash
uv run python skills/healthomics-bridge/healthomics_bridge.py --demo --output /tmp/ho
```

Use it to see the report contract. It replays a fixture and proves nothing
about AWS.

---

## 1. Upload the run's inputs

Refused outright without the egress acknowledgement:

```text
ERROR: This upload would copy local files to S3:
  /path/to/greeting.txt
  -> s3://my-bucket/input/demo/
Re-run with --allow-remote-inputs to acknowledge that genomic data will leave this machine.
```

Acknowledged but unconfirmed — still moves nothing:

```bash
--upload-inputs greeting.txt --to s3://my-bucket/input/demo/ --allow-remote-inputs
```

```text
WARNING: --allow-remote-inputs is set; 1 local file(s) will be copied to
s3://my-bucket/input/demo/, so genomic data leaves the local machine.
ESTIMATE ONLY: nothing was uploaded. Re-run with --confirm-upload to transfer.
```

```markdown
## Upload

**Destination**: `s3://my-bucket/input/demo/`

**Nothing was uploaded.** Files that would be sent:

- `/path/to/greeting.txt`

Re-run with `--confirm-upload` to transfer.
```

Add `--confirm-upload` and it transfers, reporting the URIs to paste into your
params file. **Two gates, because uploading puts a genome somewhere it was
not.**

---

## 2. Register the workflow

Dry run by default — the archive is still built and checksummed, because seeing
the digest before creating anything is the point:

```bash
--register main.wdl --workflow-name my-pipeline
```

```markdown
## Workflow definition

- **Name**: `my-pipeline`
- **Engine**: WDL
- **Definition**: `/path/to/main.wdl`
- **Archive**: 613 bytes, `5839acd3726034ac…` (stored)

| Archive member | Bytes | sha256 |
|---|---|---|
| `main.wdl` | 499 | `8882fb6ec1b9…` |

The archive digest is reproducible: the same inputs always produce the same
bytes, so it pins exactly what was uploaded.

## Nothing was created.

Re-run with `--confirm-register` to create this workflow. Registration bills
nothing; running the workflow does.
```

The engine was inferred from `.wdl`. `--confirm-register` creates it and polls
until it settles:

```markdown
## Workflow created — status `ACTIVE`

- **Workflow id**: `1234567`
```

`ACTIVE` means AWS accepted the definition. `FAILED` means it rejected it —
**there is no lint API to catch that earlier**, so validation happens
server-side after creation. See [GOTCHAS.md](GOTCHAS.md#why-there-is-no---lint).

---

## 3. Submit and watch

```bash
--start-run 1234567 --workflow-type PRIVATE \
  --params run_params.json --output-uri s3://my-bucket/output/ \
  --role-arn arn:aws:iam::<account>:role/<omics-role> \
  --run-name my-run --run-tags '{"team":"genomics"}' \
  --allow-remote-inputs --confirm-submit --wait
```

```text
WARNING: --allow-remote-inputs is set; 1 path(s) will be read or written by
AWS HealthOmics, so genomic data leaves the local machine.
Waiting for run 7654321 to reach a terminal state (polling every 30s).
The run keeps billing while this waits; Ctrl-C stops watching, not the run.
```

```json
{
  "run_id": "7654321",
  "run_status": "COMPLETED",
  "n_tasks": 1,
  "n_failed_tasks": 0,
  "submitted": true,
  "region": "us-east-1",
  "demo": false
}
```

Without `--confirm-submit` this prints the exact `StartRun` request, prices it
where AWS publishes a flat fee, and bills nothing.

---

## 4. Verify what the run produced

### Cheap: `--verify-outputs`

Lists the run's output prefix. Moves no bytes, costs nothing.

```markdown
## Outputs

3 object(s), 1,298 bytes under `s3://my-bucket/output/7654321/`.

Listing only — no bytes were transferred. **The ETag column is not a
checksum**: it equals the object's MD5 only for a single-part upload, and for a
multipart upload it is the MD5 of the part MD5s with a `-N` suffix, which
cannot be recomputed from the file. Use `--verify-outputs deep` for real
SHA-256 checksums.

| Key | Bytes | ETag | MD5? |
|---|---|---|---|
| `output/7654321/logs/engine.log` | 1,174 | `f9e1b9aa88f112e19c78f8a25d9f71a2` | yes |
| `output/7654321/logs/outputs.json` | 87 | `be34a305ae1023c4417a91eb69dcdbb8` | yes |
| `output/7654321/out/stdout/out.txt` | 37 | `cb6788c34c3e9ca9294470940ce43eb5` | yes |
```

### Rigorous: `--verify-outputs deep --confirm-download`

Downloads every object and hashes it. Needs the gate because S3 egress is
billable.

```markdown
## Outputs

3 object(s), 1,298 bytes under `s3://my-bucket/output/7654321/`.

Every listed object was downloaded to `/tmp/results` and hashed. The SHA-256
values below are real checksums of the bytes on disk.

| Key | Bytes | ETag | MD5? | SHA-256 |
|---|---|---|---|---|
| `output/7654321/logs/engine.log` | 1,174 | `f9e1b9aa88f112e19c78f8a25d9f71a2` | yes | `fbcf215c81a913b9…`|
| `output/7654321/logs/outputs.json` | 87 | `be34a305ae1023c4417a91eb69dcdbb8` | yes | `4f7a1c5aa50342c6…`|
| `output/7654321/out/stdout/out.txt` | 37 | `cb6788c34c3e9ca9294470940ce43eb5` | yes | `913f455f7304e179…`|
```

Those SHA-256 values were checked against `shasum -a 256` run independently on
the downloaded files. They match.

The provenance section changes to match what happened:

```text
This run executed in AWS HealthOmics. Replaying it requires the same account,
execution role and container images. The identifiers here pin what the run WAS.
Every one of the 3 output object(s) was downloaded and hashed; the sha256
values in tables/outputs.csv are real checksums of those bytes, computed here
rather than reported by AWS.
```

Without verification it says the opposite, and says how to fix it:

```text
No checksum in this bundle covers the run's outputs, which remain in S3 and
were not read. Add `--verify-outputs` to record what the run produced.
```

---

## 5. Bring the results home

```bash
--download-outputs 7654321 --to ./results/ --confirm-download
```

Unconfirmed, it prices the transfer first:

```text
ESTIMATE ONLY: 3 object(s), 1,298 bytes under s3://my-bucket/output/7654321/.
Nothing was downloaded. Re-run with --confirm-download to transfer
(S3 egress is billable).
```

Confirmed, it writes them preserving layout:

```markdown
## Download

**Source**: `s3://my-bucket/output/7654321/`

3 of 3 object(s) written to `/path/to/results`.
```

```text
results/
├── logs/engine.log
├── logs/outputs.json
└── out/stdout/out.txt
```

From here the outputs are ordinary local files, ready for
`variant-annotation`, `rnaseq-de`, `scrna-orchestrator` or anything else.

---

## What this example does not cover

- **Container build, push, and the ECR repository policy.** One-time
  environment setup, deliberately outside this skill — a permission change is
  not something a run-lifecycle tool should make. Both failure modes are
  documented in [GOTCHAS.md](GOTCHAS.md#containers-for-private-workflows).
- **Deleting the workflow afterwards.** Barred by consequence; the report
  prints the `aws omics delete-workflow` command instead.
