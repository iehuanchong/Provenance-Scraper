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


def refresh_recent_volumes(window_days: int = config.NAV_REFRESH_WINDOW_DAYS) -> int:
    """
    Checks NAV for every loan discovered in the last `window_days` days
    that doesn't yet have a dollar amount. Updates any that have funded.
    Returns the number of loans newly priced this run.
    """
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=window_days)).isoformat()
    updated = 0

    with db.connect() as conn:
        pending = db.get_unfunded_recent_scopes(conn, since)
        logger.info("Checking NAV for %d unfunded loans (window=%dd)",
                    len(pending), window_days)

        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        for row in pending:
            scope_addr = row["scope_addr"]
            navs = pc.get_net_asset_values(scope_addr)
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

    logger.info("Volume refresh complete: %d loans newly priced", updated)
    return updated
