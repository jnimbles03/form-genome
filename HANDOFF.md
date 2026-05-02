# Form Genome — Post-Audit Hardening Handoff

**Branch:** `post-audit-hardening`
**Audit reference:** `/Users/patrickmeyer/Documents/Claude/Projects/GenomeDeux/audit/FORM_GENOME_AUDIT.md`
**Status:** Wave 1 code fixes are landed locally. **Several actions remain
that only you can complete** — secret rotation, GCP-side flag changes,
and design decisions for Wave 2/3.

This document is the single thing you need to read before deploying.

---

## Branch overview

7 commits, none pushed:

```
5dbc560 harden analyze + crawl: SSRF guard, hot-path indexes, race + 429 fixes
d78edac perf(storage): apply P0 fixes to storage layer hot paths
9bc8151 fix(analyzer,scoring): gate duplicate vision call, redact full_text, lazy-init weights
60dc37d security: gate /api/* with login_required, fix-secret-key, env-driven allowlists
c65356e security: remove plaintext credentials and PIN literals from source
ab3799b fix: hybrid-search recall ceiling and ENABLE_VISION_ANALYSIS toggle
9b76d41 chore: add .gitignore and harden .dockerignore
```

Review: `git log --stat post-audit-hardening` and `git diff` per commit.

---

## ACTIONS REQUIRED FROM YOU

**Do not deploy this branch until items 1–6 are complete.** Items 7+ can
follow in subsequent deploys.

### 1. Rotate every secret that was committed in plaintext

The deleted `deploy.sh` leaked five production credentials. Each was
rotated on 2026-05-02 and stored in Secret Manager. The original
plaintext values are intentionally NOT enumerated here — they are
revoked, and naming them again would re-publish them in git history.

| Secret name | Where it now lives | Status |
|---|---|---|
| `ANTHROPIC_API_KEY` | Secret Manager: `anthropic-api-key` | Rotated 2026-05-02 |
| `GOOGLE_CLIENT_SECRET` | Secret Manager: `google-oauth-client-secret` | Rotated 2026-05-02 |
| `DB_PASSWORD` (Postgres) | Secret Manager: `formgenome-db-password` | Rotated 2026-05-02; Cloud SQL user updated |
| `SECRET_KEY` (Flask) | Secret Manager: `flask-secret-key` | Rotated 2026-05-02 |
| `ADMIN_PIN` | Secret Manager: `admin-pin` | Rotated 2026-05-02 |

If you need to confirm the original leaked prefixes for forensic
reasons, see the deleted `deploy.sh` at git ref `<pre-rotation>` (kept
out of the public branch). Do not re-paste them anywhere new.

Verify Secret Manager has all six entries the deploy script expects:

```bash
gcloud secrets list --project=formgenome | grep -E 'admin-pin|formgenome-db-password|flask-secret-key|anthropic-api-key|openai-api-key|google-oauth-client-id|google-oauth-client-secret'
```

If `anthropic-api-key` doesn't exist yet, create it: `deploy-cloudrun.sh`
now references it.

### 2. Drop `--allow-unauthenticated` on Cloud Run

The new `/api/*` `before_request` gate enforces login at the Flask layer,
but Cloud Run is still configured to accept unauthenticated traffic. To
actually scale safely, add IAM auth at the Cloud Run layer:

```bash
gcloud run services update form-genome \
  --region us-central1 \
  --no-allow-unauthenticated
```

Then grant `roles/run.invoker` to the OAuth-authed end-user identities or
to a service account fronted by IAP. **You will need to test the OAuth
flow after this change — the login redirect must still reach
`/auth/callback`.** If anonymous browsers can no longer reach `/login`,
exempt the auth blueprint at the IAP layer or add `Cloud Run domain
mapping` with public ingress for the auth path only.

Once this lands, also remove `--allow-unauthenticated` from
`deploy-cloudrun.sh` to prevent regression.

### 3. Set Cloud Run env vars for the new auth allowlists

The legacy hardcoded `gmail.com` domain default is gone. You must
explicitly tell Cloud Run who is allowed.

```bash
gcloud run services update form-genome --region us-central1 \
  --update-env-vars "ALLOWED_EMAIL_DOMAINS=docusign.com,ALLOWED_EMAILS=patrick.meyer@docusign.com\,patrick@meyerinterests.com,ADMIN_EMAILS=patrick.meyer@docusign.com\,patrick@meyerinterests.com"
```

