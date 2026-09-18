# Figure Origination Scraper

Daily scraper for Figure Technologies' loan-origination activity on the
Provenance Blockchain — per-originator volume/frequency, plus the
FIGR_HELOC funding-channel breakdown (securitization / participation /
warehoused / pooled / unstructured).

## How it works

Two independent, fully public Provenance data sources, no auth required:

- `service-explorer.provenance.io` — transaction search (`txs/recent`),
  used to find new loan-origination transactions.
- `api.provenance.io` — the raw chain REST API, used for scope detail,
  net-asset-value (loan dollar amount) lookups, and registry role data.

**Frequency & originator ID:** every loan origination writes a
`provenance.registry.v1.EventRoleGranted` event with `role="ORIGINATOR"`
and the loan's scope ID, scoped to Figure's `com.figure.origination.loan`
asset class. That address *is* the partner — no name resolution needed,
matches Figure's own on-chain signer.

**Volume:** loan amounts aren't set at origination. Figure posts a
net-asset-value (NAV) event to the loan's scope once it actually funds,
which can lag origination by several days. So volume is refreshed as a
separate, repeated pass over recently-discovered, not-yet-funded loans.

**Interest rate & servicing terms:** the `ledger` module records a
`MsgCreateLedgerRequest` in the **same transaction** as the loan scope
itself (confirmed by inspecting real transaction event logs), so unlike
NAV there's no funding-lag wait — rate is available immediately at
discovery time. `GET /provenance/ledger/v1/config/{asset_class_id}/{nft_id}`
returns `interest_rate` scaled such that `9,300,000 = 9.3%` (confirmed
against real data — matches debloc's own published HELOC/Consumer
median rate range almost exactly), plus bonus fields we store alongside
it: `maturity_date`, `next_pmt_date`/`next_pmt_amt`, `payment_frequency`,
day-count convention, and accrual method.

**Derived originator-level metrics:** everything debloc shows in its
"Partner growth" / "Originator concentration" / "Rate distribution"
panels — mix by loan class, share of volume, ramp curves (cumulative
volume by months-since-first-loan), new-partner-onboarding counts, and
a Herfindahl-Hirschman concentration index — is fully computable from
the raw `loans` table above, no extra API calls needed. See
`compute_originator_metrics.py`. These get written to dated CSVs each
run (`originator_mix_*.csv`, `originator_ramp_curves.csv`,
`new_partners_monthly.csv`, `concentration_monthly.csv`) and committed
alongside the raw data, so a snapshot of "what the metrics looked like
as of this run" survives in git history even though the numbers
themselves are always fully recomputable from the raw loan data if you
ever want to revise the definitions.

**Funding channels:** the `FIGR_HELOC` rollup scope carries a
periodically-updated JSON summary (securitization/participation/
warehoused/pools/unstructured totals) — pulled once daily. We also
derive a **securitized vs. whole-loan** split from these five raw
numbers: `securitized_usd = total_securitization`, and
`whole_loan_usd = total_participation + total_warehoused + total_pools
+ total_unstructured` (i.e. everything *not* securitized — sold/held as
whole loans, participated out, warehoused, or unallocated). **Caveat:**
Figure hasn't published an official definition of these five categories
anywhere we found, so this split is our best industry-standard-terms
read, not confirmed ground truth — see the docstring on
`compute_securitization_split()` in `snapshot_funding_channels.py` for
the full reasoning, and revise it there if you get an authoritative
breakdown later. The five raw values are stored untouched in the DB
either way, so nothing is lost if the mapping needs correcting.

