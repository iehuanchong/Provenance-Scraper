"""
Interest rate (and other ledger-servicing data) refresh job.

Unlike NAV/volume, the `ledger` module record for a loan is created in
the *same transaction* as the loan scope itself (both
MsgWriteScopeRequest and MsgCreateLedgerRequest fire together at
origination -- confirmed by inspecting real transaction event logs).
So this doesn't need the "wait for funding" window that refresh_volumes
does; any loan missing rate data is fair game to check, regardless of
how old it is.

Interest rate encoding: the raw `interest_rate` field is scaled such
that 100,000,000 = 100%, e.g. 9,300,000 = 9.3%. We store the
already-converted percent (interest_rate_pct) so nothing downstream
needs to remember the scale factor.
"""

import logging
import datetime as dt

import db
import provenance_client as pc

logger = logging.getLogger("refresh_rates")

_RATE_SCALE = 1_000_000  # raw interest_rate / _RATE_SCALE = percent


_COMMIT_EVERY = 25  # flush progress periodically so a mid-phase crash/timeout/kill only loses this many items' work, not the whole run


def refresh_pending_rates(limit: int = 1000) -> int:
    """
    Fetches ledger data for up to `limit` loans that don't have rate info
    yet. Returns the number of loans updated.
    """
    updated = 0
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    with db.connect() as conn:
        pending = db.get_scopes_missing_rate(conn, limit=limit)
        logger.info("Checking ledger/rate data for %d loans", len(pending))

        for i, row in enumerate(pending, start=1):
            scope_addr = row["scope_addr"]
            try:
                ledger = pc.get_ledger(scope_addr)
            except RuntimeError:
                logger.warning("Failed to fetch ledger for %s, will retry next run", scope_addr)
                continue

            if ledger is None:
                # No ledger found (unexpected for a loan-class scope, but
                # don't crash the run over one oddity) -- mark checked so
                # we don't hammer the same scope every run; it'll still
                # show up again if you lower the bar by clearing the
                # rate_checked_at column, if that ever turns out useful.
                db.mark_rate_checked(conn, scope_addr, now_iso)
                continue

            raw_rate = ledger.get("interest_rate")
            interest_rate_pct = (raw_rate / _RATE_SCALE) if raw_rate is not None else None

            next_pmt_amt = ledger.get("next_pmt_amt")
            next_pmt_amt = float(next_pmt_amt) if next_pmt_amt not in (None, "") else None

            db.set_loan_ledger_info(
                conn, scope_addr,
                interest_rate_pct=interest_rate_pct,
                ledger_class_id=ledger.get("ledger_class_id"),
                status_type_id=ledger.get("status_type_id"),
                maturity_date=ledger.get("maturity_date"),
                next_pmt_date=ledger.get("next_pmt_date"),
                next_pmt_amt=next_pmt_amt,
                payment_frequency=ledger.get("payment_frequency"),
                interest_day_count_convention=ledger.get("interest_day_count_convention"),
                interest_accrual_method=ledger.get("interest_accrual_method"),
                checked_at=now_iso,
            )
            updated += 1

            if i % _COMMIT_EVERY == 0:
                conn.commit()
                logger.info("Rate refresh progress: %d/%d checked, %d updated (committed)",
                            i, len(pending), updated)

    logger.info("Rate refresh complete: %d loans updated", updated)
    return updated
