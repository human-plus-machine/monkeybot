#!/bin/bash
# deploy.sh - Deploy monkey-bot agent to GCP Cloud Run
#
# This script handles the complete deployment lifecycle:
# - Service account creation with proper IAM roles
# - Docker image build via Cloud Build (no local Docker needed)
# - Cloud Run deployment with secrets from Secret Manager
# - Verification and health checks
#
# Usage:
#   ./deploy.sh [options]
#
# Options:
#   --project PROJECT_ID      GCP project ID (default: aurigaos)
#   --region REGION           GCP region (default: us-central1)
#   --service SERVICE_NAME    Cloud Run service name (default: monkey-bot)
#   --memory MEMORY           Memory allocation (default: 512Mi)
#   --cpu CPU                 CPU allocation (default: 1)
#   --min-instances N         Minimum instances (default: 0)
#   --max-instances N         Maximum instances (default: 1)
#   --skip-confirm            Skip confirmation prompt
#   --storage-backend TYPE    Scheduler storage backend: json|firestore (default: json)
#
# Example:
#   ./deploy.sh --project my-project --service my-bot --memory 1Gi

set -e  # Exit on error

# Default configuration (can be overridden via CLI args)
PROJECT_ID="${PROJECT_ID:-aurigaos}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-monkey-bot}"
MEMORY="512Mi"
CPU="1"
MIN_INSTANCES="0"
MAX_INSTANCES="1"
SKIP_CONFIRM="false"
STORAGE_BACKEND="json"

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --project)
            PROJECT_ID="$2"
            shift 2
            ;;
        --region)
            REGION="$2"
            shift 2
            ;;
        --service)
            SERVICE_NAME="$2"
            shift 2
            ;;
        --memory)
            MEMORY="$2"
            shift 2
            ;;
        --cpu)
            CPU="$2"
            shift 2
            ;;
        --min-instances)
            MIN_INSTANCES="$2"
            shift 2
            ;;
        --max-instances)
            MAX_INSTANCES="$2"
            shift 2
            ;;
        --storage-backend)
            STORAGE_BACKEND="$2"
            shift 2
            ;;
        --skip-confirm)
            SKIP_CONFIRM="true"
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Run './deploy.sh --help' for usage"
            exit 1
            ;;
    esac
done