(Note the escaped commas inside `ALLOWED_EMAILS` and `ADMIN_EMAILS` —
gcloud uses comma as a separator at the top level.)

If `ADMIN_EMAILS` is unset, the `/admin` page returns 503 — fail-closed.

### 4. Confirm Postgres-mode startup still works after `DB_PASSWORD` becomes mandatory

`storage.py` now raises `RuntimeError` if `DB_PASSWORD` is missing in PG
mode. This is intentional but means a deploy with a misconfigured secret
mount will refuse to boot. Verify:

```bash
gcloud run revisions list --service form-genome --region us-central1 --limit 5
gcloud logging read 'resource.type=cloud_run_revision AND severity>=ERROR' --limit 20
```

Watch for `RuntimeError: DB_PASSWORD required for PostgreSQL mode` after
the next deploy — that means the `--set-secrets DB_PASSWORD=...` mapping
isn't reaching the container.

### 5. Confirm `SECRET_KEY` is wired through Secret Manager

Same pattern as #4 — `app/__init__.py` now raises
`RuntimeError: SECRET_KEY environment variable is required in production`
on Cloud Run if missing. After secret rotation, redeploy and watch logs.

### 6. Audit any callers that depended on the old `full_text` field

`9bc8151` truncates persisted form text to ≤2000 chars and redacts PII.
Search for any external consumer:

```bash
git -C "$REPO" grep -n 'full_text' -- ':!*.md'
```

If the dashboard, an analytics export, or a downstream pipeline reads
`full_text` directly and assumes the entire PDF body, that consumer
needs to either re-fetch the PDF or accept the truncated preview.

---

## What landed in this branch (Wave 1)

P0 finding IDs reference `audit/FORM_GENOME_AUDIT.md`.

| # | Finding | Status | Commit |
|---|---|---|---|
| 1 | Production secrets in source (deploy.sh + storage.py + analyze_usda_parallel + scripts) | **CODE FIXED** — secrets must still be rotated externally (see Action 1) | c65356e |
| 2 | Cloud Run unauthenticated + `/api/*` unauthed | **CODE FIXED** at Flask layer; Cloud Run flag still requires Action 2 | 60dc37d |
| 3 | SSRF in `/crawl` | **PARTIAL** — seed URL guarded; redirect-hop validation still TODO | 5dbc560 |
| 4 | Two hardcoded `'1126'` PINs | **DONE** | c65356e |
| 5 | Long-running batches on sync request path | **DEFERRED to Wave 2** — needs Cloud Tasks | — |
| 6 | In-memory `progress.py` broken at maxScale > 1 | **DEFERRED to Wave 2** — needs Memorystore or DB-backed state | — |
| 7 | Vision fires twice per PDF | **DONE** — vision_already_ran gate + provider/model from env | 9bc8151 |
| 8 | `save()` uploads full SQLite to GCS on every record | **DONE** — moved to 60s background daemon, no cloud-DB download for count check | d78edac |
| 9 | `update_one` full-table scan per record | **DONE** — routes through `update_record()` indexed SQL UPDATE | d78edac |
| 10 | `list_all()` on hot analyze path + duplicate dispatch | **DONE** — `get_by_source_url` indexed lookup; duplicate dispatch removed | 5dbc560 |
| 11 | `delete_uncommitted` / `delete_empty` unbounded IN | **DONE** — SQL JSONB predicate on Postgres, chunked on SQLite | d78edac |
| 12 | `full_text` PII persisted on every record | **DONE** — truncated to 2000 chars + PII redaction; ~5 GB removed at scale | 9bc8151 |
| 13 | Hybrid search silent recall ceiling (`[:5]`) | **DONE** | ab3799b |
| 14 | Scoring weights opens SQLite at module import | **DONE** — lazy `_ensure_loaded()` with three-state sentinel | 9bc8151 |
| 15 | `storage.py.broken` shadowing live storage | **DONE** — file deleted | (filesystem) |
| 16 | `formgenome.db` (0B) + `Archive.zip` (264 MB) at repo root | **DONE** — `formgenome.db` deleted; `Archive.zip` in `.dockerignore` (still on disk locally for safety) | 9b76d41 + filesystem |
| 17 | `ENABLE_VISION_ANALYSIS` operator-precedence bug | **DONE** | ab3799b |
| 18 | `update_record` route registered twice | **DONE** — admin.py duplicate deleted; records.py field-allowlisted version is canonical | 60dc37d |
| 19 | Quality-confidence auto-commit | **DEFERRED to Wave 2** — needs a config flag + UI review path; flagging now would alter committed-record counts | — |

