"""
Offline sanity test for parse_loans_from_tx, using a real transaction
we pulled and verified by hand during development (tx
580E0EC1A7FFD5C78368F4107F80305ABB073CA9889E7EA8EECAED43401BF98D --
two loans batched in one tx, both originated + serviced by the same
pair of addresses).

Run with: python test_parsing.py
"""

import json
import sys

from scrape_originations import parse_loans_from_tx

SAMPLE_TX = {
    "time": "2026-09-10T20:50:21.196318775Z",
    "height": 33393014,
    "events": [
        {"type": "provenance.registry.v1.EventRoleGranted", "attributes": [
            {"key": "addresses", "value": '["pb1k38pa6rgnepcs592n4mvr3hxucw8tslprcmlvk"]'},
            {"key": "asset_class_id", "value": '"scopespec1q32dmk9ux5q50zvx7x7kvkh37c7svqc3pg"'},
            {"key": "nft_id", "value": '"scope1qqql7kjtnh5y629qjz9lfsy7c8fsujt8tq"'},
            {"key": "role", "value": '"ORIGINATOR"'},
        ]},
        {"type": "provenance.registry.v1.EventRoleGranted", "attributes": [
            {"key": "addresses", "value": '["pb1rrwuxd65chqed6t0c5z8z505daw0rvsveyl0eq"]'},
            {"key": "asset_class_id", "value": '"scopespec1q32dmk9ux5q50zvx7x7kvkh37c7svqc3pg"'},
            {"key": "nft_id", "value": '"scope1qqql7kjtnh5y629qjz9lfsy7c8fsujt8tq"'},
            {"key": "role", "value": '"SERVICER"'},
        ]},
        {"type": "provenance.registry.v1.EventRoleGranted", "attributes": [
            {"key": "addresses", "value": '["pb1k38pa6rgnepcs592n4mvr3hxucw8tslprcmlvk"]'},
            {"key": "asset_class_id", "value": '"scopespec1q32dmk9ux5q50zvx7x7kvkh37c7svqc3pg"'},
            {"key": "nft_id", "value": '"scope1qr2haslhnzh58s9yf7wmehpum3wqq522as"'},
            {"key": "role", "value": '"ORIGINATOR"'},
        ]},
        {"type": "provenance.registry.v1.EventRoleGranted", "attributes": [
            {"key": "addresses", "value": '["pb1rrwuxd65chqed6t0c5z8z505daw0rvsveyl0eq"]'},
            {"key": "asset_class_id", "value": '"scopespec1q32dmk9ux5q50zvx7x7kvkh37c7svqc3pg"'},
            {"key": "nft_id", "value": '"scope1qr2haslhnzh58s9yf7wmehpum3wqq522as"'},
            {"key": "role", "value": '"SERVICER"'},
        ]},
        # A non-loan role-granted event (different asset_class_id) --
        # should be filtered out.
        {"type": "provenance.registry.v1.EventRoleGranted", "attributes": [
            {"key": "addresses", "value": '["pb1someotherclassaddress"]'},
            {"key": "asset_class_id", "value": '"scopespec1qNOTaLOAN"'},
            {"key": "nft_id", "value": '"scope1qNOTaLOAN"'},
            {"key": "role", "value": '"ORIGINATOR"'},
        ]},
    ],
}


def main():
    loans = parse_loans_from_tx(SAMPLE_TX)
    print(json.dumps(loans, indent=2))

    assert len(loans) == 2, f"expected 2 loans, got {len(loans)}"
    scope_addrs = {loan["scope_addr"] for loan in loans}
    assert scope_addrs == {
        "scope1qqql7kjtnh5y629qjz9lfsy7c8fsujt8tq",
        "scope1qr2haslhnzh58s9yf7wmehpum3wqq522as",
    }, "unexpected scope addresses"

    for loan in loans:
        assert loan["originator_address"] == "pb1k38pa6rgnepcs592n4mvr3hxucw8tslprcmlvk"
        assert loan["servicer_address"] == "pb1rrwuxd65chqed6t0c5z8z505daw0rvsveyl0eq"

    # Confirm the unrelated asset class was excluded
    assert "scope1qNOTaLOAN" not in scope_addrs

    print("\nAll assertions passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
