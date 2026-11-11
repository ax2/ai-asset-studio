#!/usr/bin/env python3
"""Revise 望梅止渴: show the marching column and give Cao Cao campaign armour.

The original script hard-coded "never more than two visible people, no
background crowds" and "reuse the existing Cao Cao asset without redesign",
which produced a court-robed Cao Cao and a single soldier on an empty road.
This tool rewrites those rules, introduces a dedicated per-episode character
card for Cao Cao in marching gear (the global court-robe card is left alone so
other episodes are unaffected), archives the six superseded panels and reruns
the episode plus the q01 series index.

    python tools/revise_wangmei.py --dry-run    # show the plan only
    python tools/revise_wangmei.py
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

PLAN = ROOT / "batches" / "comic-sanguo-tales-q01-e10-tale-010.json"
PLAN_REL = "batches/comic-sanguo-tales-q01-e10-tale-010.json"
SERIES = "batches/series-sanguo-tales-q01.json"
OLD_NAME, NEW_NAME = "曹操", "曹操·行军"

PROFILE = {
    "age": "四十至五十岁的成年男性统帅",
    "gender": "男",
    "role": "魏武 · 行军途中的统帅",
    "era": "东汉末年",
    "face": "方正硬朗的脸型，颧骨与下颌线清晰，沿用曹操既有的浓眉与整齐长须",
    "eyes": "深褐色细长眼，目光沉鸷镇定，不怒自威",
    "skin": "久经风沙日晒的偏深麦褐肤色",
    "build": "肩背宽厚、体格结实的成年男性，写实成人头身比 1:7（头高约为全身七分之一）",
    "hair": "乌发整齐束起，戴玄色武弁（军中便冠，非朝会高冠），颌下蓄整齐长须",
    "marks": "浓眉、整齐长须；无高冠、无龙纹、无毛皮大氅、无拖地宽袍",
    "outfit": "玄黑窄袖交领战袍，外罩深褐青铜色札甲胸甲与护肩，袍摆及膝便于行军，"
              "腰束宽革带，下穿深灰长裤与黑色行军皮靴；无宽袖、无长袍、无毛皮领、"
              "无蟒龙纹，六格衣装不变",
    "accessories": "玄色武弁、宽革带、腰侧一柄带鞘环首短刀、一只卷起的军用地图筒",
    "weapon": "腰侧带鞘环首短刀，全篇不拔出、不挥舞",
    "palette": "玄黑、深褐、青铜、暗朱、墨灰",
    "temperament": "临困审势、镇定机敏、沉毅决断",
    "art_style": "premium Three Kingdoms historical-fantasy game character concept art, "
                 "highly detailed painterly digital illustration, fine brushwork on "
                 "lamellar armour and coarse campaign cloth, muted historical pigments",
    "negative": "court robe, ceremonial tall crown, dragon pattern, fur cape, wide flowing "
                "sleeves, long trailing gown, civilian scholar outfit, feather, feminine "
                "face, small head, elongated nine-head-tall proportions, chibi, photograph, "
                "3D render, plastic, modern clothing, anime, text, watermark, cropped body, "
                "extra characters, different outfit",
}

POSE = ("standing firmly on a dusty marching road in full campaign armour, dark "
        "narrow-sleeved war robe with bronze lamellar cuirass and black military cap, "
        "one hand gesturing calmly toward the road ahead, no ceremonial robe, no fur "
        "cape, normal 1:7 adult head-to-body proportion")

COLUMN = ("a clearly visible marching column of 5 to 8 additional infantrymen in the "
          "middle ground and background, all wearing the same ochre tunic, plain "
          "lamellar vest and brown headcloth as the recurring infantryman, walking in "
          "a loose single file along the dusty road; they are smaller and less detailed "
          "than the foreground figures but must read as a real troop column, not vague "
          "silhouettes")
ARMOUR = ("Cao Cao wears full CAMPAIGN ARMOUR: a dark narrow-sleeved war robe, "
          "a bronze lamellar cuirass, a black military cap and marching boots. "
          "He never wears a ceremonial court robe, dragon-patterned gown, tall "
          "crown or fur cape.")
TAIL = ("Premium Three Kingdoms historical-fantasy game concept art, highly detailed "
        "painterly digital illustration, historical pigment palette, fine armour and "
        "textile texture, even lighting; no text, no letters, no captions, no watermark, "
        "no panel borders, no split lines")

PANELS = {
    1: f"Wide view using scene-01. {ARMOUR} In the foreground he pauses on the dusty "
       f"road and turns toward the concealed bend, while the recurring infantryman "
       f"trudges behind him with lowered shoulders and a visibly slack waterskin. "
       f"Behind them show {COLUMN}. Emphasize the dry road, the troop column and the "
       f"concealed bend, not extreme summer heat. {TAIL}",
    2: f"Close view using scene-01. Only the recurring infantryman is in sharp focus: he "
       f"holds his limp uncorked waterskin tilted downward with no drops emerging, dry "
       f"lips slightly parted and tired eyes lowered. Keep his face, tied hair, brown "
       f"headcloth, ochre tunic, plain lamellar vest and all accessories identical to "
       f"his character card. Behind him, softly out of focus, {COLUMN}. Dusty road and "
       f"sparse dry grass remain visible. {TAIL}",
    3: f"Medium view using scene-01. {ARMOUR} He speaks calmly and makes a small "
       f"open-handed gesture toward the concealed road bend, without a whip or any "
       f"newly introduced prop; the recurring infantryman raises his head attentively. "
       f"Behind them, {COLUMN}; several of them turn to listen. Show no actual plum "
       f"trees, fruit, imaginary orchard or thought bubbles. {TAIL}",
    4: f"Close portrait using scene-01. Only the recurring infantryman is in sharp focus: "
       f"his eyes regain focus, lips gently close and his throat makes a subtle "
       f"swallowing motion as the thought of sour plums stimulates saliva. Convey the "
       f"change through restrained facial acting, not exaggerated drooling. Keep the same "
       f"face, hair, headcloth, clothing and armor. Behind him, softly out of focus, "
       f"{COLUMN}. No visible plums, orchard, fantasy overlay or thought bubbles. {TAIL}",
    5: f"Wide view using scene-01. {ARMOUR} He walks toward the road bend and the "
       f"recurring infantryman follows with a steadier, more upright stride, his "
       f"empty waterskin secured at its established position on his belt. Behind them, "
       f"{COLUMN}, their posture straighter than in the opening panel. Use body "
       f"posture to contrast with the exhausted opening. No animals, plum trees or "
       f"visible water. {TAIL}",
    6: f"Medium view using scene-02. {ARMOUR} The recurring infantryman kneels safely "
       f"beside the shallow spring and drinks from cupped hands, visibly relieved; "
       f"Cao Cao stands nearby with a composed expression. Behind them, {COLUMN}; "
       f"several soldiers reach the water and fill their waterskins. Keep the clear "
       f"flowing water prominent as the actual resolution of thirst. No plum grove, "
       f"animals or added props. {TAIL}",
}

VISUAL_RULES = (
    "Use premium Three Kingdoms historical-fantasy game concept art, highly detailed "
    "painterly digital illustration, historical pigment palette, fine armour and "
    "textile texture, even lighting. In this episode Cao Cao wears full CAMPAIGN ARMOUR "
    "exactly as defined by his dedicated character card: a dark narrow-sleeved war robe "
    "with bronze lamellar cuirass, black military cap and marching boots - never a "
    "ceremonial court robe, dragon-patterned gown, tall crown or fur cape. Keep every "
    "character's face, hairstyle, clothing, accessories and weapons consistent "
    "throughout. Every story panel MUST show a visible MARCHING COLUMN: 5 to 8 "
    "additional infantrymen in the middle ground or background, all in the same ochre "
    "tunic, plain lamellar vest and brown headcloth as the recurring infantryman, "
    "walking in a loose single file along the road. They may be smaller, softer and less "
    "detailed than the foreground figures, but must be clearly recognisable as a troop "
    "column rather than empty road or vague silhouettes. The two environment concept "
    "plates themselves stay empty of people; the column belongs inside the story panels. "
    "Use only scene-01 and scene-02 for all panels. Never depict an actual plum grove, "
    "imagined orchard, thought bubble, whip gesture or a specifically identified "
    "campaign. Render each panel as one uninterrupted image. Dialogue and narration "
    "exist only in metadata, never as visible text."
)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def run(cmd: list[str], what: str) -> None:
    log("执行：" + " ".join(cmd[1:]))
    rc = subprocess.run(cmd, cwd=str(ROOT)).returncode
    if rc:
        raise SystemExit(f"{what} 失败 rc={rc}")
    log(f"✓ {what} 完成")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    d = json.loads(PLAN.read_text(encoding="utf-8"))
    bid = d["id"]
    log(f"剧本 {bid} · {d['title']}")

    # 1. dedicated marching Cao Cao, leaving the global court-robe card untouched
    chars = d["characters"]
    old = next(c for c in chars if c["name"] == OLD_NAME)
    if any(c["name"] == NEW_NAME for c in chars):
        log(f"已改过，{NEW_NAME} 已在剧本中")
    else:
        old["name"] = NEW_NAME
        old["slug"] = "cao-cao-marching"
        old["existing"] = False
        old.pop("character_id", None)
        old["role"] = "行军统帅，史料称魏武（本集戎装）"
        old["profile"] = dict(PROFILE)
        old["pose"] = POSE
    # 2. global rules and per-panel descriptions
    d["visual_rules"] = VISUAL_RULES
    for sc in d["scenes"]:
        extra = str(sc.get("prompt_extra") or "")
        extra = extra.replace("no people, no animals",
                              "environment plate only, no foreground figures; "
                              "the marching column is added in the story panels")
        sc["prompt_extra"] = extra
    for p in d["panels"]:
        p["characters"] = [NEW_NAME if n == OLD_NAME else n
                           for n in p.get("characters") or []]
        p["visual"] = PANELS[p["no"]]
    log(f"人物 {[c['name'] for c in chars]} · 6 格描述已改写")

    cfg = config.load()
    panels = [r for r in store.iter_all(cfg)
              if r.get("batch_id") == bid and r.get("asset_kind") == "comic-panel"]
    log(f"待归档旧分镜 {len(panels)} 张")
    if args.dry_run:
        print("\n--- visual_rules ---\n" + VISUAL_RULES)
        print("\n--- panel 1 ---\n" + PANELS[1])
        log("dry-run 结束，未改动数据")
        return

    PLAN.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    dest = (cfg["local"] / "raw" / "_superseded"
            / f"wangmei-{datetime.now():%Y%m%d-%H%M%S}")
    dest.mkdir(parents=True, exist_ok=True)
    for r in panels:
        src = cfg["local"] / str(r.get("file") or "")
        if src.is_file():
            shutil.move(str(src), str(dest / Path(str(r["file"])).name))
        with store.connect(cfg) as con:
            con.execute("DELETE FROM asset_tags WHERE asset_id=?", (r["id"],))
            con.execute("DELETE FROM assets WHERE id=?", (r["id"],))
    log(f"已归档 {len(panels)} 张 → {dest}")

    py = [sys.executable, "-u"]
    # No --cards-ready: generate_comic.py creates the new card and its portrait.
    run(py + ["tools/generate_comic.py", "--plan", PLAN_REL], "重做望梅止渴")
    run(py + ["tools/generate_comic_series.py", "--series", SERIES,
              "--index-only"], "重建 q01 系列索引")

    rows = [r for r in store.iter_all(cfg)
            if r.get("batch_id") == bid and r.get("asset_kind") == "comic-panel"]
    bad = [r for r in rows if not r.get("reference_used") or r.get("degraded")]
    log(f"验收：{len(rows)} 格 · 参考图完整 {len(rows)-len(bad)} · 降级 {len(bad)}")
    if len(rows) != 6 or bad:
        raise SystemExit("验收未通过")
    log("全部完成")


if __name__ == "__main__":
    main()
