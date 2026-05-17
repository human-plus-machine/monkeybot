#!/usr/bin/env bash
# Deploy the playground gateway to Cloud Run (Auriga-style QA: set GCP_PROJECT to your QA project).
#
# Prerequisites:
#   - gcloud auth (user or SA with Cloud Run Admin, Storage push to gcr.io, etc.)
#   - Docker credential helper for GCR (once per machine):  gcloud auth configure-docker gcr.io
#   - Cloud Run + Vertex enabled on the project; runtime SA needs roles/aiplatform.user (or
#     equivalent) for Vertex Gemini.
#
# Vertex project id is NOT baked into the image (avoid secrets in layers; project may differ per
# env). This script sets GOOGLE_CLOUD_PROJECT / VERTEX_AI_PROJECT_ID on the Cloud Run service.
# ADC for Gemini comes from the Cloud Run default service account — no API key in the image.
#
# Validate from your laptop: in playground/chat-ui, set VITE_GATEWAY_TARGET to the service URL
# (see playground/chat-ui/env.local.sample) and run npm run dev — the Vite proxy avoids CORS.
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="${GCP_PROJECT:-${GOOGLE_CLOUD_PROJECT:-}}"
REGION="${GCP_REGION:-us-central1}"
SERVICE="${CLOUD_RUN_SERVICE:-monkeybot-playground}"
REPO_ROOT_TAG="${IMAGE_TAG:-$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo manual)}"

if [[ -z "$PROJECT" ]]; then
  echo "Set GCP_PROJECT or GOOGLE_CLOUD_PROJECT to your target project (e.g. Auriga QA)." >&2
  exit 1
fi

DOCKER_PLATFORM="${DOCKER_PLATFORM:-linux/amd64}"

# Override with a full Artifact Registry or GCR URI if needed, e.g.
# IMAGE=us-central1-docker.pkg.dev/$PROJECT/monkeybot/playground:abc123
IMAGE="${IMAGE:-gcr.io/${PROJECT}/${SERVICE}:${REPO_ROOT_TAG}}"

echo "Building and pushing $IMAGE ($DOCKER_PLATFORM) from $ROOT ..."
docker buildx build \
  --platform "$DOCKER_PLATFORM" \
  -f "$ROOT/internal/Dockerfile.auriga" \
  -t "$IMAGE" \
  --push \
  "$ROOT"

echo "Deploying Cloud Run service $SERVICE in $REGION ..."
gcloud run deploy "$SERVICE" \
  --project="$PROJECT" \
  --region="$REGION" \
  --image="$IMAGE" \
  --allow-unauthenticated \
  --port=8080 \
  --memory=2Gi \
  --cpu=2 \
  --timeout=3600 \
  --min-instances=0 \
  --set-env-vars="SANDBOX_ENABLED=false,GOOGLE_CLOUD_PROJECT=${PROJECT},VERTEX_AI_PROJECT_ID=${PROJECT}" \
  --quiet

gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" \
  --format='value(status.url)'
