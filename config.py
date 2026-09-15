"""
Central configuration for the Figure/Provenance origination scraper.
"""

import os

# --- API bases -------------------------------------------------------------
# Provenance Foundation's block explorer backend (Figure-hosted). Used for
# transaction search (txs/recent) and human-friendly scope lookups.
SERVICE_EXPLORER_BASE = "https://service-explorer.provenance.io/api/v2"

# Raw Provenance chain REST/LCD API (Figure-run public node). Used for
# scope detail, net-asset-value lookups, and registry role queries.
CHAIN_API_BASE = "https://api.provenance.io"

# --- Known addresses / specs -------------------------------------------
# Fee-grant sponsor address Figure uses for all origination txs. Not an
# originator itself -- useful for sanity-checking parsed data.
FIGURE_FEE_GRANTER = "pb1udwktv0xef4zh6xw5mjaz7lc9pwfzl8lulvw96"

# scope spec for individual loan originations (module: nft/metadata,
# name: "com.figure.origination.loan"). Discovered via
# /provenance/metadata/v1/scopespecs/all.
LOAN_ORIGINATION_SCOPE_SPEC = "scopespec1q32dmk9ux5q50zvx7x7kvkh37c7svqc3pg"

# The FIGR_HELOC "UPB Token" rollup scope -- carries the daily
# funding-channel breakdown (securitization/participation/warehoused/
# pools/unstructured) as a JSON blob inside its single "token" record.
FIGR_HELOC_ROLLUP_SCOPE = "scope1qrm5d0wjzamyywvjuws6774ljmrqu8kh9x"

# --- HTTP behaviour ----------------------------------------------------
REQUEST_TIMEOUT_SECONDS = 20
MAX_RETRIES = 5
RETRY_BACKOFF_BASE_SECONDS = 1.5
MAX_BACKOFF_SECONDS = 30          # cap a single retry's wait; rely on the
                                   # shared cooldown (below) for sustained throttling

# Per-host minimum delay between requests. These were NOT the same in
# practice: service-explorer.provenance.io handled 1,200+ sequential
# calls at 0.25s pacing with zero 429s during a real run. api.provenance.io
# started 429-ing almost every single request at that same pacing once
# enrichment + rate-refresh both started hammering it back-to-back --
# so it gets a much larger minimum delay.
SERVICE_EXPLORER_REQUEST_DELAY_SECONDS = 0.25
CHAIN_API_REQUEST_DELAY_SECONDS = 2.0

# Kept for backwards compatibility with anything importing the old name.
REQUEST_DELAY_SECONDS = SERVICE_EXPLORER_REQUEST_DELAY_SECONDS

# Page size for txs/recent search
TX_PAGE_SIZE = 100

# How many days back to look when refreshing NAV (loan amounts) for
# scopes that haven't funded yet. Figure's own median time-to-fund is
# ~2-6 days depending on product, so 45 gives headroom.
NAV_REFRESH_WINDOW_DAYS = 45

# --- Storage -------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "originations.db")
EXPORTS_DIR = os.path.join(BASE_DIR, "exports")