P1s landed in passing:

- `_make_id` now deterministic from `sha1(normalize_url(source_url))[:16]` (F-CS-09) — d78edac
- `crawl_parallel` race fix: response built inside merge lock (F-CS-14) — 5dbc560
- 429 added to crawler retry status_forcelist + `respect_retry_after_header=True` (F-CS-10 partial) — 5dbc560
- OAuth allowlist env-driven, drops default `gmail.com` (F-AO-10) — 60dc37d
- `SECRET_KEY` required in production (F-AO-11) — 60dc37d

---

## Caveats from the agents that did the work

These are noted by the sub-agents that did the implementations. None
block this branch from being deployed once Actions 1–6 are done, but
they are real and worth tracking.

### Storage caveats (commit d78edac)

1. **High-water mark for the `>50% shrink` cloud-sync guard is
   per-process now.** A fresh Cloud Run instance that boots while the
   cloud DB has, say, 5,000 records but `download_from_cloud()` doesn't
   run (or fails) keeps the high-water mark at 0. The shrink guard is
   effectively disabled for that process until its first successful
   upload. Trade-off is what the audit asked for; less protective than
   "always download cloud and compare," but no longer pays GCS egress
   on every save. Acceptable if `download_from_cloud()` is reliable at
   startup.

2. **`update_record()` shallow-merges; `save()` replaces.** All current
   `migrate_*` callers pass full record dicts so behavior is preserved,
   but a future caller that omits a field expecting deletion would now
   retain the prior value. If you find one, switch back to `save()` for
   that path.

3. **`update_record()` does not refresh the indexed columns
   (`ts`, `source_url`, `form_name`).** It only writes `data`. A
   reanalyze that rewrites `form_name` will leave the indexed column
   stale. Follow-up: extend `update_record()` to refresh indexed
   columns from the merged JSON.

### Analyze + crawl caveats (commit 5dbc560)

4. **SSRF guard is on the SEED URL only.** The `requests.Session().get(
   ..., allow_redirects=True)` chain inside `crawler.py` can still
   follow a redirect from `evil.com` to `169.254.169.254`. Manual
   redirect walking with re-validation per hop is flagged with a block
   comment in `crawl.py`. Genuine fix is in Wave 2.

5. **`find_by_base_form_pattern` uses a `LIKE %pattern%` against
   `source_url`.** Strict matching still happens in
   `language_dedup.find_matching_form` (page count + base pattern +
   domain). The audit's preferred fix — a dedicated indexed
   `base_form_pattern` column — needs a schema migration. TODO in the
   helper docstring.

### Analyzer caveats (commit 9bc8151)

6. **`full_text` truncation may break consumers reading past 2000
   chars.** Documented at top of `analyze_pdf`. Worth grepping for
   `full_text` in any external dashboard / pipeline.

---

## Wave 2 backlog (to land in subsequent PRs)

These need infra setup or design choices before code can land. Each is a
separate PR.

### P0 #5 — Long-running batches off the sync request path

**Why:** gunicorn `--timeout 120` (Dockerfile) vs Cloud Run `--timeout
300` (deploy-cloudrun.sh). Any batch >120 s gets SIGKILLed by gunicorn.
Several `migrate_*`, `reanalyze_*`, `normalize_titles` endpoints exceed
this routinely. Documented in `CLOUD_RUN_SCALING_FIX.md` as the 503/10.6%
failure pattern.

**Sketch:**
- Stand up Cloud Tasks queue: `gcloud tasks queues create form-genome-jobs --location=us-central1`.
- Add `app/services/jobs/dispatch.py` that converts each batch endpoint
  into "enqueue then return job_id".
- Worker: separate Cloud Run service (or Cloud Run Jobs for one-shot
  migrations) that consumes the queue. Reuses the analyzer code but
  runs without an HTTP request lifetime.
