"""Register approved ex-date rights. Preview by default; no automatic cash credit.

Run from server/: python -m scripts.register_dividend_entitlement manifest.json
Use --apply only after record-date holdings and exact settlement identity review.
"""

import argparse
import json
from pathlib import Path

from app.db import make_engine, make_session_factory
from app.services.dividend_entitlement import DividendEntitlementService, DividendRequest
from app.settings import get_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    request = DividendRequest.model_validate_json(args.manifest.read_text())
    engine = make_engine(get_settings().db_url)
    try:
        result = DividendEntitlementService(make_session_factory(engine)).register(
            request, apply=args.apply
        )
        print(json.dumps(result, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
