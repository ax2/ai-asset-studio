#!/usr/bin/env python3
"""Redo one character's art and every comic panel that used the old portrait.

    python tools/redo_character.py --character 诸葛亮 --dry-run
    python tools/redo_character.py --character 诸葛亮

The character's visual spec lives in batches/sanguo-heroes.json.  Edit that
file first, then run this tool.  Superseded PNGs are moved to
data/raw/_superseded/<slug>-<timestamp>/ (never deleted) and their index rows
are removed, so re-generation cannot accidentally reuse the old look.

Steps, all idempotent:
  1. archive old portrait / turnaround / view assets + the pack row
  2. archive comic panels of the affected episode scripts
  3. clear the card's ref_asset_id and rebuild its anchor from the new profile
  4. regenerate portrait + turnaround sheet (generate_sanguo.py --force)
  5. regenerate the affected episodes (generate_comic.py --cards-ready)
  6. rebuild the two series indexes and sync to Google Drive
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

from studio import characters, config, store  # noqa: E402

MANIFEST = ROOT / "batches" / "sanguo-heroes.json"
PLANS = [
    "batches/comic-s06-du-wei-declines-office.json",
    "batches/comic-sanguo-tales-q01-e03-tale-003.json",
]
SERIES = [
    "batches/series-sanguo-tales-10.json",
    "batches/series-sanguo-tales-q01.json",
]


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def run(cmd: list[str], what: str) -> None:
    log("执行：" + " ".join(cmd[1:]))
    rc = subprocess.run(cmd, cwd=str(ROOT)).returncode
    if rc:
        raise SystemExit(f"{what} 失败 rc={rc}")
    log(f"✓ {what} 完成")


def find_card(cfg: dict, name: str) -> dict:
    card = next((c for c in characters.list_cards(cfg)
                 if c["name"] == name), None)
    if not card:
        raise SystemExit(f"找不到角色卡：{name}")
    return card


def archive_assets(cfg: dict, ids: set[str], dest: Path, dry: bool) -> int:
    moved = 0
    for aid in sorted(ids):
        rec = store.get(cfg, aid)
        if not rec:
            continue
        rel = str(rec.get("file") or "")
        src = cfg["local"] / rel
        log(f"  归档 {rec.get('asset_kind')} {aid} · {rel}")
        if dry:
            continue
        if src.is_file():
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest / Path(rel).name))
        with store.connect(cfg) as con:
            con.execute("DELETE FROM asset_tags WHERE asset_id=?", (aid,))
            con.execute("DELETE FROM assets WHERE id=?", (aid,))
        moved += 1
    return moved


def main() -> None:
    ap = argparse.ArgumentParser(description="重做某角色的立绘与相关连环画")
    ap.add_argument("--character", default="诸葛亮")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要做的事")
    args = ap.parse_args()
    name = args.character
    cfg = config.load()
    card = find_card(cfg, name)
    cid = card["id"]
    log(f"角色 {name} · 卡片 {cid} · 当前锚点 v{card.get('anchor_version')}")

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    spec = next((s for s in manifest["characters"]
                 if s["name"] == name), None)
    if not spec:
        raise SystemExit(f"清单里没有角色：{name}")
    slug = spec.get("slug") or name
    dest = (cfg["local"] / "raw" / "_superseded"
            / f"{slug}-{datetime.now():%Y%m%d-%H%M%S}")

    # 1. every asset that belongs to this character card
    own = {r["id"] for r in store.iter_all(cfg)
           if r.get("character_id") == cid}
    # 2. every panel of the affected episodes (whole episodes, so the six
    #    frames stay visually consistent with the new portrait)
    batch_ids = [json.loads((ROOT / p).read_text(encoding="utf-8"))["id"]
                 for p in PLANS]
    panels = {r["id"] for r in store.iter_all(cfg)
              if r.get("asset_kind") == "comic-panel"
              and r.get("batch_id") in batch_ids}
    log(f"待归档：角色资产 {len(own)} 张 + 连环画分镜 {len(panels)} 张")
    n = archive_assets(cfg, own | panels, dest, args.dry_run)
    log(f"已归档 {n} 张 → {dest if not args.dry_run else '(dry-run)'}")

    pack_ids = []
    if not args.dry_run:
        with store.connect(cfg) as con:
            rows = list(con.execute("SELECT id, data FROM packs"))
        for pid, raw in rows:
            d = json.loads(raw)
            if (d.get("data") or {}).get("character_id") == cid:
                pack_ids.append(pid)
        if pack_ids:
            with store.connect(cfg) as con:
                con.executemany("DELETE FROM packs WHERE id=?",
                                [(p,) for p in pack_ids])
        log(f"已归档多视角包记录 {len(pack_ids)} 条")

    if args.dry_run:
        log("dry-run 结束，未改动数据")
        return

    # 3. force the card anchor to rebuild from the edited visual spec
    fresh = dict(card)
    fresh["profile"] = dict(spec["profile"])
    fresh["anchor"] = ""          # build_anchor() will regenerate it
    fresh["ref_asset_id"] = ""    # point at the new portrait only
    saved = characters.put_card(cfg, fresh)
    log(f"角色锚点升版 v{saved['anchor_version']} · {len(saved['anchor'])} 字符")
    assert "feather" in (saved["profile"].get("negative") or ""), \
        "负面约束未写进角色卡"

    py = [sys.executable, "-u"]
    # 4. portrait + turnaround sheet
    run(py + ["tools/generate_sanguo.py", "--only", name,
              "--skip-extract", "--force"], "重生成立绘与四视图")
    # 5. affected episodes (existing scene concepts are reused)
    for p in PLANS:
        run(py + ["tools/generate_comic.py", "--plan", p,
                  "--cards-ready"], f"重做 {Path(p).name}")
    # 6. series index, ZIPs and cloud sync
    for s in SERIES:
        run(py + ["tools/generate_comic_series.py", "--series", s,
                  "--index-only"], f"重建索引 {Path(s).name}")

    # 7. sanity: every affected panel must exist with character references
    rows = [r for r in store.iter_all(cfg)
            if r.get("asset_kind") == "comic-panel"
            and r.get("batch_id") in batch_ids]
    bad = [r for r in rows
           if not r.get("reference_used") or r.get("degraded")]
    log(f"验收：{len(rows)} 格分镜 · 参考图完整 {len(rows) - len(bad)} · "
        f"降级/缺参考 {len(bad)}")
    if len(rows) != 12 or bad:
        raise SystemExit("验收未通过，请检查上面的日志")
    log("全部完成")


if __name__ == "__main__":
    main()