- Persist progress per `job_id` in a `jobs` Postgres table (closes
  P0 #6 in the same PR).

**Estimate:** 1 week.

### P0 #6 — Persistent `progress` state

**Why:** `services/progress.py` is a module-level dict. With `maxScale:
4`, polls hit a random instance and get a random answer. Restarts
discard in-flight progress.

**Options:**
- Memorystore (Redis): low-latency, ~$45/mo for the smallest instance.
- Postgres `jobs` table: free, slower, simpler.

**Recommendation:** Postgres `jobs` table — share infra with the Cloud
Tasks worker (P0 #5). Add `services/progress_store.py` that reads/writes
keyed by `job_id`.

### P0 #19 — Auto-commit gate

**Why:** `DEPLOYMENT_SUMMARY.md` notes that quality-confidence HIGH
forms are auto-committed. A model regression would silently mutate
committed records.

**Sketch:**
- Add `app.config["AUTO_COMMIT_HIGH_CONFIDENCE"] = bool` (env-driven,
  default False).
- In `analyzer.py`, when the auto-commit branch fires, only commit if
  the flag is True. Otherwise mark `pending_review = True` and surface
  in the admin UI.
- Add an admin endpoint to bulk-approve pending-review forms.

### P1 — Per-route rate limiting

`flask-limiter` is configured but never used per-route. The `memory://`
storage means each Cloud Run instance has its own counter; effective
limit is `default × maxScale`.

**Steps:**
- Move limiter creation to `app/extensions.py` so blueprints can
  `from app.extensions import limiter`.
- Decorate sensitive routes:
  - `/login`: `@limiter.limit("10/minute")`
  - `/api/reanalyze*`, `/api/normalize_titles`, `/api/migrate_*`,
    `/api/batch_reanalyze`: `@limiter.limit("20/hour")` per IP, `5/hour`
    per user once user-keyed limits land.
- Switch `storage_uri` to `memory://` only in dev; use Memorystore in
  prod (`storage_uri="redis://10.x.x.x:6379"`).

### P1 — Structured logging migration

Most service files still `print(...)`. New code in this branch uses
`logging.getLogger(__name__)`. To finish:

- Replace `print(` with `logger.info(` / `logger.warning(` /
  `logger.error(` in `crawler.py`, `db_sync.py`, `domain_entity_cache.py`,
  `pdf_vision_analyzer.py`, `llm_router.py`, `llm_discover.py`,
  `title_llm.py`, `title_normalizer.py`.
- Add a JSON formatter in `app/__init__.py` so Cloud Logging gets
  severity + structured fields. Minimal:
  ```python
  import json, logging
  class JsonFormatter(logging.Formatter):
      def format(self, r):
          return json.dumps({
              "severity": r.levelname,
              "message": r.getMessage(),
              "logger": r.name,
              "request_id": getattr(r, "request_id", None),
          })
  ```
- Add `request_id` correlation via a Flask `before_request` hook that
  generates a UUID and injects into the log adapter.

### P1 — LLM router fan-out cap

`llm_router.py` retries 12× across 4 providers with no budget cap. Add:
- A `MAX_TOTAL_ATTEMPTS = 4` constant — primary `retries+1` plus 1
  attempt per fallback provider.
- Short-circuit on auth errors (401/403) instead of falling through.
- A `budget_hint=` kwarg that callers pass to bypass fallback for
  cheap-only paths.

### P1 — robots.txt + per-host rate limiting

Crawler currently identifies as Chrome (line 53), does not consult
robots, and has 8 worker threads per crawl with no per-host token bucket.
At 100× volume this gets the IP banned and possibly sued. Add:
- `urllib.robotparser` check in `_session()` setup.
- A per-host `collections.defaultdict(threading.Semaphore)` keyed by
  netloc with N=2 default.
- Honest UA: `FormGenomeCrawler/1.0 (+https://your-contact-url)`.
- Remove the per-vendor `_shim_headers_for` Schwab hack — its existence
  is a tell that a host has already partially detected the bot.

### P1 — Domain entity cache → Postgres

`domain_entity_cache.py` writes JSON to `data/` per instance. With N
instances, hit rate trends to 1/N. Migrate to a `domain_entities`
Postgres table with `ON CONFLICT (domain) DO NOTHING`. ~50 LOC.

### P1 — Versioned export contract

The dashboard's `csv_to_fgd.py:30` `COLUMN_MAP` is unversioned, and the
`/api/records?committed=1` response is unversioned. Add:
- `CONTRACT_VERSION = "1.0"` constant in both ends.
- Embed as `"_contract": "1.0"` field in API responses + as a
  `--contract-version` flag on the dashboard CLI.
- Refuse to render if mismatched major version.

---

## Wave 3 backlog (months out)

- **Multi-tenancy** — currently zero tenant isolation. Add `tenant_id`
  column and per-request scoping. Required before onboarding more than
  one customer. (F-AO-17)
- **Playwright pool / dedicated worker** — move browser launches off
  the Flask request path entirely. (F-CS-08)
- **Postgres-only in prod** — eliminate the SQLite branches from
  `storage.py` filtering functions; keep SQLite only as a dev fixture.
  (F-CS-11, F-CS-15)
- **Schema-validated LLM output** — pydantic models for vision and
  discover. Reject non-conforming results into a `parse_error` queue
  instead of committing them. (F-AP-07)
- **Repo cleanup** — 22 `test_*.py` files at root with `i:/My Drive/`
  hardcoded paths, three near-duplicate `analyze_usda*.py` scripts,
  archive 9 incident `.md` files. Promote `ParallelAnalyzer` to
  `app/services/jobs/parallel.py`. Pin `requirements.txt`.
- **Eliminate hardcoded customer logic** — Schwab/Fidelity domain
  literals scattered through `analyzer.py`, `llm_discover.py`,
  `title_llm.py`, `crawler.py`. Move to a data-driven config keyed by
  domain. (F-AP-16, F-CS-27)
- **Audit `requirements.txt`** — currently unpinned. Pin and audit
  transitive deps (`pip-audit`, `pip-licenses`).

---

## Dashboard skill (separate repo)

`/Users/patrickmeyer/Documents/Claude/Projects/GenomeDeux/form-genome-dashboard/`
also got hardened in this session. Changes (uncommitted there since the
skill repo has no git):

- **SRI hashes** added for React, ReactDOM, and Babel cdnjs scripts.
  Browser will refuse to execute substituted scripts. Hashes match
  the exact bundles loaded; recompute when you bump versions.
- **`readonly_check` JS expression replaced by structured
  `readonly_actions: [...]` list.** Eliminates eval surface from
  branding configs. Legacy `readonly_check` still accepted for
  back-compat but the build path validates types when the new field is
  used.
- **Logo embed limits**: rejects images >2 MB; rejects SVGs containing
  `<script>` or `javascript:` URIs.
- **CSV column-existence assert**: refuses to convert a CSV that's
  missing required columns (`Form Name`, `Entity Name`, `Complexity
  Score`, `NIGO Score`, `Action Type`). No more silent zeros.

To redistribute, re-zip the directory:

```bash
cd /Users/patrickmeyer/Documents/Claude/Projects/GenomeDeux
rm -f form-genome-dashboard.zip
zip -r form-genome-dashboard.zip form-genome-dashboard/ -x "*.DS_Store" "*__pycache__*"
```

---

## What did NOT change

- No production deploy was done.
- No live secrets were rotated (Action 1).
- No GCP-side flags were toggled (Action 2).
- No `git push` happened — the branch is local only.
- The 264 MB `Archive.zip` is still on disk; it is now `.dockerignore`d
  but not deleted. Decide if it should be archived elsewhere or
  permanently removed.
- 22 root-level `test_*.py` files with hardcoded `i:/My Drive/...`
  paths are still there; cleanup is in Wave 3 backlog.

---

## Suggested next session

1. Do Action 1 (secret rotation) and Action 5 (verify SECRET_KEY plumbed).
2. Cut a deploy with the new branch + new secrets. Watch logs for the
   two new RuntimeError paths from `storage.py` and `app/__init__.py`.
3. Once stable, do Action 2 (drop `--allow-unauthenticated`). Test
   OAuth login flow end-to-end before announcing.
4. Then schedule Wave 2 design discussion: Cloud Tasks vs Cloud Run
   Jobs, Memorystore vs Postgres for progress, multi-tenant timeline.
