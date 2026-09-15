"""
Offline sanity test for the DB layer and the funding-channel snapshot
extractor, using the real FIGR_HELOC scope payload we captured.

Run with: python test_db_and_snapshot.py
Uses a temporary DB file so it never touches data/originations.db.
"""

import os
import sys
import tempfile
import datetime as dt

import config

# Point at a throwaway DB before importing db (module-level DB_PATH read
# happens inside connect(), which reads config.DB_PATH each call --
# safe to override here).
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
config.DB_PATH = _tmp_db.name

import db
from snapshot_funding_channels import _extract_token_json, compute_securitization_split
from refresh_rates import _RATE_SCALE

REAL_FIGR_HELOC_SCOPE_DETAIL = {
    "records": [
        {
            "record": {
                "name": "token",
                "outputs": [
                    {
                        "hash": (
                            '{"supply":22446057111938,"account_count":5671,'
                            '"structures_count":78334,"total_securitization":9845429979512,'
                            '"total_participation":526279436529,"total_warehoused":7996838274810,'
                            '"total_pools":3437319630647,"total_unstructured":480370089530,'
                            '"as_of_time":1789045210}'
                        ),
                        "status": "RESULT_STATUS_PASS",
                    }
                ],
            }
        }
    ]
}


def test_extract_token_json():
    payload = _extract_token_json(REAL_FIGR_HELOC_SCOPE_DETAIL)
    assert payload is not None
    assert payload["structures_count"] == 78334
    assert payload["total_securitization"] == 9845429979512
    print("test_extract_token_json: OK")


def test_securitization_split():
    payload = _extract_token_json(REAL_FIGR_HELOC_SCOPE_DETAIL)
    split = compute_securitization_split(payload)

    expected_securitized = 9845429979512
    expected_whole_loan = 526279436529 + 7996838274810 + 3437319630647 + 480370089530

    assert split["securitized_usd"] == expected_securitized
    assert split["whole_loan_usd"] == expected_whole_loan
    assert abs(split["securitized_share"] - (expected_securitized / (expected_securitized + expected_whole_loan))) < 1e-9

    # Sanity: with real FIGR_HELOC numbers, whole-loan channels (participation
    # + warehoused + pools + unstructured) outweigh pure securitization.
    assert split["whole_loan_usd"] > split["securitized_usd"]

    print("test_securitization_split: OK")


REAL_LEDGER_RESPONSE = {
    "key": {
        "nft_id": "scope1qqql7kjtnh5y629qjz9lfsy7c8fsujt8tq",
        "asset_class_id": "scopespec1q32dmk9ux5q50zvx7x7kvkh37c7svqc3pg",
    },
    "ledger_class_id": "figure-servicing-1-0",
    "status_type_id": 1,
    "next_pmt_date": 0,
    "next_pmt_amt": "0",
    "interest_rate": 9300000,
    "maturity_date": 0,
    "interest_day_count_convention": "DAY_COUNT_CONVENTION_ACTUAL_ACTUAL",
    "interest_accrual_method": "INTEREST_ACCRUAL_METHOD_SIMPLE_INTEREST",
    "payment_frequency": "PAYMENT_FREQUENCY_MONTHLY",
}


def test_ledger_rate_scale():
    # 9,300,000 raw -> 9.3% (confirmed live against real chain data,
    # matches debloc's own published HELOC/Consumer median rate range)
    pct = REAL_LEDGER_RESPONSE["interest_rate"] / _RATE_SCALE
    assert abs(pct - 9.3) < 1e-9
    print("test_ledger_rate_scale: OK")


def test_ledger_db_roundtrip():
    db.init_db()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    scope_addr = "scope1qqql7kjtnh5y629qjz9lfsy7c8fsujt8tq"

    with db.connect() as conn:
        # loan row already exists from test_db_roundtrip(); if this test
        # runs standalone, insert a minimal one first.
        if not db.scope_exists(conn, scope_addr):
            db.upsert_loan(
                conn, scope_addr=scope_addr, tx_hash="X", block_height=1,
                block_time=now, originator_address="pb1someone",
                servicer_address=None, contract_spec_name=None,
                loan_class=None, discovered_at=now,
            )

        db.set_loan_ledger_info(
            conn, scope_addr,
            interest_rate_pct=REAL_LEDGER_RESPONSE["interest_rate"] / _RATE_SCALE,
            ledger_class_id=REAL_LEDGER_RESPONSE["ledger_class_id"],
            status_type_id=REAL_LEDGER_RESPONSE["status_type_id"],
            maturity_date=REAL_LEDGER_RESPONSE["maturity_date"],
            next_pmt_date=REAL_LEDGER_RESPONSE["next_pmt_date"],
            next_pmt_amt=float(REAL_LEDGER_RESPONSE["next_pmt_amt"]),
            payment_frequency=REAL_LEDGER_RESPONSE["payment_frequency"],
            interest_day_count_convention=REAL_LEDGER_RESPONSE["interest_day_count_convention"],
            interest_accrual_method=REAL_LEDGER_RESPONSE["interest_accrual_method"],
            checked_at=now,
        )

    with db.connect() as conn:
        row = conn.execute(
            "SELECT interest_rate_pct, ledger_class_id, payment_frequency FROM loans WHERE scope_addr = ?",
            (scope_addr,),
        ).fetchone()
        assert abs(row["interest_rate_pct"] - 9.3) < 1e-9
        assert row["ledger_class_id"] == "figure-servicing-1-0"
        assert row["payment_frequency"] == "PAYMENT_FREQUENCY_MONTHLY"

    print("test_ledger_db_roundtrip: OK")


