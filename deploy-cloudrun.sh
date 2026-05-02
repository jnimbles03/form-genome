#!/bin/bash
# Deploy form-genome to Google Cloud Run with all fixes

set -e  # Exit on error

# Configuration
PROJECT_ID="formgenome"
REGION="us-central1"
SERVICE_NAME="form-genome"
IMAGE_NAME="gcr.io/${PROJECT_ID}/${SERVICE_NAME}:latest"

echo "========================================="
echo "  Deploying Form Genome to Cloud Run"
echo "========================================="
echo "Project: $PROJECT_ID"
echo "Service: $SERVICE_NAME"
echo "Region: $REGION"
echo

# Step 1: Build Docker image
echo "📦 Step 1: Building Docker image..."
docker build -t ${IMAGE_NAME} .

# Step 2: Push to Google Container Registry
echo ""
echo "⬆️  Step 2: Pushing to Container Registry..."
docker push ${IMAGE_NAME}

# Step 3: Deploy to Cloud Run
echo ""
echo "🚀 Step 3: Deploying to Cloud Run..."
gcloud run deploy ${SERVICE_NAME} \
  --image ${IMAGE_NAME} \
  --platform managed \
  --region ${REGION} \
  --allow-unauthenticated \
  --memory 2Gi \
  --cpu 2 \
  --timeout 300 \
  --max-instances 10 \
  --set-env-vars "CLOUD_SQL_CONNECTION_NAME=formgenome:us-central1:formgenome-db,DB_NAME=postgres,DB_USER=postgres,FLASK_ENV=production" \
  --set-secrets "DB_PASSWORD=formgenome-db-password:latest,ADMIN_PIN=admin-pin:latest,SECRET_KEY=flask-secret-key:latest,OPENAI_API_KEY=openai-api-key:latest,ANTHROPIC_API_KEY=anthropic-api-key:latest,GOOGLE_CLIENT_ID=google-oauth-client-id:latest,GOOGLE_CLIENT_SECRET=google-oauth-client-secret:latest" \
  --add-cloudsql-instances formgenome:us-central1:formgenome-db

echo ""
echo "========================================="
echo "  ✅ Deployment Complete!"
echo "========================================="
echo ""
echo "Service URL:"
gcloud run services describe ${SERVICE_NAME} --region=${REGION} --format='value(status.url)'
echo ""
echo "View logs:"
echo "  gcloud run logs read ${SERVICE_NAME} --region=${REGION}"
echo ""
echo "🎉 Your updated app is now live with:"
echo "  ✓ Fixed column widths (150-200px)"
echo "  ✓ Enhanced title normalization"
echo "  ✓ Cloud SQL integration (5,693 records)"
echo "  ✓ LLM normalization API"
