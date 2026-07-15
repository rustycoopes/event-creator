<#
.SYNOPSIS
Slice R11 redesign — provisions the Cloud Tasks queues that replace the Celery/Redis worker for
pipeline dispatch. See docs/adr/0001-event-creator-worker-cpu-throttling.md (organize-me repo).

.DESCRIPTION
Windows PowerShell equivalent of provision.sh — same steps, same idempotency pattern, same
resource names. Keep both scripts in sync; provision.sh is the canonical version (this repo's
CI/CD runs on Linux), this one exists so an operator on Windows doesn't need WSL/Git Bash for a
one-time manual step.

This is a manual, one-time (re-runnable) operator script — not part of CI/CD. Run it once per
environment, after that environment's Cloud Run service has been deployed at least once:

    gcloud auth login
    gcloud config set project gen-lang-client-0791944342
    .\infra\cloud_tasks\provision.ps1 -Environment qa
    .\infra\cloud_tasks\provision.ps1 -Environment prod
#>

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("qa", "prod")]
    [string]$Environment
)

$ErrorActionPreference = "Stop"

$ProjectId = "gen-lang-client-0791944342"
$Region = "northamerica-northeast1"
$QueueName = "event-creator-pipeline-$Environment"
$RunService = "event-creator-$Environment"
# The only GCP identity Cloud Run containers run as today (see
# docs/platform-restructure/secrets-and-accounts.md in organize-me) — reused here as both the
# identity that enqueues tasks and the OIDC identity Cloud Tasks presents when it pushes back to
# the service, rather than introducing a new per-service account this repo hasn't adopted yet.
$DeploySa = "170051512639-compute@developer.gserviceaccount.com"

gcloud config set project $ProjectId | Out-Null

Write-Host "== 1. Cloud Tasks queue ($QueueName) =="
$queueExists = $true
try {
    gcloud tasks queues describe $QueueName --location=$Region 2>$null | Out-Null
} catch {
    $queueExists = $false
}
if (-not $queueExists) {
    gcloud tasks queues create $QueueName `
        --location=$Region `
        --max-concurrent-dispatches=1 `
        --max-attempts=3
} else {
    # Re-runnable: keep an existing queue's concurrency/retry settings in sync. Note
    # max-concurrent-dispatches=1 only bounds concurrency - it does not guarantee dispatch
    # *order* (Cloud Tasks documents order as best-effort by schedule time, not a guarantee, and
    # a retry on an earlier item can let a later item's task become eligible first). The strict,
    # in-order batch-import requirement (#110) is met by explicit chaining in
    # app.api.v1.internal_pipeline instead - see that module's docstring.
    gcloud tasks queues update $QueueName `
        --location=$Region `
        --max-concurrent-dispatches=1 `
        --max-attempts=3
}

Write-Host "== 2. Grant $DeploySa roles/cloudtasks.enqueuer (create tasks) =="
gcloud tasks queues add-iam-policy-binding $QueueName `
    --location=$Region `
    --member="serviceAccount:$DeploySa" `
    --role="roles/cloudtasks.enqueuer" `
    --condition=None | Out-Null

Write-Host "== 3. Grant $DeploySa roles/run.invoker on $RunService (Cloud Tasks' OIDC push target) =="
gcloud run services add-iam-policy-binding $RunService `
    --region=$Region `
    --member="serviceAccount:$DeploySa" `
    --role="roles/run.invoker" `
    --condition=None | Out-Null

Write-Host "Done. Queue: $QueueName (location=$Region, max-concurrent-dispatches=1, max-attempts=3)"
Write-Host "Confirm the deployed service's env vars include CLOUD_TASKS_QUEUE=$QueueName,"
Write-Host "CLOUD_TASKS_LOCATION=$Region, GCP_PROJECT_ID=$ProjectId, PIPELINE_INVOKER_SERVICE_ACCOUNT=$DeploySa."
