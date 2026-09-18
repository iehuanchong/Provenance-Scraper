"""
Optional enrichment job: fills in contract_spec_name / loan_class for
loans discovered by scrape_originations.py.

This is separate from discovery because it costs one extra API call per
loan (a full scope-detail fetch, to read the session's contract spec
name) -- discovery deliberately avoids this so the hot daily path stays
cheap. Run this less frequently, or with a --limit cap, if volume grows.

CLASSIFICATION HISTORY / CONFIDENCE LEVEL -- read before trusting this:

v1 of this classifier guessed at substrings like "HELOC", "Auto",
"Personal" appearing in the contract spec name. That guess was WRONG:
against a real day of production data (1,204 loans), every single
enriched loan's contract_spec_name was one of exactly two values --
"RecordPropertyLoanContract" or "RecordLoanContract" -- neither of
which contains any of the guessed substrings. These are generic
pipeline-step names in Figure's loan-recording system, not
per-product identifiers on their face.

v2 (current) maps those two exact names directly:
  RecordPropertyLoanContract -> "HELOC/Property-Secured"
  RecordLoanContract         -> "Consumer/Unsecured"

Evidence for this mapping: in that same real-data sample, 79.3% of
enriched loans were RecordPropertyLoanContract vs debloc.ai's own
published HELOC share of loan count (76.0%, from their origination ->
funding table) -- a close match. This is CIRCUMSTANTIAL, not confirmed:
the interest rate ranges for the two groups overlap heavily (6.15-15.03%
vs 6.75-13.3%), which doesn't cleanly separate them on its own. The
stronger confirmation -- checking whether RecordPropertyLoanContract
scopes actually carry a `lien_property` record (a real-estate lien) and
RecordLoanContract scopes don't -- was planned but blocked by
api.provenance.io being unreachable from the environment that wrote
this. If you can run a live check (fetch a scope of each type with
include_records=true and look for a `lien_property` record name), do
that before trusting this classification for anything important.

Also unresolved: this only distinguishes "property-secured" from
"unsecured" -- it can't currently split Consumer from Auto, since we've
never observed a contract name that looks Auto-specific. If Auto loans
exist in your data, they're currently lumped into "Consumer/Unsecured".

`contract_spec_name` is always stored alongside `loan_class`, so you can
re-derive a better mapping later without re-scraping anything.
"""

import logging
import datetime as dt

import db
import provenance_client as pc

logger = logging.getLogger("enrich_loan_classes")

# Exact contract-name matches take priority -- these are the only two
# names actually observed in production as of this writing (see module
# docstring for the evidence and its limits).
_EXACT_MATCHES = {
    "com.figure.los.contract.origination.RecordPropertyLoanContract": "HELOC/Property-Secured",
    "com.figure.los.contract.origination.RecordLoanContract": "Consumer/Unsecured",
}

# Fallback substring patterns, kept in case a not-yet-seen contract name
# turns out to be more explicit (e.g. if an Auto-specific contract name
# shows up later). Order matters: first match wins.
_SUBSTRING_PATTERNS = [
    ("Heloc", "HELOC/Property-Secured"),
    ("HELOC", "HELOC/Property-Secured"),
    ("ResidentialBridge", "Bridge"),
    ("Property", "HELOC/Property-Secured"),
    ("Mortgage", "HELOC/Property-Secured"),
    ("Auto", "Auto"),
    ("Personal", "Consumer/Unsecured"),
    ("Consumer", "Consumer/Unsecured"),
]


def classify(contract_spec_name: str | None) -> str:
    if not contract_spec_name:
        return "Unknown"
    if contract_spec_name in _EXACT_MATCHES:
        return _EXACT_MATCHES[contract_spec_name]
    for pattern, label in _SUBSTRING_PATTERNS:
        if pattern in contract_spec_name:
            return label
    return "Other"


def _get_session_contract_name(scope_detail: dict) -> str | None:
    sessions = scope_detail.get("sessions", [])
    if not sessions:
        return None
    return sessions[0].get("session", {}).get("name")


def reclassify_existing() -> int:
    """
    Re-runs classify() against every loan that already has a
    contract_spec_name stored, using the CURRENT mapping. Free -- no API
    calls, just re-applying the classification logic to data we already
    have. Run this whenever _EXACT_MATCHES / _SUBSTRING_PATTERNS changes,
    so historical rows self-heal instead of staying stuck with whatever
    the classifier said at the time they were first enriched.
    """
    updated = 0
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT scope_addr, contract_spec_name, loan_class FROM loans "
            "WHERE contract_spec_name IS NOT NULL"
        ).fetchall()
        for row in rows:
            new_class = classify(row["contract_spec_name"])
            if new_class != row["loan_class"]:
                conn.execute(
                    "UPDATE loans SET loan_class = ? WHERE scope_addr = ?",
                    (new_class, row["scope_addr"]),
                )
                updated += 1
    logger.info("Reclassified %d loans under the current mapping", updated)
    return updated


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
