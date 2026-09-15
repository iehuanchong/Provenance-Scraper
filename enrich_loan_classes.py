"""
Optional enrichment job: fills in contract_spec_name / loan_class for
loans discovered by scrape_originations.py.

This is separate from discovery because it costs one extra API call per
loan (a full scope-detail fetch, to read the session's contract spec
name) -- discovery deliberately avoids this so the hot daily path stays
cheap. Run this less frequently, or with a --limit cap, if volume grows.

Classification is a best-effort heuristic based on substrings in the
contract spec's class name (e.g. "...RecordPropertyLoanContract" implies
a property-secured product). Figure hasn't published an official
name->product mapping anywhere we found, so treat `loan_class` as a
convenience label, not ground truth -- `contract_spec_name` is stored
alongside it so you can refine the mapping yourself if needed.
"""

import logging
import datetime as dt

import db
import provenance_client as pc

logger = logging.getLogger("enrich_loan_classes")

# Best-effort substring -> product class mapping. Order matters: first
# match wins. Extend this as you observe more contract spec names.
_CLASS_PATTERNS = [
    ("Heloc", "HELOC"),
    ("HELOC", "HELOC"),
    ("ResidentialBridge", "Bridge"),
    ("Property", "Mortgage/Property"),
    ("Mortgage", "Mortgage/Property"),
    ("Auto", "Auto"),
    ("Personal", "Consumer"),
    ("Consumer", "Consumer"),
]


def classify(contract_spec_name: str | None) -> str:
    if not contract_spec_name:
        return "Unknown"
    for pattern, label in _CLASS_PATTERNS:
        if pattern in contract_spec_name:
            return label
    return "Other"


def _get_session_contract_name(scope_detail: dict) -> str | None:
    sessions = scope_detail.get("sessions", [])
    if not sessions:
        return None
    return sessions[0].get("session", {}).get("name")


def enrich_pending(limit: int = 500) -> int:
    """Enriches up to `limit` loans that don't yet have a contract_spec_name.
    Returns the number enriched."""
    enriched = 0
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT scope_addr FROM loans
            WHERE contract_spec_name IS NULL
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        for row in rows:
            scope_addr = row["scope_addr"]
            try:
                detail = pc.get_scope_detail(
                    scope_addr, include_records=False, include_sessions=True
                )
            except RuntimeError:
                logger.warning("Failed to fetch scope detail for %s, skipping", scope_addr)
                continue

            contract_name = _get_session_contract_name(detail)
            loan_class = classify(contract_name)

            conn.execute(
                """
                UPDATE loans
                SET contract_spec_name = ?, loan_class = ?
                WHERE scope_addr = ?
                """,
                (contract_name, loan_class, scope_addr),
            )
            enriched += 1

    logger.info("Enriched %d loans with class labels", enriched)
    return enriched