Also worth flagging: we haven't confirmed the exact decimal scale of
these five numbers (raw values are large integers — e.g.
`total_securitization: 9845429979512`) against a known dollar figure,
unlike individual loan NAV amounts which we did confirm are plain USD
(no scaling). Treat the funding-channel numbers, and the derived split,
as internally consistent with each other (so the *share*/*ratio* is
reliable) even though the absolute dollar magnitude hasn't been
independently verified.

See the module docstrings in each file for more detail; this was reverse
engineered by hand against the live chain, not from any official docs.

## Files

| File | Purpose |
|---|---|
| `config.py` | API bases, known addresses/specs, tunables |
| `provenance_client.py` | HTTP client with retry/backoff |
| `db.py` | SQLite schema + data access |
| `scrape_originations.py` | Discovery job — finds new loans, records originator/servicer/timing |
| `refresh_rates.py` | Polls the ledger module for interest rate/servicing terms (immediate, no lag) |
| `refresh_volumes.py` | Polls NAV for recent unfunded loans |
| `snapshot_funding_channels.py` | Daily FIGR_HELOC aggregate snapshot |
| `enrich_loan_classes.py` | Optional: best-effort HELOC/Consumer/Auto classification |
| `compute_originator_metrics.py` | Derived analytics: mix, ramp curves, new-partner counts, concentration |
| `run_daily.py` | Orchestrator — run this one |
| `docs/index.html` | Live dashboard site (GitHub Pages) — see "Dashboard site" below |
| `test_parsing.py`, `test_db_and_snapshot.py`, `test_rate_limiting.py`, `test_discovery_resilience.py` | Offline tests using real captured chain data |

## Running locally

```bash
pip install -r requirements.txt

# Scrape yesterday (UTC), refresh volumes, snapshot funding channels, export CSVs
python run_daily.py

# Backfill an explicit range instead
python run_daily.py --backfill 2026-08-01 2026-08-31

# Skip the slower per-loan classification step
python run_daily.py --no-enrich
```

Output:
- `data/originations.db` — SQLite database (all raw data lives here)
- `exports/loans.csv` — every loan, one row each, including rate/servicing terms
- `exports/originator_daily_summary.csv` — loan count, funded volume, and median rate, grouped by originator/day
- `exports/originator_mix_all_time.csv`, `exports/originator_mix_trailing_30d.csv` — volume/count by loan class per originator
- `exports/originator_first_loan.csv` — first origination date per originator
- `exports/originator_ramp_curves.csv` — cumulative funded volume by months-since-first-loan, top 10 originators
- `exports/new_partners_monthly.csv` — count of originators onboarding each calendar month
- `exports/concentration_monthly.csv` — top-5 share + Herfindahl-Hirschman concentration index, per month
- `exports/funding_channels.csv` — daily funding-channel snapshots,
  including the derived `securitized_usd` / `whole_loan_usd` /
  `securitized_share` columns

Run the tests any time with `python test_parsing.py` and
`python test_db_and_snapshot.py` — no network access needed, they replay
real chain data captured during development.

## Dashboard site

`docs/index.html` is a self-contained dashboard (no build step — plain
HTML/CSS/JS, Chart.js + PapaParse loaded from CDN) that fetches the
CSV exports directly from your repo's raw GitHub URLs and renders them
client-side. It updates automatically every time the daily workflow
commits new CSVs — no rebuild or redeploy needed, since it fetches
fresh data on every page load.

**One-time setup to publish it via GitHub Pages:**

1. In your repo, go to **Settings → Pages**.
2. Under "Build and deployment", set **Source** to "Deploy from a
   branch", **Branch** to `main`, folder to `/docs`. Save.
3. GitHub will give you a URL like
   `https://<your-username>.github.io/<your-repo>/` — it can take a
   minute or two to go live the first time.
4. **Important:** open `docs/index.html` and check the `REPO` constant
   near the top of the `<script>` block — it must exactly match your
   `username/repo-name`. If you forked/renamed this project, update
   that line before publishing.

The page pulls data straight from
`https://raw.githubusercontent.com/<REPO>/main/exports/*.csv`, which is
confirmed to send permissive CORS headers (`access-control-allow-origin: *`),
so the cross-origin fetch from your `github.io` domain works without
any server-side proxy.

**Design notes:** styled after Keefe, Bruyette & Woods' brand navy
(`#003579`, taken directly from their site's theme-color) — as an
institutional research-monitor layout (hairline-divided sections, dense
right-aligned tables) rather than a consumer dashboard, since that's
closer to how a real IB research terminal actually looks. The header
typeface (Georgia) is a professional judgment call, not a verified
match to KBW's actual webfont, since their CSS wasn't accessible to
extract it from. Body/data text uses Arial as requested.

Every chart/table degrades gracefully to a plain-language empty-state
message rather than erroring when a given CSV doesn't have data yet
(e.g. `concentration_monthly.csv` needs a few months of history before
it's meaningful) — verified with a headless-DOM test harness against
both real scraped data and a fully-empty first-run scenario.

## Running it daily on GitHub (no server needed)

This repo already includes `.github/workflows/daily_scrape.yml`, which
runs the scraper once a day and commits the updated database + CSVs
back into the repo automatically.

**Setup steps:**

1. **Create a new GitHub repo** and push this folder to it:
   ```bash
   cd debloc-scraper
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/<your-username>/<your-repo>.git
   git push -u origin main
   ```

2. **That's it for basic setup** — no secrets or API keys needed, since
   both Provenance APIs are public. The workflow already has
   `permissions: contents: write`, which lets it commit results back.

3. **It'll run automatically at midnight Eastern time, every day**,
   correctly handling the EST/EDT switch. GitHub Actions cron only
   understands fixed UTC times, so the workflow schedules *both*
   possible UTC times (05:00 UTC for EST, 04:00 UTC for EDT) and a
   guard job (`check-time`) checks the real America/New_York clock at
   run time and only lets the matching one actually scrape — no manual
   edits needed across DST changes. If you want a different time
   entirely, edit both `cron:` lines in
   `.github/workflows/daily_scrape.yml` and the `HOUR_ET` comparison in
   the `check-time` job accordingly.

4. **To trigger a run manually** (e.g. to test it, or to backfill):
   go to your repo's **Actions** tab → **Daily Figure origination
   scrape** → **Run workflow**. This runs `run_daily.py` with no
   arguments (i.e. "yesterday"), same as the schedule does.

5. **To backfill historical data**, easiest is to run it locally once
   with `--backfill`, then `git push` the resulting `data/originations.db`
   — the daily workflow will pick up from there.

6. **Watching it run**: each run's logs are visible under the Actions
   tab. If a run pushes an updated `data/originations.db`, you'll see a
   new commit on `main` from `github-actions[bot]`.

### A note on scale — and a real rate limit we hit

Figure originates roughly 800-900 loans/day platform-wide per their own
published stats. The discovery job makes one API call per transaction
(not per loan — most transactions bundle 1 loan, occasionally 2+), so
that's a few hundred to ~1,000 calls/day, all against
`service-explorer.provenance.io`.

**`api.provenance.io` (used for enrichment, rate refresh, and volume
refresh) turned out to have a real, undocumented rate limit that's much
stricter than `service-explorer.provenance.io`'s.** In an actual first
run, 1,204 sequential calls to service-explorer at 0.25s pacing produced
zero 429s — but the moment enrichment and rate-refresh started hitting
api.provenance.io back-to-back for hundreds of loans, nearly every
single request came back `429`.

Two things in the code now handle this:

1. **`provenance_client.py` paces api.provenance.io and
   service-explorer.provenance.io separately** (`config.CHAIN_API_REQUEST_DELAY_SECONDS`
   vs `config.SERVICE_EXPLORER_REQUEST_DELAY_SECONDS`), and adapts —
   a 429 permanently raises that host's delay for every *subsequent*
   request (not just retries of the same resource, which was the actual
   bug in the first version: backing off on one loan while immediately
   hammering the next, different loan at full speed). It also honors a
   `Retry-After` header exactly when the server sends one, and eases the
   delay back down after a long clean streak. See
   `test_rate_limiting.py` for this behavior verified against a mocked
   session.

2. **`run_daily.py` caps how many loans get enriched/rate-checked/
   volume-checked per run** (`--enrich-limit`, `--rate-limit`,
   `--volume-limit`, all default 150) rather than trying to clear an
   entire backlog in one go. This is safe because all three are
   idempotent "whatever's still pending" queries — anything left over
   just gets picked up on the next day's run. A large first-time
   backlog (like an initial 1,200-loan day) will take a few runs to
   fully enrich/rate/price, not one.

If you're backfilling a lot of history at once and want it to go
faster, you can raise these limits, but expect api.provenance.io to
throttle you if you push too hard — the adaptive delay will handle it
correctly now, just possibly slowly. GitHub Actions jobs can run up to
6 hours, so there's headroom either way.

### A real crash we hit, and how the code is now resilient to it

In production, a single transaction (out of ~1,200 that day) hit a
read timeout against `service-explorer.provenance.io`, exhausted all 5
retries, and raised. Two compounding problems, both fixed now:

1. **The whole run crashed.** `scrape_originations.py`'s per-transaction
   loop had no try/except around the network call — unlike
   `enrich_loan_classes.py` / `refresh_rates.py` / `refresh_volumes.py`,
   which already skip-and-continue on a single item's failure. Fixed:
   discovery now does the same — a transaction that fails after retries
   is logged and skipped, not fatal. Since `upsert_loan` is idempotent,
   nothing is lost by skipping; it'll just get picked up again if that
   date range is ever re-scraped.

2. **Worse: all progress from that entire day would have been
   discarded, not just the one bad transaction.** The whole multi-page
   discovery loop ran inside a single SQLite transaction that only
   committed at the very end — so a crash on transaction #400 (out of,
   say, 1,200) meant transactions #1-399's work was rolled back too when
   the connection closed without committing. Fixed: `scrape_originations.py`
   now commits after every page of results, so a later failure can't
   erase earlier, already-good progress. `run_daily.py` also now runs
   each phase (discovery, enrichment, rate refresh, volume refresh,
   funding snapshot, CSV export) independently — a failure in one
   doesn't prevent the others from running, and the GitHub Actions
   workflow's commit step runs even if `run_daily.py` reports a failure,
   so partial progress still gets saved rather than the whole day's work
   vanishing. The job still shows as failed in the Actions tab when this
   happens, so it stays visible rather than silently swallowed.

See `test_discovery_resilience.py` for this verified directly: it
simulates a transaction that fails after retries, sandwiched between
two that succeed, and confirms both surrounding transactions still land
in the database.
