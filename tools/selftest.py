#!/usr/bin/env python3
"""Offline regression check for the character / game-asset pipeline.

Never calls the API and never spends money. Writes only into data/packs/,
which is gitignored. Run from the repository root:

    python tools/selftest.py
"""
from __future__ import annotations

import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio import characters, config, png, sprites, store, turnaround  # noqa: E402

CREATED_PACKS: list[str] = []
CREATED_CARDS: list[str] = []

NOVEL = (
    "雨落下来时，林晚低声道：\"走吧。\" 她一身月白长衫，乌发用一支木簪松松束起，"
    "眉眼清冷，左眉尾一道浅疤痕，腰间束着靛蓝腰带，佩一柄短剑。"
    "沈砚问她是否看清了来人。沈砚披着玄黑披风，身形高挑。"
)


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + label + ("  " + detail if detail else ""))
    if not ok:
        raise AssertionError(label)


def main() -> None:
    cfg = config.load()

    # PNG codec: RGB frames must be promoted to RGBA when packed into a sheet,
    # otherwise every channel shifts and alpha ends up 0.
    rgb = png.make(8, 8, 3)
    rgb.rows[1][0:3] = bytes((10, 20, 30))
    sh = png.sheet([rgb, rgb], 2, 8, 8)
    check("png RGB -> RGBA sheet", sh.pixel(0, 1) == (10, 20, 30, 255))
    back = png.decode(png.encode(sh))
    check("png encode/decode roundtrip", back is not None and back.bpp == 4)
    check("png split_grid", len(png.split_grid(sh, 2, 1)) == 2)

    # Novel extraction and versioned anchors.
    res = characters.extract(cfg, NOVEL, mode="local")
    names = [c["name"] for c in res["characters"]]
    check("novel name detection", "林晚" in names and "沈砚" in names, str(names))
    card = characters.put_card(cfg, dict(res["characters"][0], name="__selftest__"))
    check("anchor built", "CHARACTER ANCHOR" in card["anchor"])
    v1 = card["anchor_version"]
    edited = dict(card)
    edited["profile"] = dict(card["profile"], hair="银白长发")
    edited["anchor"] = card["anchor"]
    card2 = characters.put_card(cfg, edited)
    CREATED_CARDS.append(card2["id"])
    check("profile edit bumps anchor version", card2["anchor_version"] == v1 + 1)
    check("anchor history kept", len(card2["anchor_history"]) >= 1)

    # Four-view package in demo mode (no API, no asset-library writes).
    turn = turnaround.build(cfg, character_id=card2["id"], dry_run=True,
                            name="selftest-turnaround")
    CREATED_PACKS.append(turn["id"])
    root = Path(turn["dir"])
    check("turnaround four views", len(turn["views"]) == 4)
    check("turnaround files", all((root / f).is_file() for f in
          ("turnaround.png", "views/front.png", "views/back.png",
           "views.json", "character-card.json", "preview.html")))
    with zipfile.ZipFile(turn["zip"]) as zf:
        check("turnaround zip", zf.testzip() is None)

    # Sprite package from a real PNG, all engine exports.
    rows, _ = store.query_page(cfg, limit=2000)
    asset = next((r for r in rows if str(r.get("file", "")).lower().endswith(".png")), None)
    check("a real PNG asset exists", asset is not None, "run tools/generate.py first" if not asset else "")
    if asset:
        pack = sprites.build(cfg, asset=asset, actions=["idle", "walk"],
                             frames_per_action=4, cell=64, cols=4, fps=8,
                             name="selftest-sprites")
        CREATED_PACKS.append(pack["id"])
        proot = Path(pack["dir"])
        check("sprite frame count", pack["frames"] == 8)
        check("sprite sheet undecodable?", png.decode((proot / "sheet.png").read_bytes()) is not None)
        check("sprite per-frame PNGs", len(list((proot / "frames").glob("*.png"))) == 8)
        atlas = json.loads((proot / "sheet.json").read_text(encoding="utf-8"))
        check("sprite atlas tags", len(atlas["frames"]) == 8 and len(atlas["meta"]["frameTags"]) == 2)
        check("sprite engine exports", all((proot / f).is_file() for f in
              ("sheet.array.json", "godot/SpriteFrames.tres",
               "unity/UnitySpriteSlicer.cs", "unity/slices.json", "preview.html", "README.txt")))

    # Pack index consistency: GUI links must resolve to the DB record.
    for p in characters.list_packs(cfg):
        check("pack indexed " + p["id"][:16], characters.get_pack(cfg, p["id"]) is not None)

    print("\nALL CHECKS PASSED (offline, $0 spent)")


def cleanup() -> None:
    """Remove only the rows/files this script created; never touch real assets."""
    if not (CREATED_PACKS or CREATED_CARDS):
        return
    cfg = config.load()
    base = (cfg["local"] / "packs").resolve()
    for pid in CREATED_PACKS:
        rec = characters.get_pack(cfg, pid)
        if not rec:
            continue
        for raw in (rec.get("path"), (rec.get("data") or {}).get("dir"),
                    (rec.get("data") or {}).get("zip")):
            if not raw:
                continue
            target = Path(str(raw)).resolve()
            if not target.is_relative_to(base) or target == base:
                continue
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.is_file():
                target.unlink(missing_ok=True)
        with store.connect(cfg) as con:
            con.execute("DELETE FROM packs WHERE id=?", (pid,))
    with store.connect(cfg) as con:
        for cid in CREATED_CARDS:
            con.execute("DELETE FROM characters WHERE id=?", (cid,))
    print("cleaned up %d test pack(s) and %d test card(s)"
          % (len(CREATED_PACKS), len(CREATED_CARDS)))


if __name__ == "__main__":
    try:
        main()
    finally:
        cleanup()
