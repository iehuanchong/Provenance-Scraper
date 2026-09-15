"""
SQLite storage for scraped origination data.

Tables
------
loans
    One row per individual loan scope. Frequency data (originator,
    servicer, timing) is populated at discovery time. amount_usd is
    populated later, once the loan funds and Figure posts a
    net-asset-value event for its scope (this can lag origination by
    days). Ledger/rate fields (interest_rate_pct, maturity_date, etc.)
    come from the `ledger` module and are available immediately at
    origination -- no funding lag, unlike amount_usd.

funding_channel_snapshots
    One row per day, capturing the FIGR_HELOC rollup's aggregate
    breakdown (securitization / participation / warehoused / pools /
    unstructured), independent of the per-loan data above.

scrape_state
    Small key/value table tracking the last successfully scraped date,
    so re-runs can resume instead of re-fetching everything.
"""

import sqlite3
import contextlib
import os
import logging

import config

logger = logging.getLogger("db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS loans (
    scope_addr         TEXT PRIMARY KEY,
    tx_hash             TEXT NOT NULL,
    block_height         INTEGER,
    block_time           TEXT NOT NULL,     -- ISO8601 UTC, from the tx
    originator_address   TEXT NOT NULL,
    servicer_address     TEXT,
    contract_spec_name   TEXT,              -- e.g. RecordPropertyLoanContract
    loan_class           TEXT,              -- best-effort: HELOC / Consumer / Auto / Other
    amount_usd           REAL,              -- NULL until NAV posts
    nav_updated_block    INTEGER,

    -- Ledger/rate fields, from the `ledger` module (provenance.ledger.v1).
    -- Available immediately at origination -- the ledger is created in
    -- the same tx as the loan scope, unlike amount_usd which waits on NAV.
    interest_rate_pct           REAL,       -- e.g. 9.3 for 9.3% (raw value / 1,000,000)
    ledger_class_id              TEXT,      -- e.g. "figure-servicing-1-0"
    status_type_id                INTEGER,  -- numeric status code (see LedgerClassStatusTypes)
    maturity_date                  INTEGER, -- days since epoch, 0 if unset
    next_pmt_date                   INTEGER,-- days since epoch, 0 if unset
    next_pmt_amt                     REAL,
    payment_frequency                 TEXT, -- e.g. PAYMENT_FREQUENCY_MONTHLY
    interest_day_count_convention      TEXT,
    interest_accrual_method             TEXT,
    rate_checked_at                      TEXT,  -- last time we polled the ledger endpoint

    discovered_at        TEXT NOT NULL,     -- when our scraper first saw it (UTC now)
    volume_checked_at    TEXT               -- last time we polled NAV for this scope
);

CREATE INDEX IF NOT EXISTS idx_loans_originator ON loans(originator_address);
CREATE INDEX IF NOT EXISTS idx_loans_block_time ON loans(block_time);
CREATE INDEX IF NOT EXISTS idx_loans_amount_null ON loans(amount_usd) WHERE amount_usd IS NULL;
CREATE INDEX IF NOT EXISTS idx_loans_rate_null ON loans(interest_rate_pct) WHERE interest_rate_pct IS NULL;

