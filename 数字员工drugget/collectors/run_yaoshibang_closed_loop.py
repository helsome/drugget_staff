"""Deprecated compatibility wrapper for the generic fixture runner."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from run_fixture_live_smoke import main


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="兼容入口：请优先使用 run_fixture_live_smoke.py")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--seed-key")
    group.add_argument("--store-id")
    parser.add_argument("--brand")
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    if args.store_id and not args.brand:
        parser.error("--store-id 必须与 --brand 一起使用")
    asyncio.run(main(seed_key=args.seed_key, store_id=args.store_id, brand=args.brand,
                     platform="yaoshibang", max_candidates=1, output_root=args.output_root))
