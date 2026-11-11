#!/usr/bin/env python3
"""Regenerate only selected comic panels of one episode.

    python tools/redo_panels.py --plan batches/comic-xxx.json --panels 4,5 \
        --series batches/series-xxx.json --dry-run
    python tools/redo_panels.py --plan batches/comic-xxx.json --panels 4,5 \
        --series batches/series-xxx.json

Edit the panel's `visual` (and optionally `visual_rules`) in the script first.
Only the listed panels are archived (moved, never deleted) and regenerated;
generate_comic.py's idempotent lookup skips every other panel, scene concept
and character portrait, so a two-panel fix costs two images, not a full episode.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio import config, store  # noqa: E402


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def run(cmd: list[str], what: str) -> None:
    log("执行：" + " ".join(cmd[1:]))
    rc = subprocess.run(cmd, cwd=str(ROOT)).returncode
    if rc:
        raise SystemExit(f"{what} 失败 rc={rc}")
    log(f"✓ {what} 完成")


def main() -> None:
    ap = argparse.ArgumentParser(description="只重做指定格的分镜")
    ap.add_argument("--plan", required=True, help="剧本 JSON（相对项目根）")
    ap.add_argument("--panels", required=True, help="格号，逗号分隔，如 4,5")
    ap.add_argument("--series", default="", help="可选：重建系列索引")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    plan_path = ROOT / args.plan
    d = json.loads(plan_path.read_text(encoding="utf-8"))
    bid = d["id"]
    wanted = {int(x) for x in args.panels.split(",") if x.strip()}
    numbers = {int(p["no"]) for p in d["panels"]}
    if not wanted <= numbers:
        raise SystemExit(f"格号超出剧本范围 {sorted(numbers)}：{sorted(wanted)}")
    log(f"{d['title']} · {bid} · 重做第 {sorted(wanted)} 格")

    cfg = config.load()
    targets = [r for r in store.iter_all(cfg)
               if r.get("batch_id") == bid
               and r.get("asset_kind") == "comic-panel"
               and int(r.get("panel_no") or 0) in wanted]
    for r in targets:
        log(f"  待归档 第 {r.get('panel_no')} 格 {r['id']}")
    if args.dry_run:
        log("dry-run 结束，未改动数据")
        return

    dest = (cfg["local"] / "raw" / "_superseded"
            / f"{bid}-panels-{datetime.now():%Y%m%d-%H%M%S}")
    dest.mkdir(parents=True, exist_ok=True)
    for r in targets:
        src = cfg["local"] / str(r.get("file") or "")
        if src.is_file():
            shutil.move(str(src), str(dest / Path(str(r["file"])).name))
        with store.connect(cfg) as con:
            con.execute("DELETE FROM asset_tags WHERE asset_id=?", (r["id"],))
            con.execute("DELETE FROM assets WHERE id=?", (r["id"],))
    log(f"已归档 {len(targets)} 张 → {dest}")

    py = [sys.executable, "-u"]
    run(py + ["tools/generate_comic.py", "--plan", args.plan,
              "--cards-ready"], "重做指定分镜")
    if args.series:
        run(py + ["tools/generate_comic_series.py", "--series", args.series,
                  "--index-only"], "重建系列索引")

    rows = [r for r in store.iter_all(cfg)
            if r.get("batch_id") == bid and r.get("asset_kind") == "comic-panel"]
    bad = [r for r in rows
           if not r.get("reference_used") or r.get("degraded")]
    log(f"验收：{len(rows)}/{len(numbers)} 格 · 参考图完整 "
        f"{len(rows) - len(bad)} · 降级/缺参考 {len(bad)}")
    if len(rows) != len(numbers) or bad:
        raise SystemExit("验收未通过")
    log("全部完成")


if __name__ == "__main__":
    main()
