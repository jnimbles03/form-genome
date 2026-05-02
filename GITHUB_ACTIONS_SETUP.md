# GitHub Actions → Cloud Run via Workload Identity Federation

The `.github/workflows/deploy.yml` workflow is committed but won't run
successfully until the GCP-side wiring below is in place. This is a
one-time setup; after it works once, every future merge to `main`
(once you uncomment the `push` trigger) auto-deploys.

## Why WIF instead of a JSON service-account key

Service-account JSON keys stored as GitHub secrets are a common
foot-gun: they're long-lived, easy to leak, and impossible to scope
to a single repo. Workload Identity Federation issues short-lived
tokens that are valid only for runs from a specific repo + branch +
workflow. No long-lived secret ever leaves GCP.

## One-time GCP setup

You'll run these once. They require `gcloud` authenticated as someone
with project owner / IAM admin on `formgenome`.

```bash
PROJECT_ID=formgenome
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
GITHUB_OWNER=jnimbles03
GITHUB_REPO=form-genome
SA_NAME=github-deployer
SA_EMAIL=$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com

# 1. Create a deployer service account
gcloud iam service-accounts create $SA_NAME \
  --display-name="GitHub Actions deployer for $GITHUB_REPO" \
  --project=$PROJECT_ID

# 2. Grant it the minimum roles needed for `gcloud builds submit` +
#    `gcloud run deploy` against an IAP-protected Cloud Run service.
for ROLE in \
  roles/run.admin \
  roles/cloudbuild.builds.editor \
  roles/storage.admin \
  roles/artifactregistry.writer \
  roles/secretmanager.secretAccessor \
  roles/cloudsql.client \
  roles/iam.serviceAccountUser; do
  gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:$SA_EMAIL" \
    --role="$ROLE" \
    --condition=None
done

# 3. Create a Workload Identity Pool + Provider for GitHub
gcloud iam workload-identity-pools create github-pool \
  --location=global \
  --display-name="GitHub Actions" \
  --project=$PROJECT_ID

gcloud iam workload-identity-pools providers create-oidc github-provider \
  --location=global \
  --workload-identity-pool=github-pool \
  --display-name="GitHub OIDC" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
  --attribute-condition="assertion.repository=='${GITHUB_OWNER}/${GITHUB_REPO}'" \
  --project=$PROJECT_ID

# 4. Allow the GitHub repo's identity to impersonate the deployer SA
gcloud iam service-accounts add-iam-policy-binding $SA_EMAIL \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/attribute.repository/$GITHUB_OWNER/$GITHUB_REPO" \
  --project=$PROJECT_ID

# 5. Print the values you'll paste into GitHub repo variables
echo
echo "GitHub repo variables to set:"
echo "  GCP_WIF_PROVIDER=projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/providers/github-provider"
echo "  GCP_DEPLOYER_SA=$SA_EMAIL"
```

## Add to GitHub

Take the two values printed by the last command and add them as
**repository variables** (not secrets — they're not sensitive):

1. https://github.com/jnimbles03/form-genome/settings/variables/actions
2. Click "New repository variable"
3. Name: `GCP_WIF_PROVIDER` → Value: the `projects/.../providers/github-provider` string
4. Repeat for `GCP_DEPLOYER_SA` → `github-deployer@formgenome.iam.gserviceaccount.com`

## Verify with a manual deploy

1. Go to https://github.com/jnimbles03/form-genome/actions
2. Pick "Deploy to Cloud Run" in the left sidebar
3. Click "Run workflow" → "Run workflow"
4. Watch the run. The "Authenticate to Google Cloud" step should
   succeed within seconds; build + deploy take ~3–5 minutes.

If auth fails, the most common causes:

| Symptom | Cause | Fix |
|---|---|---|
| `Permission 'iam.serviceAccounts.getAccessToken' denied` | Step 4 above didn't run | Re-run the `add-iam-policy-binding` for `roles/iam.workloadIdentityUser` |
| `Audience mismatch / no matching attribute condition` | `--attribute-condition` in step 3 doesn't match `${GITHUB_OWNER}/${GITHUB_REPO}` | Re-create the provider with the correct condition |
| Build fails immediately on `gcloud builds submit` | `roles/cloudbuild.builds.editor` not granted, OR `roles/storage.admin` missing | Re-run step 2 |

## After the first successful manual run

Edit `.github/workflows/deploy.yml` and uncomment the `push:` trigger:

```yaml
on:
  workflow_dispatch:
    inputs:
      reason:
        description: '...'
  push:                 # ← uncomment
    branches: [main]    # ← uncomment
```

Commit + push (via PR) and from then on, every merge to `main`
auto-deploys.

## Branch protection (recommended)

Once CI deploys are working, lock down `main`:

```bash
gh api -X PUT repos/jnimbles03/form-genome/branches/main/protection \
  --input - <<'JSON'
{
  "required_status_checks": null,
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": 0,
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

This requires a PR for any future change to `main`, blocks force-push,
blocks branch deletion. Bump `required_approving_review_count` to 1
once you have a second reviewer.

## Emergency: deploy from local machine

If GitHub Actions is down or you need to deploy out-of-band, the
existing `deploy-cloudrun.sh` still works:

```bash
./deploy-cloudrun.sh
```

It uses your local `gcloud` auth and produces an identical revision.
Note that local deploys bypass the `concurrency` lock, so coordinate
with anyone who might be running CI at the same time.
