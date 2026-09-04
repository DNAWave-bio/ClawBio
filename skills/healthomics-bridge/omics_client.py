"""Allow-listed boto3 client for AWS HealthOmics.

Botocore *operation names* are refused here, before they reach AWS, so the
skill's blast radius is a property of this module rather than of the caller's
discipline. A refused operation never becomes a request.

The allowlist is drawn by CONSEQUENCE, not by verb prefix. That distinction
matters: ``StartRun`` spends real money and is allowed (behind two gates),
while ``CreateRunCache`` is cheap and is not, because it mutates shared account
state outside one run's lifecycle. Sorting on the verb would get both backwards.
"""

from __future__ import annotations

from typing import Any, Protocol

# The only HealthOmics operations this skill may call. Every entry is read-only
# except StartRun, which is gated behind --allow-remote-inputs and
# --confirm-submit.
#
# Gated by CONSEQUENCE, not by verb prefix. Deliberately absent, and unreachable at any gate: every Delete*
# (destroys state this skill cannot restore), CancelRun (destroys in-flight
# work), every Update* (mutates shared config), the sequence/reference store
# and read-set import/export families, and run group / run cache mutation.
#
# CreateWorkflow was previously absent because this skill had no --register
# mode to reach it, and an allowlist entry no code path can invoke overstates
# the blast radius. --register now exists, so it is back — gated behind
# --confirm-register. By the consequence rule it belongs on the allowed side:
# it creates one cheap, reversible, in-domain resource and bills nothing, which
# is a smaller consequence than StartRun's, and StartRun is allowed.
# The allowlist holds exactly the operations code paths actually call, enforced
# by test_every_allow_listed_operation_is_actually_reachable.
#
# GetRunTask *was* dropped on the reasoning that ListRunTasks already returns
# each task's status, resources and timings. That reasoning missed one field:
# ListRunTasks does not return statusMessage or failureReason, so a failed
# task's own reason was unreachable and the "Failed tasks" section could name
# a task but never say why. A live private-workflow run exposed this -- the
# run-level statusMessage came through, the task-level one did not, even
# though GetRunTask held it (failureReason: RUN_TASK_FAILED) the whole time.
ALLOWED_OPERATIONS = frozenset(
    {
        "ListRuns",
        "GetRun",
        "ListRunTasks",
        "GetRunTask",
        "ListWorkflows",
        "GetWorkflow",
        "ListTagsForResource",
        "CreateWorkflow",
        "StartRun",
    }
)

# Named rather than pattern-matched, so the rule survives an API rename and so
# a reader can see *why* each one is barred rather than inferring it.
PERMANENTLY_EXCLUDED: dict[str, str] = {
    "DeleteRun": "destroys state this skill cannot restore",
    "DeleteWorkflow": "destroys state this skill cannot restore",
    "DeleteRunGroup": "destroys state this skill cannot restore",
    "DeleteRunCache": "destroys state this skill cannot restore",
    "DeleteSequenceStore": "destroys state this skill cannot restore",
    "CancelRun": "destroys in-flight work",
    "UpdateRunGroup": "mutates shared account state outside one run's lifecycle",
    "UpdateRunCache": "mutates shared account state outside one run's lifecycle",
    "UpdateWorkflow": "mutates a workflow this skill did not necessarily create",
    "CreateRunGroup": "mutates shared account state outside one run's lifecycle",
    "CreateRunCache": "mutates shared account state outside one run's lifecycle",
    "CreateSequenceStore": "storage administration, a different job",
    "StartReadSetImportJob": "storage administration, a different job",
}


class OmicsCallError(RuntimeError):
    """A HealthOmics operation failed or was refused."""


class OperationNotAllowed(OmicsCallError):
    """An operation outside ALLOWED_OPERATIONS was requested and refused."""


class OmicsClient(Protocol):
    """The narrow surface this skill's own functions depend on.

    Declared as a Protocol so every test can substitute a recording fake, and
    so no offline test constructs a real boto3 client or reads a credential.
    """

    def call(self, operation: str, **kwargs: Any) -> dict[str, Any]: ...


def build_boto_client(region: str | None = None, profile: str | None = None) -> Any:
    """Create the underlying boto3 omics client.

    Imported lazily and reported actionably when absent, so the offline demo
    runs with no cloud dependency installed at all.

    Credentials are never read, stored or forwarded by this skill — boto3
    resolves them itself from the standard chain (profile, environment,
    instance role), exactly as the AWS CLI would.
    """
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - exercised by absence, not by tests
        raise OmicsCallError(
            "Live HealthOmics calls need boto3. Install it with "
            "`uv pip install boto3`. The offline demo (--demo) needs no "
            "cloud dependency at all."
        ) from exc

    from botocore.config import Config

    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return session.client("omics", region_name=region, config=_RETRY_CONFIG(Config))


def _RETRY_CONFIG(Config: Any) -> Any:
    """Say how this skill retries rather than inheriting whatever botocore
    defaults to.

    HealthOmics throttles, and ``--wait`` polls ``GetRun`` for as long as a run
    takes — the exact shape that trips a rate limit. botocore's ``legacy`` mode
    is the default and retries a narrower set of errors with no client-side
    rate limiting; ``adaptive`` adds it, which is what a polling caller wants.
    Ten attempts rather than five because a throttle during a half-hour poll
    should slow the skill down, not end it.

    This is a deliberate, stated policy: an inherited default is a decision
    nobody made.
    """
    return Config(retries={"mode": "adaptive", "max_attempts": 10})