# Derived configuration
IMAGE_NAME="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"
SERVICE_ACCOUNT_NAME="${SERVICE_NAME}-sa"
SERVICE_ACCOUNT_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${GREEN}🚀 Deploying ${SERVICE_NAME} to GCP Cloud Run${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "${BLUE}Configuration:${NC}"
echo -e "  Project:         ${GREEN}${PROJECT_ID}${NC}"
echo -e "  Region:          ${GREEN}${REGION}${NC}"
echo -e "  Service:         ${GREEN}${SERVICE_NAME}${NC}"
echo -e "  Image:           ${GREEN}${IMAGE_NAME}${NC}"
echo -e "  Service Account: ${GREEN}${SERVICE_ACCOUNT_EMAIL}${NC}"
echo -e "  Memory:          ${GREEN}${MEMORY}${NC}"
echo -e "  CPU:             ${GREEN}${CPU}${NC}"
echo -e "  Instances:       ${GREEN}${MIN_INSTANCES}-${MAX_INSTANCES}${NC}"
echo -e "  Storage Backend: ${GREEN}${STORAGE_BACKEND}${NC}"
echo ""

# Pre-deployment checklist
if [[ "$SKIP_CONFIRM" != "true" ]]; then
    echo -e "${YELLOW}📋 Pre-deployment Checklist${NC}"
    echo "1. Have you set all required secrets in GCP Secret Manager?"
    echo "   - vertex-ai-project-id"
    echo "   - allowed-users"
    echo "2. Have you tested all features locally?"
    echo "3. Have you committed all changes to git?"
    echo "4. Is your .env.example up to date?"
    echo ""
    read -p "Continue with deployment? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${RED}Deployment cancelled${NC}"
        exit 1
    fi
    echo ""
fi

# Step 0: Ensure service account exists with proper permissions
echo -e "${GREEN}🔐 Step 1/4: Checking service account...${NC}"
if gcloud iam service-accounts describe ${SERVICE_ACCOUNT_EMAIL} --project ${PROJECT_ID} >/dev/null 2>&1; then
    echo -e "${GREEN}✓ Service account ${SERVICE_ACCOUNT_EMAIL} already exists${NC}"
else
    echo -e "${YELLOW}⚙️  Creating service account ${SERVICE_ACCOUNT_EMAIL}...${NC}"
    gcloud iam service-accounts create ${SERVICE_ACCOUNT_NAME} \
        --display-name "${SERVICE_NAME} Service Account" \
        --description "Service account for ${SERVICE_NAME} Cloud Run service" \
        --project ${PROJECT_ID}
    echo -e "${GREEN}✓ Service account created${NC}"
fi

# Grant necessary permissions to the service account
echo -e "${GREEN}🔑 Ensuring service account has necessary permissions...${NC}"

# Core permissions needed by all monkey-bot deployments
REQUIRED_ROLES=(
    "roles/secretmanager.secretAccessor"  # Access secrets from Secret Manager
    "roles/run.invoker"                   # Invoke Cloud Run services
    "roles/aiplatform.user"               # Call Vertex AI models
)

# Add Firestore role if using Firestore backend
if [[ "$STORAGE_BACKEND" == "firestore" ]]; then
    REQUIRED_ROLES+=("roles/datastore.user")  # Access Firestore
fi

for role in "${REQUIRED_ROLES[@]}"; do
    echo -e "  Granting ${role}..."
    gcloud projects add-iam-policy-binding ${PROJECT_ID} \
        --member="serviceAccount:${SERVICE_ACCOUNT_EMAIL}" \
        --role="${role}" \
        --condition=None \
        >/dev/null 2>&1 || true
done

echo -e "${GREEN}✓ Permissions configured${NC}"
echo ""

# Step 1: Build Docker image using Cloud Build (no local Docker needed!)
echo -e "${GREEN}📦 Step 2/4: Building Docker image with Cloud Build...${NC}"
gcloud builds submit --tag ${IMAGE_NAME}:latest --project ${PROJECT_ID} .
echo -e "${GREEN}✓ Image built successfully${NC}"
echo ""

# Step 2: Deploy to Cloud Run
echo -e "${GREEN}🚢 Step 3/4: Deploying to Cloud Run...${NC}"

# Base deploy command
DEPLOY_CMD="gcloud run deploy ${SERVICE_NAME} \
    --image ${IMAGE_NAME}:latest \
    --platform managed \
    --region ${REGION} \
    --project ${PROJECT_ID} \
    --service-account ${SERVICE_ACCOUNT_EMAIL} \
    --allow-unauthenticated \
    --memory ${MEMORY} \
    --cpu ${CPU} \
    --timeout 300 \
    --concurrency 1 \
    --max-instances ${MAX_INSTANCES} \
    --min-instances ${MIN_INSTANCES} \
    --set-env-vars ENVIRONMENT=production,SCHEDULER_STORAGE=${STORAGE_BACKEND}"

# Add secrets
# Note: Adjust this list based on your actual secrets in Secret Manager
DEPLOY_CMD="${DEPLOY_CMD} \
    --set-secrets VERTEX_AI_PROJECT_ID=vertex-ai-project-id:latest,ALLOWED_USERS=allowed-users:latest"

# Execute deployment
eval ${DEPLOY_CMD}

echo -e "${GREEN}✓ Deployment successful${NC}"
echo ""

# Step 3: Verify deployment
echo -e "${GREEN}🔍 Step 4/4: Verifying deployment...${NC}"

# Get service URL
SERVICE_URL=$(gcloud run services describe ${SERVICE_NAME} \
    --platform managed \
    --region ${REGION} \
    --project ${PROJECT_ID} \
    --format 'value(status.url)')

echo -e "  Service URL: ${GREEN}${SERVICE_URL}${NC}"

# Test health endpoint
echo -e "  Testing health endpoint..."
if curl -s -f "${SERVICE_URL}/health" >/dev/null 2>&1; then
    echo -e "${GREEN}✓ Health check passed${NC}"
else
    echo -e "${YELLOW}⚠️  Health check failed (this may be normal if service is still starting)${NC}"
fi

echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}✅ Deployment Complete!${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "${BLUE}Service Information:${NC}"
echo -e "  URL:         ${GREEN}${SERVICE_URL}${NC}"
echo -e "  Health:      ${GREEN}${SERVICE_URL}/health${NC}"
echo -e "  Webhook:     ${GREEN}${SERVICE_URL}/webhook${NC}"
echo -e "  Cron Tick:   ${GREEN}${SERVICE_URL}/cron/tick${NC}"
echo ""
echo -e "${YELLOW}📝 Post-deployment Steps:${NC}"
echo ""
echo -e "${YELLOW}1. Test Health Endpoint:${NC}"
echo -e "   curl ${SERVICE_URL}/health"
echo ""
echo -e "${YELLOW}2. Monitor Logs:${NC}"
echo -e "   gcloud logging read 'resource.type=cloud_run_revision AND resource.labels.service_name=${SERVICE_NAME}' --limit 50 --format json --project ${PROJECT_ID}"
echo ""
echo -e "${YELLOW}3. Configure Google Chat (if using):${NC}"
echo -e "   - Create a webhook in your Google Chat space"
echo -e "   - Set webhook URL to: ${SERVICE_URL}/webhook"
echo -e "   - Add GOOGLE_CHAT_WEBHOOK secret to Secret Manager (for bot-initiated messages)"
echo ""
echo -e "${YELLOW}4. Set up Cloud Scheduler (for background jobs):${NC}"
echo -e "   gcloud scheduler jobs create http ${SERVICE_NAME}-tick \\"
echo -e "       --location ${REGION} \\"
echo -e "       --schedule '*/1 * * * *' \\"
echo -e "       --uri '${SERVICE_URL}/cron/tick' \\"
echo -e "       --http-method POST \\"
echo -e "       --project ${PROJECT_ID}"
echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "${GREEN}For detailed documentation, see docs/getting-started.md and .env.example${NC}"
echo ""
