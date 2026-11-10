#!/usr/bin/env python3
"""Import a batch of externally generated PNGs into the studio index.

Useful when images come from a generator other than the OpenAI API (for example
when the API endpoint is unreachable). The manifest keeps every prompt so the
batch stays reproducible.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio import config, exporter, imaging, store  # noqa: E402


def prune_demos(cfg: dict) -> int:
    rows = store.load(cfg)
    demos = [r for r in rows if r.get("demo")]
    for row in demos:
        path = cfg["local"] / str(row.get("file", ""))
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass
    store.save(cfg, [r for r in rows if not r.get("demo")])
    return len(demos)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="import a generated image batch")
    ap.add_argument("--manifest", default="batches/first-batch.json")
    ap.add_argument("--incoming", default=None,
                    help="folder holding the generated PNGs (default data/raw/incoming)")
    ap.add_argument("--prune-demo", action="store_true",
                    help="remove all dry-run placeholder assets from the index")
    ap.add_argument("--sync", action="store_true",
                    help="sync imported keepers to the asset root")
    ap.add_argument("--force", action="store_true",
                    help="import again even if the label already exists")
    args = ap.parse_args()

    cfg = config.load()
    manifest_path = ROOT / args.manifest
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    incoming = Path(args.incoming) if args.incoming else cfg["local"] / "raw" / "incoming"
    if not incoming.is_dir():
        print("incoming folder not found:", incoming, file=sys.stderr)
        sys.exit(2)

    removed = prune_demos(cfg) if args.prune_demo else 0
    existing = {r.get("label") for r in store.load(cfg)} if not args.force else set()

    day = store.now()[:10].replace("-", "")
    dest_dir = cfg["local"] / "raw" / day
    dest_dir.mkdir(parents=True, exist_ok=True)

    imported, skipped, missing = 0, 0, []
    for item in manifest.get("items", []):
        label = item["label"]
        if label in existing:
            skipped += 1
            continue
        matches = sorted(incoming.glob(str(item["file_prefix"]) + "*.png"))
        if not matches:
            missing.append(item["file_prefix"])
            continue
        src = matches[-1]
        style = imaging.load_style(cfg, item["style_id"])
        if not style:
            missing.append(item["style_id"] + " (unknown style)")
            continue

        asset_id = store.new_id()
        filename = asset_id + ".png"
        dest = dest_dir / filename
        shutil.copy2(src, dest)

        record = {
            "id": asset_id,
            "file": f"raw/{day}/{filename}",
            "filename": filename,
            "style_id": item["style_id"],
            "style_name": style.get("name", item["style_id"]),
            "label": label,
            "asset_kind": item.get("asset_kind", ""),
            "prompt": item.get("prompt", ""),
            "blocks": {},
            "slots": {},
            "extra": "",
            "prompt_version": 1,
            "model": manifest.get("generator", "external"),
            "quality": manifest.get("quality", "high"),
            "size": manifest.get("size", "1024x1024"),
            "cost_usd": 0.0,
            "batch_estimate": False,
            "demo": False,
            "source": "imagegen",
            "parent_id": None,
            "rating": 4,
            "tags": [item.get("asset_kind", ""), "首批", item["style_id"]],
            "favorite": False,
            "status": "keeper",
            "note": ("内置图像生成产出，不消耗 OpenAI 额度；右下角含平台"
                     "「AI生成」显式标识，按 AIGC 标识要求保留。"),
            "metadata": imaging.check_metadata(dest),
            "created_at": store.now(),
        }
        store.add(cfg, record)
        imported += 1
        print("  + %-18s %s" % (item["asset_kind"], label))

    print("imported=%d skipped=%d demos_removed=%d missing=%s" %
          (imported, skipped, removed, missing or "none"))
    if missing:
        print("WARNING: some manifest items had no matching PNG", file=sys.stderr)

    if args.sync:
        result = exporter.sync(cfg)
        print("sync:", "copied %d to %s" % (result.get("copied", 0), result.get("target"))
              if result.get("ok") else result.get("reason"))


if __name__ == "__main__":
    main()
