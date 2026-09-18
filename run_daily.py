"""
Daily entrypoint. Run this from cron / GitHub Actions / wherever.

    python run_daily.py                # scrape yesterday (UTC), refresh volumes, snapshot
    python run_daily.py --backfill 2026-08-01 2026-08-31   # backfill a date range
    python run_daily.py --no-enrich     # skip the slower per-loan class enrichment

What it does, in order:
  1. discover_originations() for the target date window
     -> new rows in `loans` (scope, originator, servicer, timing)
  2. enrich_pending()  [optional, slower]
     -> fills in contract_spec_name / loan_class for new loans
  3. refresh_pending_rates()
     -> polls the ledger module for interest rate / servicing terms
        (available immediately at origination, no funding lag)
  4. refresh_recent_volumes()
     -> polls NAV for any recent loan still missing a dollar amount
        (this DOES lag origination by days, since it waits on funding)
  5. snapshot_funding_channels()
     -> records today's FIGR_HELOC aggregate breakdown
  6. export_csv_summaries()
     -> writes exports/*.csv, including derived originator-level metrics
        (mix, ramp curves, new-partner counts, concentration) so a dated
        snapshot of each survives in git history even though they're
        always fully recomputable from the raw `loans` table.
"""

import argparse
import csv
import logging
import statistics
import sys
import datetime as dt

import config
import db
from scrape_originations import discover_originations
from refresh_volumes import refresh_recent_volumes
from refresh_rates import refresh_pending_rates
from snapshot_funding_channels import snapshot_funding_channels, compute_securitization_split
from enrich_loan_classes import enrich_pending, reclassify_existing
import compute_originator_metrics as metrics

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("run_daily")


LOAN_EXPORT_COLUMNS = [
    "scope_addr", "tx_hash", "block_time", "originator_address",
    "servicer_address", "contract_spec_name", "loan_class",
    "amount_usd", "interest_rate_pct", "ledger_class_id",
    "status_type_id", "maturity_date", "next_pmt_date", "next_pmt_amt",
    "payment_frequency", "interest_day_count_convention",
    "interest_accrual_method", "discovered_at",
]


