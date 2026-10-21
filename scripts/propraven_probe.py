"""Probe a PropRaven key without wasting lookups.

    PROPRAVEN_API_KEY=pz_... python3 scripts/propraven_probe.py [TX] [Dallas]

Prints, in order:
  1. quota     GET /account/usage       (free-ish, tells you what's left)
  2. coverage  GET /coverage?state=TX   (confirms the county has data)
  3. one page  GET /deals/absentee      (3 rows, to confirm the field names)

Nothing else is called. Run this before pointing the pipeline at a live key.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.providers.propraven import PropRavenError, PropRavenProvider  # noqa: E402


def main() -> int:
    state = (sys.argv[1] if len(sys.argv) > 1 else "TX").upper()
    city = sys.argv[2] if len(sys.argv) > 2 else "Dallas"

    p = PropRavenProvider()
    if not p.api_key:
        print("PROPRAVEN_API_KEY is not set.")
        print("  export PROPRAVEN_API_KEY=pz_...   (or add it to Railway env vars)")
        return 1
    print(f"key: ...{p.api_key[-6:]}   base: {p.base}\n")

    print("1) quota")
    try:
        q = p.usage()
        print(f"   used={q.used} limit={q.limit} remaining={q.remaining} period={q.period}")
        if q.low:
            print("   LOW — the pipeline will stop pulling soon.")
    except PropRavenError as e:
        print(f"   ! {e}")

    print(f"\n2) coverage for {state}")
    try:
        cov = p.coverage(state)
        print("   " + json.dumps(cov, indent=2)[:900])
    except PropRavenError as e:
        print(f"   ! {e}")

    county = p.county_fips(state, city)
    print(f"\n3) one page of /deals/absentee for {city}, {state} (county {county}) — 3 rows")
    if not county:
        print("   ! no county FIPS known for that city; set PROPRAVEN_COUNTY_FIPS")
        return 1
    try:
        rows = p.fetch(city=city, state=state, limit=3)
        print(f"   got {len(rows)} mapped rows\n")
        for r in rows:
            print("   " + json.dumps(r, indent=2, default=str)[:700].replace("\n", "\n   "))
            print()
        if not rows:
            print("   No rows came back. The county may not be covered, or the")
            print("   deal feed may need a paid plan. Check /coverage above.")
    except PropRavenError as e:
        print(f"   ! {e}")
        return 1

    print(f"lookups remaining after this run: {p.last_remaining()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
