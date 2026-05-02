# Form Genome — Architectural Decisions

Decisions taken on **2026-05-02** following the post-audit review.
These bind the Wave 1.5 / Wave 2 / Wave 3 plans. Update this doc with a
new dated section if any decision is reversed; don't quietly edit.

---

## Tier A — frame everything

1. **Scaling target: 100×** today's load.
   Design for 100×, ship and run at 10× initially. We don't pay 1000×
   complexity costs (read replicas, multi-region) until we have evidence
   we need them.

2. **Tenancy: single-tenant** for the foreseeable future (≥ 6 months).
   No `tenant_id` work in Wave 2. If a 2nd customer is signed, this
   decision flips and we revisit before code lands.

3. **Storage: Postgres-only in prod, SQLite for dev fixtures only.**
   ~30% of `storage.py`'s dual-mode branches gets deleted in Wave 2.
   SQLite's only role is local dev test fixtures.

4. **Cloud Run auth: Identity-Aware Proxy (IAP).**
   Drop `--allow-unauthenticated`. IAP fronts the service; Flask trusts
   the `X-Goog-Authenticated-User-*` headers. The application-layer
   `before_request` gate added in commit `60dc37d` becomes a defense-in-
   depth fallback. Workspace group membership controls access.

## Tier B — required by Wave 2 PRs

5. **Off-request work: Cloud Tasks for per-record fan-out, Cloud Run
   Jobs for bounded migrations.** Same analyzer code, different
   invocations.

6. **Persistent progress: Postgres `jobs` table.** No Memorystore. ~1ms
   reads at our poll rate is plenty; saves ~$540/yr and one moving part.

7. **Auto-commit: gated behind a config flag, default off.** Ship the
   flag immediately. Default behavior becomes "commit only with explicit
   review" until the review-queue UI lands in Wave 3. The flag is the
   one-line emergency switch the next time a model regresses.

8. **LLM budget: soft alerting.** Slack/email thresholds at 50% / 80% /
   100% of monthly budget. No hard ceiling — runaway is more obvious in
   alerts than in mysterious refusals while we're still finding load
   shape.

## Tier C — operational

9. **Crawler politeness: Wave 1.5.** Land `robots.txt` honoring,
   per-host token bucket, honest UA in the next ~3 days, BEFORE any
   Wave 2 work. The current implementation will get IP-banned at any
   real scale.

10. **Chrome extension: build a real one.** 2-week parallel track.
    Doesn't block Wave 2. Spec it in a separate doc.

11. **`Archive.zip` (264 MB): move out of repo** within a week. Ideally
    to `gs://formgenome-archive/<dated>/` or a separate Drive location.

12. **Source control: migrate to a private GitHub repo.** Before any
    Wave 2 work lands so PR review is available. The current
    Drive-synced `.git` with no remote is fine for the post-audit
    branch but inadequate for ongoing collaborative work.

---

## Sequencing — what these decisions imply

### Now (Day 0–3)

In order, because each unblocks the next:

- **Action 1** (HANDOFF): rotate the five leaked secrets.
- **Action 2 + 4 (decision):** drop `--allow-unauthenticated`, set up IAP
  with the Workspace group containing your authorized users.
- **Action 3** (HANDOFF): set `ALLOWED_EMAIL_DOMAINS`, `ALLOWED_EMAILS`,
  `ADMIN_EMAILS` env vars (defense-in-depth behind IAP).
- **Action 5 + 6 (HANDOFF):** verify the new `RuntimeError` paths
  surface correctly and confirm `full_text` consumers still work.
- **GitHub migration**: create private repo, push the
  `post-audit-hardening` branch, open it as PR #1 for review.
- **Move `Archive.zip`** out of the working tree.

### Wave 1.5 (Days 3–6) — crawler politeness, one PR

- `robots.txt` honoring via `urllib.robotparser` (cached per-domain,
  TTL 24 h)
- Per-host token bucket (`collections.defaultdict(threading.Semaphore)`,
  N=2 default, env-tunable)
- Honest UA: `FormGenomeCrawler/1.0 (+https://your-contact-url)`
- Remove the `_shim_headers_for` per-vendor Schwab hack — its existence
  is a tell that politeness was already inadequate.
- Manual redirect-walking with re-validation per hop (closes the SSRF
  redirect-hop gap noted in HANDOFF).

Estimated 1 PR, ~300 LOC, 1 day of work.

### Wave 2 (Weeks 1–4) — six PRs, ordered

