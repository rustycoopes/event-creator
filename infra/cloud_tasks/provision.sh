#!/usr/bin/env bash
# Slice R11 redesign — provisions the Cloud Tasks queues that replace the Celery/Redis worker
# for pipeline dispatch. See docs/adr/0001-event-creator-worker-cpu-throttling.md (organize-me
# repo) for why: a separate always-on worker process crash-loops under Cloud Run's request-based
# CPU throttling. Cloud Tasks turns each pipeline run into a genuine inbound HTTP request to this
# same service instead, so CPU is only ever billed for the duration of an actual run.
#
# This is a manual, one-time (re-runnable) operator script — not part of CI/CD, mirrors
# infra/gcp_lb/provision.sh's own pattern (organize-me repo). Run it once per environment, after
# that environment's Cloud Run service has been deployed at least once (queue creation doesn't
# depend on it, but the IAM grants below target the service by name):
#
#   gcloud auth login
#   gcloud config set project gen-lang-client-0791944342
#   bash infra/cloud_tasks/provision.sh qa
#   bash infra/cloud_tasks/provision.sh prod
#
# Idempotent: every step checks whether its resource/binding already exists before creating it.

set -euo pipefail

ENVIRONMENT="${1:?usage: provision.sh <qa|prod>}"
if [[ "$ENVIRONMENT" != "qa" && "$ENVIRONMENT" != "prod" ]]; then
  echo "error: environment must be 'qa' or 'prod', got '$ENVIRONMENT'" >&2
  exit 1
fi

PROJECT_ID="gen-lang-client-0791944342"
REGION="northamerica-northeast1"
QUEUE_NAME="event-creator-pipeline-${ENVIRONMENT}"
RUN_SERVICE="event-creator-${ENVIRONMENT}"
# The only GCP identity Cloud Run containers run as today (see
# docs/platform-restructure/secrets-and-accounts.md in organize-me) — reused here as both the
# identity that enqueues tasks and the OIDC identity Cloud Tasks presents when it pushes back to
# the service, rather than introducing a new per-service account this repo hasn't adopted yet.
DEPLOY_SA="170051512639-compute@developer.gserviceaccount.com"

gcloud config set project "$PROJECT_ID" >/dev/null

echo "== 1. Cloud Tasks queue ($QUEUE_NAME) =="
if ! gcloud tasks queues describe "$QUEUE_NAME" --location="$REGION" >/dev/null 2>&1; then
  gcloud tasks queues create "$QUEUE_NAME" \
    --location="$REGION" \
    --max-concurrent-dispatches=1 \
    --max-attempts=3
else
  # Re-runnable: keep an existing queue's concurrency/retry settings in sync. Note
  # max-concurrent-dispatches=1 only bounds concurrency - it does not guarantee dispatch
  # *order* (Cloud Tasks documents order as best-effort by schedule time, not a guarantee, and a
  # retry on an earlier item can let a later item's task become eligible first). The strict,
  # in-order batch-import requirement (#110) is met by explicit chaining in
  # app.api.v1.internal_pipeline instead - see that module's docstring.
  gcloud tasks queues update "$QUEUE_NAME" \
    --location="$REGION" \
    --max-concurrent-dispatches=1 \
    --max-attempts=3
fi

echo "== 2. Grant $DEPLOY_SA roles/cloudtasks.enqueuer (create tasks) =="
gcloud tasks queues add-iam-policy-binding "$QUEUE_NAME" \
  --location="$REGION" \
  --member="serviceAccount:${DEPLOY_SA}" \
  --role="roles/cloudtasks.enqueuer" \
  --condition=None >/dev/null

echo "== 3. Grant $DEPLOY_SA roles/run.invoker on $RUN_SERVICE (Cloud Tasks' OIDC push target) =="
gcloud run services add-iam-policy-binding "$RUN_SERVICE" \
  --region="$REGION" \
  --member="serviceAccount:${DEPLOY_SA}" \
  --role="roles/run.invoker" \
  --condition=None >/dev/null

echo "Done. Queue: $QUEUE_NAME (location=$REGION, max-concurrent-dispatches=1, max-attempts=3)"
echo "Confirm the deployed service's env vars include CLOUD_TASKS_QUEUE=$QUEUE_NAME,"
echo "CLOUD_TASKS_LOCATION=$REGION, GCP_PROJECT_ID=$PROJECT_ID, PIPELINE_INVOKER_SERVICE_ACCOUNT=$DEPLOY_SA."
