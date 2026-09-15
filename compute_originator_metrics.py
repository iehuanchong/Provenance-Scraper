"""
Derived/computed originator-level metrics -- the analytics debloc.ai
surfaces (mix, share, ramp curves, new-partner counts, concentration)
that don't need any additional scraping, only aggregation over the raw
`loans` table we already populate.

Kept as a separate module (rather than baked into run_daily.py) so the
definitions are easy to find and revise later without touching the
scraping logic itself. Each function returns a list of sqlite3.Row-like
dicts; run_daily.py writes them to CSV so a dated snapshot of "what the
metrics looked like as of this run" survives in git history even though
the underlying numbers are always fully recomputable from raw data.

All dollar figures use amount_usd, which is NULL until a loan's NAV
posts (see refresh_volumes.py) -- SQLite's SUM/AVG ignore NULLs
automatically, so unfunded loans simply don't contribute to volume
figures yet, but do still count in loan-count/frequency figures.
"""

import sqlite3

import db


def originator_mix(conn: sqlite3.Connection, days: int | None = None) -> list[dict]:
    """
    Per-originator breakdown of volume/count by loan_class.
    days=None -> all-time. days=30 -> trailing 30 days from the most
    recent loan in the table (not wall-clock 'now', so this stays
    meaningful when back-filling historical data).
    """
    date_filter = ""
    params: tuple = ()
    if days is not None:
        date_filter = """
            WHERE date(block_time) >= date(
                (SELECT MAX(block_time) FROM loans), ?
            )
        """
        params = (f"-{days} days",)

    rows = conn.execute(
        f"""
        SELECT
            originator_address,
            COALESCE(loan_class, 'Unknown') AS loan_class,
            COUNT(*) AS loan_count,
            SUM(amount_usd) AS volume_usd
        FROM loans
        {date_filter}
        GROUP BY originator_address, loan_class
        ORDER BY originator_address, volume_usd DESC
        """,
        params,
    ).fetchall()

    # Add each originator's share of their own total, so "mix" is directly
    # readable as a percentage per class.
    totals: dict[str, float] = {}
    for row in rows:
        totals[row["originator_address"]] = totals.get(row["originator_address"], 0) + (row["volume_usd"] or 0)

    out = []
    for row in rows:
        total = totals.get(row["originator_address"], 0)
        share = (row["volume_usd"] or 0) / total if total else None
        out.append({
            "originator_address": row["originator_address"],
            "loan_class": row["loan_class"],
            "loan_count": row["loan_count"],
            "volume_usd": row["volume_usd"],
            "share_of_originator_volume": share,
            "period": f"trailing_{days}d" if days else "all_time",
        })
    return out


def originator_first_loan(conn: sqlite3.Connection) -> list[dict]:
    """First-ever origination date per originator -- the basis for both
    ramp curves and new-partner-onboarding counts."""
    rows = conn.execute(
        """
        SELECT originator_address, MIN(date(block_time)) AS first_loan_date
        FROM loans
        GROUP BY originator_address
        ORDER BY first_loan_date
        """
    ).fetchall()
    return [dict(row) for row in rows]


def new_partners_monthly(conn: sqlite3.Connection) -> list[dict]:
    """Count of originators whose first-ever loan falls in each calendar month."""
    rows = conn.execute(
        """
        WITH first_loans AS (
            SELECT originator_address, MIN(date(block_time)) AS first_loan_date
            FROM loans
            GROUP BY originator_address
        )
        SELECT
            strftime('%Y-%m', first_loan_date) AS month,
            COUNT(*) AS new_partner_count
        FROM first_loans
        GROUP BY month
        ORDER BY month
        """
    ).fetchall()
    return [dict(row) for row in rows]


def originator_ramp_curves(conn: sqlite3.Connection, top_n: int = 10) -> list[dict]:
    """
    Cumulative funded volume per originator, bucketed by months-since-
    their-first-loan. Limited to the top `top_n` originators by all-time
    volume, matching debloc's "Top 5" style ramp-curve chart.
    """
    top_originators = conn.execute(
        """
        SELECT originator_address, SUM(amount_usd) AS total_volume
        FROM loans
        GROUP BY originator_address
        ORDER BY total_volume DESC
        LIMIT ?
        """,
        (top_n,),
    ).fetchall()
    top_addrs = [row["originator_address"] for row in top_originators]
    if not top_addrs:
        return []

    placeholders = ",".join("?" for _ in top_addrs)
    rows = conn.execute(
        f"""
        WITH first_loans AS (
            SELECT originator_address, MIN(date(block_time)) AS first_loan_date
            FROM loans
            GROUP BY originator_address
        ),
        dated AS (
            SELECT
                l.originator_address,
                CAST(
                    (julianday(date(l.block_time)) - julianday(f.first_loan_date)) / 30.44
                    AS INTEGER
                ) AS months_since_first_loan,
                l.amount_usd
            FROM loans l
            JOIN first_loans f ON f.originator_address = l.originator_address
            WHERE l.originator_address IN ({placeholders})
        )
        SELECT
            originator_address,
            months_since_first_loan,
            SUM(amount_usd) AS period_volume_usd
        FROM dated
        GROUP BY originator_address, months_since_first_loan
        ORDER BY originator_address, months_since_first_loan
        """,
        top_addrs,
    ).fetchall()

    # Turn period volumes into a running cumulative total per originator.
    out = []
    running: dict[str, float] = {}
    for row in rows:
        addr = row["originator_address"]
        running[addr] = running.get(addr, 0) + (row["period_volume_usd"] or 0)
        out.append({
            "originator_address": addr,
            "months_since_first_loan": row["months_since_first_loan"],
            "cumulative_funded_volume_usd": running[addr],
        })
    return out


def concentration_monthly(conn: sqlite3.Connection) -> list[dict]:
    """
    Per calendar month: top-5 originator share of that month's volume,
    plus a Herfindahl-Hirschman-style concentration index (sum of squared
    market shares across all active originators that month -- ranges
    0 to 1, higher = more concentrated).
    """
    rows = conn.execute(
        """
        SELECT
            strftime('%Y-%m', block_time) AS month,
            originator_address,
            SUM(amount_usd) AS volume_usd
        FROM loans
        WHERE amount_usd IS NOT NULL
        GROUP BY month, originator_address
        """
    ).fetchall()

    by_month: dict[str, list[tuple[str, float]]] = {}
    for row in rows:
        by_month.setdefault(row["month"], []).append(
            (row["originator_address"], row["volume_usd"] or 0)
        )

    out = []
    for month, entries in sorted(by_month.items()):
        total = sum(v for _, v in entries)
        if total <= 0:
            continue
        shares = sorted((v / total for _, v in entries), reverse=True)
        top5_share = sum(shares[:5])
        hhi = sum(s * s for s in shares)
        out.append({
            "month": month,
            "active_originators": len(entries),
            "total_volume_usd": total,
            "top5_share": top5_share,
            "concentration_index_hhi": hhi,
        })
    return out


def compute_all(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Convenience: runs every derived metric and returns them keyed by name."""
    return {
        "originator_mix_all_time": originator_mix(conn, days=None),
        "originator_mix_trailing_30d": originator_mix(conn, days=30),
        "originator_first_loan": originator_first_loan(conn),
        "new_partners_monthly": new_partners_monthly(conn),
        "originator_ramp_curves": originator_ramp_curves(conn, top_n=10),
        "concentration_monthly": concentration_monthly(conn),
    }
