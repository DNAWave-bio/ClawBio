# healthomics-bridge golden workflows

These are copyable dry-run-first patterns for the workflows this bridge is most
likely to drive. Replace ids, bucket names, roles and params with account-local
values before adding any confirmation flag.

## Ready2Run discovery

```bash
python skills/healthomics-bridge/healthomics_bridge.py \
  --recommend-workflow "protein folding from FASTA" \
  --workflow-type READY2RUN \
  --output out/healthomics-recommend
```

Then inspect the top id:

```bash
python skills/healthomics-bridge/healthomics_bridge.py \
  --params-template <workflow-id> \
  --workflow-type READY2RUN \
  --output out/healthomics-template
```

## Ready2Run submit, estimate first

```bash
python skills/healthomics-bridge/healthomics_bridge.py \
  --start-run <workflow-id> \
  --workflow-type READY2RUN \
  --params params.json \
  --output-uri s3://<bucket>/healthomics/output/ \
  --role-arn arn:aws:iam::<account>:role/<omics-role> \
  --run-name ready2run-trial \
  --allow-remote-inputs \
  --output out/ready2run-estimate
```

Add `--confirm-submit --wait` only after the request and estimate look right.

## Private WDL registration

```bash
python skills/healthomics-bridge/healthomics_bridge.py \
  --register main.wdl \
  --workflow-name private-wdl-demo \
  --output out/private-wdl-dry-run
```

Add `--confirm-register` to create the persistent private workflow.

## Private workflow version

```bash
python skills/healthomics-bridge/healthomics_bridge.py \
  --register main.wdl \
  --workflow-id <workflow-id> \
  --new-version-name v2 \
  --output out/private-wdl-version-dry-run
```

Add `--confirm-register` to create the version.

## Verify and hand off outputs

```bash
python skills/healthomics-bridge/healthomics_bridge.py \
  --run-status <run-id> \
  --verify-outputs manifest \
  --output out/run-status
```

For real SHA-256 hashes of output bytes:

```bash
python skills/healthomics-bridge/healthomics_bridge.py \
  --run-status <run-id> \
  --verify-outputs deep \
  --confirm-download \
  --to out/downloaded-files \
  --output out/run-status-deep
```

The deep bundle writes `outputs.json` and `handoff.json` for downstream ClawBio
skills such as `variant-annotation`, `vcf-annotator`, `rnaseq-de`,
`scrna-orchestrator` and `multiqc-reporter`.

## Govern run tags

```bash
python skills/healthomics-bridge/healthomics_bridge.py \
  --list-tags <run-id> \
  --output out/tags
```

To make a run's cost/allocation metadata exactly match a desired JSON object:

```bash
python skills/healthomics-bridge/healthomics_bridge.py \
  --sync-tags <run-id> \
  --tags '{"team":"genomics","project":"atlas","environment":"dev"}' \
  --output out/sync-tags
```

`--sync-tags` reads current tags, sets changed keys, and removes stale keys. The
bundle writes `tables/tags.csv`, and the replay command carries the desired JSON
so the metadata state is auditable.