Each PR is intended to merge independently and should be sized so a
reviewer can hold the whole thing in their head.

| # | PR | Closes | Size |
|---|---|---|---|
| W2-1 | **Postgres-only storage refactor.** Delete SQLite branches from `list_committed`, `count_filtered`, `list_filtered`, `delete_uncommitted`, `delete_empty`, `count_empty`. Add `jobs` table schema migration. Document SQLite as dev-fixture-only. | F-CS-11, F-CS-15, partial decision-3 | ~600 LOC, -1000 LOC, 3 days |
| W2-2 | **Cloud Run Jobs for `migrate_*`.** Each `migrate_*` endpoint becomes a thin wrapper that triggers a Cloud Run Job with the same code path. Endpoints return `job_id`; clients poll. | P0 #5 (migrations), P0 #6 (uses jobs table) | ~400 LOC, 3 days |
| W2-3 | **Cloud Tasks for `reanalyze_*` + per-record fan-out.** Replace the in-process daemon thread in `batch_reanalyze.py` with Cloud Tasks. Same `jobs` table for progress. | P0 #5 (reanalyze), full P0 #6 | ~500 LOC, 4 days |
| W2-4 | **Auto-commit flag + review queue stub.** `app.config["AUTO_COMMIT_HIGH_CONFIDENCE"]` env-driven, default `False`. Forms that *would* have auto-committed get `pending_review = True`. Admin endpoint to bulk-approve. (Full review UI deferred to Wave 3.) | P0 #19 (decision 7) | ~150 LOC, 1 day |
| W2-5 | **Per-route rate limits + limiter refactor.** Move limiter to `app/extensions.py`. Decorate `/login` (10/min), all `/api/migrate_*`, `/api/reanalyze*`, `/api/normalize_titles`, `/api/batch_reanalyze` (20/hour). Switch storage to Memorystore *or* Postgres `rate_limits` table — given decision 6 (no Memorystore), pick **Postgres**. | F-AO-08 | ~200 LOC, 1 day |
| W2-6 | **LLM router fan-out cap + soft budget alerts.** Cap to `primary_retries=2 + 1 fallback per provider`. Short-circuit on auth errors. Daily token-spend tally → Sheets webhook → alerts at 50/80/100%. | F-AP-06, decision 8 | ~250 LOC, 2 days |

Total: ~14 days of focused work, 6 PRs, all reviewable independently.

### Wave 2.5 (Week 4) — observability + cleanup

- **Structured logging migration**: 8 service files still use `print()`.
  JSON formatter + `request_id` correlation.
- **Domain entity cache → Postgres**: ~50 LOC, eliminates per-instance
  cache miss tax.
- **Versioned export contract**: `_contract: "1.0"` field in
  `/api/records` responses, dashboard refuses mismatches.

### Wave 3 (Months 2–3, parallel tracks)

- **Chrome extension** — 2-week parallel track. Independent of backend
  work; decoupling means an extension dev (or you) can work on it while
  Wave 2 PRs land.
- **Repo cleanup** — 22 root-level `test_*.py` into `tests/`, archive
  9 incident `.md` files, promote `ParallelAnalyzer` to
  `app/services/jobs/parallel.py`, pin `requirements.txt`.
- **Eliminate hardcoded customer logic** — Schwab/Fidelity literals to
  data-driven config.
- **Schema-validated LLM output** — pydantic models, parse-error queue.
- **Review queue UI** — graduates the auto-commit flag from
  emergency-switch to standard workflow.

---

## What is NOT happening (per these decisions)

- **No multi-tenancy.** Don't add `tenant_id` columns, don't write
  per-tenant rate limiters, don't worry about isolation. Decision 2.
- **No Memorystore.** All shared state goes in Postgres. Decision 6.
- **No hard LLM budget.** Don't wire refusal-on-budget-exhausted in
  Wave 2. Decision 8.
- **No SQLite in production.** Don't keep dual-mode "for safety" — the
  drift between Postgres and SQLite paths IS the safety hazard.
  Decision 3.

---

## When to revisit

- Sign a 2nd customer → **decision 2 flips**, schedule a Wave 2.5 for
  multi-tenancy before any new feature work.
- LLM monthly bill exceeds $X (you pick X) for two consecutive months →
  **decision 8 flips**, hard ceiling becomes mandatory.
- Crawler IP-banned by 2+ major hosts despite Wave 1.5 → revisit
  honest-vs-stealth UA strategy.
- Cloud Run cold-start latency hurts UX → revisit `min-instances` and
  by extension cost shape.
