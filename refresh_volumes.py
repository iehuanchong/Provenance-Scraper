"""
Volume (dollar amount) refresh job.

Loan amounts aren't known at origination time -- Figure posts a
net-asset-value (NAV) event to a loan's scope once it actually funds,
which can lag origination by several days (median time-to-fund varies
by product: roughly same-day for Auto, days for HELOC per debloc's own
published stats). So this runs as a separate, repeatable pass over
recently-discovered loans that don't have an amount yet, rather than
being part of the one-shot discovery job.

Safe to run daily alongside discovery -- it just re-checks whatever
still has amount_usd IS NULL within the recent window.
"""

import logging
import datetime as dt

import config
import db
import provenance_client as pc

logger = logging.getLogger("refresh_volumes")

_COMMIT_EVERY = 25  # flush progress periodically so a mid-phase crash/timeout/kill only loses this many items' work, not the whole run


def refresh_recent_volumes(window_days: int = config.NAV_REFRESH_WINDOW_DAYS,
                            limit: int = 500) -> int:
    """
    Checks NAV for loans discovered in the last `window_days` days that
    don't yet have a dollar amount, up to `limit` per run (api.provenance.io
    has a real, undocumented rate limit -- see provenance_client.py -- so
    an unbounded backlog here can turn one run into an hours-long one；
    capping it just means the remainder gets picked up on the next run,
    since this only ever touches loans still missing amount_usd).
    Returns the number of loans newly priced this run.
    """
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=window_days)).isoformat()
    updated = 0

    with db.connect() as conn:
        pending = db.get_unfunded_recent_scopes(conn, since, limit=limit)
        logger.info("Checking NAV for %d unfunded loans (window=%dd, limit=%d)",
                    len(pending), window_days, limit)

        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        for i, row in enumerate(pending, start=1):
            scope_addr = row["scope_addr"]
            try:
                navs = pc.get_net_asset_values(scope_addr)
            except RuntimeError:
                logger.warning("Failed to fetch NAV for %s after retries, will retry next run", scope_addr)
                continue

            if not navs:
                db.mark_volume_checked(conn, scope_addr, now_iso)
                continue

            # Take the most recent NAV entry (highest block height) --
            # in practice there's usually exactly one.
            latest = max(navs, key=lambda n: int(n.get("updated_block_height", 0)))
            price = latest.get("price", {})
            if price.get("denom") != "usd":
                # Unexpected denom -- skip rather than misreport a value.
                logger.warning("Non-USD NAV denom for %s: %s", scope_addr, price)
                db.mark_volume_checked(conn, scope_addr, now_iso)
                continue

            amount_usd = float(price.get("amount", 0))
            db.set_loan_amount(
                conn, scope_addr,
                amount_usd=amount_usd,
                nav_updated_block=int(latest.get("updated_block_height", 0)),
                checked_at=now_iso,
            )
            updated += 1

            if i % _COMMIT_EVERY == 0:
                conn.commit()
                logger.info("Volume refresh progress: %d/%d checked, %d newly priced (committed)",
                            i, len(pending), updated)

    logger.info("Volume refresh complete: %d loans newly priced", updated)
    return updated
