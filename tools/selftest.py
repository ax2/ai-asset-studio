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

from studio import (characters, config, imaging, png, sprites,  # noqa: E402
                    store, turnaround)

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
    check("config.json is gitignored", config.config_git_ignored())
    check("public config never leaks a key", "api_key" not in config.public(cfg))

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

    # Separate-mode resume: an interrupted 4-view pack must be finished without
    # regenerating (and re-paying for) the views that already exist. Runs in a
    # throwaway library with a mocked API, so it costs $0 and touches no assets.
    import tempfile
    from unittest.mock import patch
    with tempfile.TemporaryDirectory(prefix="ai-studio-resume-") as td:
        tmp = Path(td)
        for sub in ("raw", "packs", "thumbs", "export"):
            (tmp / sub).mkdir()
        tcfg = dict(cfg)
        tcfg["local"] = tmp
        store.ensure(tcfg)
        tcard = characters.put_card(tcfg, characters.blank_card("断点续跑测试"))
        tiny = png.encode(png.make(16, 16, 4))
        calls = {"n": 0}

        def fake_generate(*_a, **_kw):
            calls["n"] += 1
            return tiny, "png"

        def fake_edit(*_a, **_kw):
            calls["n"] += 1
            return tiny

        with patch.object(imaging, "call_openai", side_effect=fake_generate), \
             patch.object(imaging, "call_openai_edit", side_effect=fake_edit):
            first = turnaround.build(tcfg, character_id=tcard["id"],
                                     mode="separate", dry_run=False,
                                     name="resume-first", size="1024x1024")
            check("separate first run generates 4 views",
                  calls["n"] == 4 and len(first["views"]) == 4)
            prior = {}
            trows, _ = store.query_page(tcfg, character_id=tcard["id"], limit=100)
            for r in trows:
                kind = str(r.get("asset_kind") or "")
                if r.get("source") == "turnaround-view" and kind.startswith("view-"):
                    prior[kind[5:]] = r
            check("all 4 views indexed before resume", len(prior) == 4)
            calls["n"] = 0
            second = turnaround.build(tcfg, character_id=tcard["id"],
                                      mode="separate", dry_run=False,
                                      name="resume-second", size="1024x1024",
                                      reuse_views=prior)
            check("resume makes zero API calls", calls["n"] == 0)
            check("resume reuses all views",
                  all(v.get("reused") for v in second["views"]))
            check("resume pack is still complete",
                  len(second["views"]) == 4 and
                  (Path(second["dir"]) / "turnaround.png").is_file())
        # ``with sqlite3.Connection`` commits but does not close the handle.
        # Release the throwaway DB before Windows tries to remove the temp dir.
        import gc
        del first, second, prior, trows, tcard, tiny
        gc.collect()

    # gpt-image-2 rejects input_fidelity with HTTP 400. The edit call must
    # retry once without that field instead of giving up on reference images.
    import io
    import urllib.error
    from unittest.mock import patch

    class FakeResponse:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return self.body

    sent = []
    errors = [
        b'{"error":{"param":"input_fidelity","message":"does not support the '
        b'\'input_fidelity\' parameter"}}',
        b'{"error":{"param":"background","message":"Transparent background '
        b'is not supported for this model."}}',
    ]

    class FakeOpener:
        def open(self, req, timeout=0):
            sent.append(req.data or b"")
            if len(sent) <= len(errors):
                raise urllib.error.HTTPError(
                    "https://api.openai.com/v1/images/edits", 400, "Bad Request", {},
                    io.BytesIO(errors[len(sent) - 1]))
            return FakeResponse(b'{"data":[{"b64_json":"AAAA"}]}')

    with patch.object(imaging, "_opener", return_value=FakeOpener()):
        out = imaging.call_openai_edit(cfg, "test", [png.encode(png.make(8, 8, 4))],
                                       model="gpt-image-2")
    check("edit drops unsupported params one by one",
          len(sent) == 3
          and b"input_fidelity" in sent[0] and b"input_fidelity" not in sent[1]
          and b"background" in sent[1] and b"background" not in sent[2]
          and out == b"\x00\x00\x00")

    # gpt-image-2.6-sunburst rejects repeated `image` fields for multi-character
    # panels, so the client must send the image[] array syntax up front and keep
    # one fallback for models that do not understand it.
    tiny = png.encode(png.make(8, 8, 4))

    class ArrayOpener:
        def __init__(self, errors):
            self.errors = errors
            self.sent = []

        def open(self, req, timeout=0):
            self.sent.append(req.data or b"")
            if len(self.sent) <= len(self.errors):
                raise urllib.error.HTTPError(
                    "https://api.openai.com/v1/images/edits", 400, "Bad Request", {},
                    io.BytesIO(self.errors[len(self.sent) - 1]))
            return FakeResponse(b'{"data":[{"b64_json":"AAAA"}]}')

    op = ArrayOpener([])
    with patch.object(imaging, "_opener", return_value=op):
        imaging.call_openai_edit(cfg, "two characters", [tiny, tiny],
                                 model="gpt-image-2.6-sunburst")
    check("multi reference uses image[] array syntax",
          len(op.sent) == 1
          and op.sent[0].count(b'name="image[]"') == 2
          and b'name="image"' not in op.sent[0])

    op = ArrayOpener([])
    with patch.object(imaging, "_opener", return_value=op):
        imaging.call_openai_edit(cfg, "one character", [tiny], model="gpt-image-2")
    check("single reference keeps plain image field",
          len(op.sent) == 1 and op.sent[0].count(b'name="image"') == 1
          and b'name="image[]"' not in op.sent[0])

    op = ArrayOpener([
        b'{"error":{"param":"image[]","message":"Unknown parameter: \'image[]\'."}}',
    ])
    with patch.object(imaging, "_opener", return_value=op):
        imaging.call_openai_edit(cfg, "two characters", [tiny, tiny],
                                 model="legacy-model")
    check("array syntax falls back to repeated image fields",
          len(op.sent) == 2
          and op.sent[0].count(b'name="image[]"') == 2
          and op.sent[1].count(b'name="image"') == 2)

    # Scene cards are first-class reproducible assets.
    from studio import scenes as scn
    test_scene = {"id": "__selftest_scene__", "name": "自检庭院",
                  "description": "空庭院", "prompt_extra": "empty courtyard",
                  "time_of_day": "morning", "mood": "quiet",
                  "batch_id": "__selftest__"}
    scn.put(cfg, test_scene)
    got = scn.get(cfg, "__selftest_scene__")
    check("scene card roundtrip", got is not None and got["name"] == "自检庭院")
    check("scene card timestamped", bool(got and got.get("created_at")))
    sp = scn.build_prompt(cfg, test_scene, style_id="sanguo-comic",
                          visual_rules="warm parchment")
    check("scene prompt forbids text and people",
          "no text" in sp and "no people" in sp)
    anchor = "CHARACTER ANCHOR verbatim."
    pp = scn.panel_prompt(
        cfg, {"no": 1, "visual": "a boy stands", "composition": "wide",
              "characters": ["曹冲"]}, test_scene,
        style_id="sanguo-comic", visual_rules="warm parchment", anchors=[anchor])
    check("panel prompt keeps anchor verbatim", anchor in pp)
    check("panel prompt is single-frame", "SINGLE COMIC PANEL" in pp)
    scn.delete(cfg, "__selftest_scene__")
    check("scene card cleaned up", scn.get(cfg, "__selftest_scene__") is None)

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