def test_derived_metrics():
    """Exercises compute_originator_metrics against a small synthetic
    dataset (values chosen by hand so results are easy to verify)."""
    import compute_originator_metrics as metrics

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    with db.connect() as conn:
        sample_loans = [
            # originator A: two loans, one class, clearly dominant
            ("scopeA1", "pb1A", "2026-01-05T00:00:00Z", "HELOC", 100_000.0),
            ("scopeA2", "pb1A", "2026-02-05T00:00:00Z", "HELOC", 200_000.0),
            # originator B: smaller, mixed classes, starts later
            ("scopeB1", "pb1B", "2026-02-10T00:00:00Z", "Consumer", 10_000.0),
            ("scopeB2", "pb1B", "2026-02-15T00:00:00Z", "Auto", 5_000.0),
        ]
        for scope, originator, block_time, loan_class, amount in sample_loans:
            db.upsert_loan(
                conn, scope_addr=scope, tx_hash=f"tx-{scope}", block_height=1,
                block_time=block_time, originator_address=originator,
                servicer_address=None, contract_spec_name=None,
                loan_class=loan_class, discovered_at=now,
            )
            db.set_loan_amount(conn, scope, amount_usd=amount,
                                nav_updated_block=1, checked_at=now)

    with db.connect() as conn:
        mix = metrics.originator_mix(conn, days=None)
        a_heloc = next(r for r in mix if r["originator_address"] == "pb1A" and r["loan_class"] == "HELOC")
        assert a_heloc["volume_usd"] == 300_000.0
        assert abs(a_heloc["share_of_originator_volume"] - 1.0) < 1e-9

        first_loans = {r["originator_address"]: r["first_loan_date"] for r in metrics.originator_first_loan(conn)}
        assert first_loans["pb1A"] == "2026-01-05"
        assert first_loans["pb1B"] == "2026-02-10"

        new_partners = {r["month"]: r["new_partner_count"] for r in metrics.new_partners_monthly(conn)}
        assert new_partners.get("2026-01") == 1  # originator A
        assert new_partners.get("2026-02") == 1  # originator B

        conc = {r["month"]: r for r in metrics.concentration_monthly(conn)}
        feb = conc["2026-02"]
        # Feb volume: A=200k, B=15k -> total 215k, A's share dominates
        assert abs(feb["total_volume_usd"] - 215_000.0) < 1e-6
        assert feb["top5_share"] > 0.9  # only 2 originators, both counted

        ramp = metrics.originator_ramp_curves(conn, top_n=10)
        a_ramp = [r for r in ramp if r["originator_address"] == "pb1A"]
        assert a_ramp[-1]["cumulative_funded_volume_usd"] == 300_000.0

    print("test_derived_metrics: OK")


def test_db_roundtrip():
    db.init_db()
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    with db.connect() as conn:
        db.upsert_loan(
            conn,
            scope_addr="scope1qqql7kjtnh5y629qjz9lfsy7c8fsujt8tq",
            tx_hash="580E0EC1A7FFD5C78368F4107F80305ABB073CA9889E7EA8EECAED43401BF98D",
            block_height=33393014,
            block_time="2026-09-10T20:50:21.196318775Z",
            originator_address="pb1k38pa6rgnepcs592n4mvr3hxucw8tslprcmlvk",
            servicer_address="pb1rrwuxd65chqed6t0c5z8z505daw0rvsveyl0eq",
            contract_spec_name=None,
            loan_class=None,
            discovered_at=now,
        )
        # Duplicate insert should be a silent no-op (ON CONFLICT DO NOTHING)
        db.upsert_loan(
            conn,
            scope_addr="scope1qqql7kjtnh5y629qjz9lfsy7c8fsujt8tq",
            tx_hash="DUPLICATE_ATTEMPT",
            block_height=1,
            block_time=now,
            originator_address="someone_else",
            servicer_address=None,
            contract_spec_name=None,
            loan_class=None,
            discovered_at=now,
        )

    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM loans WHERE scope_addr = ?",
            ("scope1qqql7kjtnh5y629qjz9lfsy7c8fsujt8tq",),
        ).fetchone()
        assert row["originator_address"] == "pb1k38pa6rgnepcs592n4mvr3hxucw8tslprcmlvk"
        assert row["tx_hash"] == "580E0EC1A7FFD5C78368F4107F80305ABB073CA9889E7EA8EECAED43401BF98D"
        assert row["amount_usd"] is None

        db.set_loan_amount(conn, row["scope_addr"], amount_usd=200000.0,
                            nav_updated_block=33122086, checked_at=now)

    with db.connect() as conn:
        row = conn.execute(
            "SELECT amount_usd FROM loans WHERE scope_addr = ?",
            ("scope1qqql7kjtnh5y629qjz9lfsy7c8fsujt8tq",),
        ).fetchone()
        assert row["amount_usd"] == 200000.0

    print("test_db_roundtrip: OK")


def test_funding_snapshot_roundtrip():
    with db.connect() as conn:
        db.upsert_funding_snapshot(
            conn,
            snapshot_date="2026-09-10",
            as_of_time=1789045210,
            supply=22446057111938,
            account_count=5671,
            structures_count=78334,
            total_securitization=9845429979512,
            total_participation=526279436529,
            total_warehoused=7996838274810,
            total_pools=3437319630647,
            total_unstructured=480370089530,
            scraped_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM funding_channel_snapshots WHERE snapshot_date = ?",
            ("2026-09-10",),
        ).fetchone()
        assert row["structures_count"] == 78334
    print("test_funding_snapshot_roundtrip: OK")


def main():
    test_extract_token_json()
    test_securitization_split()
    test_ledger_rate_scale()
    test_db_roundtrip()
    test_ledger_db_roundtrip()
    test_derived_metrics()
    test_funding_snapshot_roundtrip()
    os.unlink(_tmp_db.name)
    print("\nAll DB/snapshot tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
