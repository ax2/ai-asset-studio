#!/usr/bin/env python3
"""Pre-build thumbnails for existing assets.

Run once after a bulk import so the first gallery visit does not have to
generate hundreds of thumbnails on demand.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio import config, store, thumbs  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="pre-build image thumbnails")
    ap.add_argument("--limit", type=int, default=0, help="only first N assets (0 = all)")
    args = ap.parse_args()
    cfg = config.load()
    made = cached = unsupported = 0
    started = time.time()
    for i, row in enumerate(store.iter_all(cfg)):
        if args.limit and i >= args.limit:
            break
        rel = str(row.get("file") or "")
        if not rel.lower().endswith(".png"):
            continue
        if thumbs.cache_path(cfg, rel).is_file():
            cached += 1
            continue
        try:
            if thumbs.ensure(cfg, rel):
                made += 1
            else:
                unsupported += 1
        except Exception:
            unsupported += 1
    print("done in %.1fs | made=%d cached=%d unsupported=%d"
          % (time.time() - started, made, cached, unsupported))


if __name__ == "__main__":
    main()
