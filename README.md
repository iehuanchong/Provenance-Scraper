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
| `test_parsing.py`, `test_db_and_snapshot.py` | Offline tests using real captured chain data |

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

### A note on scale

Figure originates roughly 800-900 loans/day platform-wide per their own
published stats. The discovery job makes one API call per transaction
(not per loan — most transactions bundle 1 loan, occasionally 2+), so
that's a few hundred to ~1,000 calls/day. Rate refresh and volume
refresh each add roughly one call per loan on top of that (rate
resolves immediately so it's a one-time cost per loan; volume can take
several retries across days until a loan funds). All told, a few
thousand calls/day at steady state — still well within GitHub Actions'
free-tier minute allowance, and gentle on Provenance's public
infrastructure at the default `REQUEST_DELAY_SECONDS` pacing in
`config.py`.

If you want to be extra conservative (no documented rate limit was
found for either API), raise `REQUEST_DELAY_SECONDS` — the tradeoff is
just a longer-running workflow, which GitHub Actions handles fine up to
6 hours per job.
