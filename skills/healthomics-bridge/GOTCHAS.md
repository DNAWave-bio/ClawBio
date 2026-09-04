# healthomics-bridge — the long gotchas

`SKILL.md` carries the short ones. These two need more room than a bullet, and
both were hit live rather than imagined.

## Containers for private workflows

A private workflow runs your own container image from ECR. Two things go wrong
the first time, and neither error message says what is actually wrong.

### 1. Architecture: build for `linux/amd64`, always

Building on Apple Silicon produces an `arm64` image. HealthOmics compute is
`x86_64` only, and every task fails with:

```
exec /bin/bash: exec format error
```

The failure surfaces from *inside* the container — well past this skill's own
gates. The request was correct, `--confirm-submit` succeeded, the run started,
and it still failed. Nothing local can catch it.

```bash
docker buildx build --platform linux/amd64 -t <account>.dkr.ecr.<region>.amazonaws.com/<repo>:latest --push .
```

Check an existing image before blaming anything else:

```bash
aws ecr describe-images --repository-name <repo> \
  --query 'imageDetails[].imageManifestMediaType'
```

### 2. The ECR repository policy is a *second*, separate grant

A submission can fail with:

```
Unable to access image URI: <uri>. Ensure the ECR private repository exists
and has granted access for the omics service principle to access the repository.
```

...even when your execution role already holds `ecr:BatchGetImage` on that
repository. **Role permissions and the repository policy are two different
grants and HealthOmics needs both**: the role lets your identity call ECR, the
repository policy lets `omics.amazonaws.com` itself reach the repository.

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

```bash
aws ecr set-repository-policy --repository-name <repo> --policy-text file://policy.json
aws ecr get-repository-policy --repository-name <repo>   # verify
```

**This skill will never do that for you.** Setting a repository policy is a
permission change: its blast radius extends beyond the current task, and it is
the classic privilege-escalation vector. `put_bucket_policy` and friends are
barred by name in `s3_client.py`, and the equivalent ECR call is not reachable
at all. Container build, push and grant are one-time environment setup, not
per-run work — the boundary is deliberate, not an omission.

Verify both *before* `--confirm-submit`. A run that fails on a bad image still
bills for the compute that never ran your workload.

## Why there is no `--lint`

The HealthOmics API has no lint or validate operation — verified against
botocore's own service model, not assumed. `--register` therefore cannot check
a definition before creating it. AWS validates server-side instead: the
workflow is created and then settles on `ACTIVE` or `FAILED`, and this skill
polls until it does and reports which. A `FAILED` registration means AWS
rejected the archive; check that the entrypoint filename matches what the
engine expects.

Registration bills nothing, so a failed attempt costs only the round trip.
