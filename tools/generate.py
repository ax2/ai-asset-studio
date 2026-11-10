#!/usr/bin/env python3
"""Command line entry point. Dry-run is the default so a typo never spends money."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio import config, exporter, imaging, pricing, store  # noqa: E402


def slot_matrix(style: dict, count: int) -> list:
    slots = style.get("slots", {})
    out = []
    for i in range(count):
        picked = {}
        for key in ("subject", "asset_kind", "mood", "concept"):
            values = slots.get(key) or []
            if values:
                picked[key] = values[i % len(values)]
        out.append(picked)
    return out


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="ai-asset-studio CLI")
    ap.add_argument("--style", default="retro-film-diary")
    ap.add_argument("--count", type=int, default=30)
    ap.add_argument("--model", default=None)
    ap.add_argument("--quality", default=None)
    ap.add_argument("--extra", default="")
    ap.add_argument("--execute", action="store_true",
                    help="actually call the API (default is dry-run)")
    ap.add_argument("--batch-estimate", action="store_true",
                    help="apply the 50% batch discount to the cost estimate only")
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--health", action="store_true")
    ap.add_argument("--list-styles", action="store_true")
    args = ap.parse_args()

    cfg = config.load()

    if args.list_styles:
        for style in imaging.load_styles(cfg):
            print("%-22s %-10s %s" % (style["id"], style.get("status", ""),
                                      style.get("name", "")))
        return
    if args.stats:
        print(json.dumps(store.stats(cfg), ensure_ascii=False, indent=2))
        return
    if args.health:
        print(json.dumps(exporter.health(cfg), ensure_ascii=False, indent=2))
        return
    if args.sync:
        print(json.dumps(exporter.sync(cfg, args.style), ensure_ascii=False, indent=2))
        return
    if args.export:
        rows = store.query(cfg, style=args.style, limit=500)
        print(json.dumps(exporter.export_zip(cfg, rows), ensure_ascii=False, indent=2))
        return

    style = imaging.load_style(cfg, args.style)
    if not style:
        print("unknown style: %s" % args.style, file=sys.stderr)
        sys.exit(2)

    model = args.model or style.get("model", {}).get("default") or cfg["default_model"]
    quality = args.quality or style.get("model", {}).get("quality") or cfg["default_quality"]
    count = max(1, min(args.count, 50))
    dry = not args.execute
    estimate = pricing.estimate(model, quality, count, batch=args.batch_estimate)

    print("style   : %s (%s)" % (style["id"], style.get("name", "")))
    print("mode    : %s" % ("DRY RUN - no API call" if dry else "REAL RUN"))
    print("model   : %s / %s" % (model, quality))
    print("count   : %d" % count)
    print("estimate: $%.4f" % estimate)

    if not dry:
        if not config.has_key(cfg):
            print("OPENAI_API_KEY is not set. Nothing was generated.", file=sys.stderr)
            sys.exit(2)
        cap = float((style.get("budget") or {}).get("per_run_cap_usd") or 2)
        if estimate > cap:
            print("estimate exceeds this style's per-run cap ($%.2f). Aborted." % cap,
                  file=sys.stderr)
            sys.exit(2)
        if store.stats(cfg)["real_cost_usd"] + estimate > float(cfg["monthly_budget_usd"]):
            print("would exceed the monthly budget. Aborted.", file=sys.stderr)
            sys.exit(2)

    for i, slots in enumerate(slot_matrix(style, count), 1):
        res = imaging.generate(cfg, style_id=style["id"], slots=slots,
                               extra=args.extra, model=model, quality=quality,
                               dry_run=dry, batch_estimate=args.batch_estimate)
        print("[%2d/%2d] %s  %s" % (i, count, res["asset"]["id"],
                                    "demo" if res["demo"] else "generated"))
    print("done. open the studio to review: python server.py")


if __name__ == "__main__":
    main()