def _write_csv(path: str, rows: list[dict]):
    with open(path, "w", newline="") as f:
        if not rows:
            return
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def export_csv_summaries():
    import os
    os.makedirs(config.EXPORTS_DIR, exist_ok=True)

    with db.connect() as conn:
        # --- raw loans, one row each ---------------------------------
        loans = conn.execute(
            f"SELECT {', '.join(LOAN_EXPORT_COLUMNS)} FROM loans ORDER BY block_time DESC"
        ).fetchall()
        with open(f"{config.EXPORTS_DIR}/loans.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(LOAN_EXPORT_COLUMNS)
            for row in loans:
                writer.writerow(tuple(row))

        # --- funding channels + derived securitized/whole-loan split -
        snapshots = conn.execute(
            "SELECT * FROM funding_channel_snapshots ORDER BY snapshot_date DESC"
        ).fetchall()
        with open(f"{config.EXPORTS_DIR}/funding_channels.csv", "w", newline="") as f:
            if snapshots:
                writer = csv.writer(f)
                base_cols = list(snapshots[0].keys())
                derived_cols = ["securitized_usd", "whole_loan_usd", "securitized_share"]
                writer.writerow(base_cols + derived_cols)
                for row in snapshots:
                    split = compute_securitization_split(dict(row))
                    writer.writerow(
                        tuple(row) + (
                            split["securitized_usd"],
                            split["whole_loan_usd"],
                            split["securitized_share"],
                        )
                    )

        # --- daily loan count + funded volume + median rate, per originator
        raw = conn.execute(
            """
            SELECT originator_address, date(block_time) AS origination_date,
                   amount_usd, interest_rate_pct
            FROM loans
            """
        ).fetchall()
        grouped: dict[tuple, dict] = {}
        for row in raw:
            key = (row["originator_address"], row["origination_date"])
            bucket = grouped.setdefault(key, {"count": 0, "volume": 0.0, "rates": []})
            bucket["count"] += 1
            if row["amount_usd"] is not None:
                bucket["volume"] += row["amount_usd"]
            if row["interest_rate_pct"] is not None:
                bucket["rates"].append(row["interest_rate_pct"])

        by_originator_rows = []
        for (originator, origination_date), bucket in sorted(
            grouped.items(), key=lambda kv: (kv[0][1], -kv[1]["volume"]), reverse=False
        ):
            median_rate = statistics.median(bucket["rates"]) if bucket["rates"] else None
            by_originator_rows.append({
                "originator_address": originator,
                "origination_date": origination_date,
                "loan_count": bucket["count"],
                "funded_volume_usd": bucket["volume"],
                "median_interest_rate_pct": median_rate,
            })
        _write_csv(f"{config.EXPORTS_DIR}/originator_daily_summary.csv", by_originator_rows)

        # --- derived originator-level metrics (mix, ramp curves, etc.) -
        derived = metrics.compute_all(conn)
        for name, rows in derived.items():
            _write_csv(f"{config.EXPORTS_DIR}/{name}.csv", rows)

    logger.info("CSV exports written to %s", config.EXPORTS_DIR)


def main():
    parser = argparse.ArgumentParser(description="Daily Figure origination scraper")
    parser.add_argument(
        "--backfill", nargs=2, metavar=("FROM_DATE", "TO_DATE"),
        help="Backfill an explicit date range (YYYY-MM-DD YYYY-MM-DD) instead of just yesterday",
    )
    parser.add_argument(
        "--no-enrich", action="store_true",
        help="Skip the per-loan contract-class enrichment step (faster, cheaper)",
    )
    parser.add_argument(
        "--enrich-limit", type=int, default=150,
        help="Max number of loans to enrich per run (default: 150 -- conservative given api.provenance.io's real rate limit, see provenance_client.py)",
    )
    parser.add_argument(
        "--rate-limit", type=int, default=150,
        help="Max number of loans to check ledger/rate data for per run (default: 150)",
    )
    parser.add_argument(
        "--volume-limit", type=int, default=150,
        help="Max number of loans to check NAV/volume for per run (default: 150)",
    )
    args = parser.parse_args()

    db.init_db()

    if args.backfill:
        from_date, to_date = args.backfill
    else:
        yesterday = dt.date.today() - dt.timedelta(days=1)
        from_date = to_date = yesterday.isoformat()

    # Each phase runs independently: a bug or a stretch of bad luck in
    # one (an unhandled exception, not just the per-item retries each
    # phase already does internally) shouldn't prevent the others from
    # running or prevent export_csv_summaries() from committing whatever
    # progress was made. Individual API-call failures within a phase are
    # already handled by provenance_client's retries and by the
    # per-item try/except in scrape_originations/enrich_loan_classes/
    # refresh_rates/refresh_volumes -- this is a second, coarser safety
    # net for anything those don't catch.
    failures = []

    def run_phase(name, fn):
        logger.info("=== %s ===", name)
        try:
            fn()
        except Exception:
            logger.exception("Phase '%s' failed -- continuing with the rest of the run", name)
            failures.append(name)

    run_phase("Discovery", lambda: discover_originations(from_date, to_date))

    if not args.no_enrich:
        run_phase("Enrichment", lambda: enrich_pending(limit=args.enrich_limit))
        run_phase("Reclassification", reclassify_existing)

    run_phase("Rate refresh", lambda: refresh_pending_rates(limit=args.rate_limit))
    run_phase("Volume refresh", lambda: refresh_recent_volumes(limit=args.volume_limit))
    run_phase("Funding-channel snapshot", snapshot_funding_channels)
    run_phase("CSV export", export_csv_summaries)

    if failures:
        logger.error("Daily run finished with failures in: %s", ", ".join(failures))
        sys.exit(1)
    logger.info("Daily run complete.")


if __name__ == "__main__":
    main()