CREATE TABLE IF NOT EXISTS funding_channel_snapshots (
    snapshot_date         TEXT PRIMARY KEY,  -- 'YYYY-MM-DD' (UTC, date scraped)
    as_of_time             INTEGER,           -- epoch seconds, from the on-chain record
    supply                 REAL,
    account_count           INTEGER,
    structures_count        INTEGER,
    total_securitization     REAL,
    total_participation      REAL,
    total_warehoused         REAL,
    total_pools              REAL,
    total_unstructured       REAL,
    scraped_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scrape_state (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""


@contextlib.contextmanager
def connect():
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added after the initial release. Kept as an explicit migration
# list (rather than just relying on CREATE TABLE IF NOT EXISTS, which
# doesn't add columns to an already-existing table) so upgrading an
# existing data/originations.db in place just works.
_MIGRATIONS = [
    ("loans", "interest_rate_pct", "REAL"),
    ("loans", "ledger_class_id", "TEXT"),
    ("loans", "status_type_id", "INTEGER"),
    ("loans", "maturity_date", "INTEGER"),
    ("loans", "next_pmt_date", "INTEGER"),
    ("loans", "next_pmt_amt", "REAL"),
    ("loans", "payment_frequency", "TEXT"),
    ("loans", "interest_day_count_convention", "TEXT"),
    ("loans", "interest_accrual_method", "TEXT"),
    ("loans", "rate_checked_at", "TEXT"),
]


def _run_migrations(conn):
    for table, column, coltype in _MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            logger.info("Migrated: added %s.%s", table, column)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" in str(exc).lower():
                pass  # already applied, nothing to do
            else:
                raise


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        _run_migrations(conn)


# --- loans -------------------------------------------------------------

def upsert_loan(conn, *, scope_addr, tx_hash, block_height, block_time,
                 originator_address, servicer_address, contract_spec_name,
                 loan_class, discovered_at):
    conn.execute(
        """
        INSERT INTO loans (
            scope_addr, tx_hash, block_height, block_time,
            originator_address, servicer_address, contract_spec_name,
            loan_class, discovered_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(scope_addr) DO NOTHING
        """,
        (scope_addr, tx_hash, block_height, block_time, originator_address,
         servicer_address, contract_spec_name, loan_class, discovered_at),
    )


def scope_exists(conn, scope_addr: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM loans WHERE scope_addr = ?", (scope_addr,)
    ).fetchone()
    return row is not None


def get_unfunded_recent_scopes(conn, since_iso: str):
    """Loans discovered since `since_iso` that don't have a dollar amount yet."""
    return conn.execute(
        """
        SELECT scope_addr FROM loans
        WHERE amount_usd IS NULL AND block_time >= ?
        """,
        (since_iso,),
    ).fetchall()


def get_scopes_missing_rate(conn, limit: int = 1000):
    """Loans that don't have ledger/rate info yet. Unlike NAV, the ledger
    is created in the same tx as the loan, so there's no meaningful
    'recent window' filter needed -- any loan missing it is fair game."""
    return conn.execute(
        """
        SELECT scope_addr FROM loans
        WHERE interest_rate_pct IS NULL
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def set_loan_ledger_info(conn, scope_addr: str, *, interest_rate_pct,
                          ledger_class_id, status_type_id, maturity_date,
                          next_pmt_date, next_pmt_amt, payment_frequency,
                          interest_day_count_convention,
                          interest_accrual_method, checked_at):
    conn.execute(
        """
        UPDATE loans SET
            interest_rate_pct = ?,
            ledger_class_id = ?,
            status_type_id = ?,
            maturity_date = ?,
            next_pmt_date = ?,
            next_pmt_amt = ?,
            payment_frequency = ?,
            interest_day_count_convention = ?,
            interest_accrual_method = ?,
            rate_checked_at = ?
        WHERE scope_addr = ?
        """,
        (interest_rate_pct, ledger_class_id, status_type_id, maturity_date,
         next_pmt_date, next_pmt_amt, payment_frequency,
         interest_day_count_convention, interest_accrual_method,
         checked_at, scope_addr),
    )


def mark_rate_checked(conn, scope_addr: str, checked_at: str):
    conn.execute(
        "UPDATE loans SET rate_checked_at = ? WHERE scope_addr = ?",
        (checked_at, scope_addr),
    )


def set_loan_amount(conn, scope_addr: str, amount_usd: float,
                     nav_updated_block: int, checked_at: str):
    conn.execute(
        """
        UPDATE loans
        SET amount_usd = ?, nav_updated_block = ?, volume_checked_at = ?
        WHERE scope_addr = ?
        """,
        (amount_usd, nav_updated_block, checked_at, scope_addr),
    )


def mark_volume_checked(conn, scope_addr: str, checked_at: str):
    conn.execute(
        "UPDATE loans SET volume_checked_at = ? WHERE scope_addr = ?",
        (checked_at, scope_addr),
    )


# --- funding channel snapshots ------------------------------------------

def upsert_funding_snapshot(conn, *, snapshot_date, as_of_time, supply,
                             account_count, structures_count,
                             total_securitization, total_participation,
                             total_warehoused, total_pools,
                             total_unstructured, scraped_at):
    conn.execute(
        """
        INSERT INTO funding_channel_snapshots (
            snapshot_date, as_of_time, supply, account_count,
            structures_count, total_securitization, total_participation,
            total_warehoused, total_pools, total_unstructured, scraped_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(snapshot_date) DO UPDATE SET
            as_of_time=excluded.as_of_time,
            supply=excluded.supply,
            account_count=excluded.account_count,
            structures_count=excluded.structures_count,
            total_securitization=excluded.total_securitization,
            total_participation=excluded.total_participation,
            total_warehoused=excluded.total_warehoused,
            total_pools=excluded.total_pools,
            total_unstructured=excluded.total_unstructured,
            scraped_at=excluded.scraped_at
        """,
        (snapshot_date, as_of_time, supply, account_count, structures_count,
         total_securitization, total_participation, total_warehoused,
         total_pools, total_unstructured, scraped_at),
    )


# --- scrape state --------------------------------------------------------

def get_state(conn, key: str, default=None):
    row = conn.execute(
        "SELECT value FROM scrape_state WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def set_state(conn, key: str, value: str):
    conn.execute(
        """
        INSERT INTO scrape_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
