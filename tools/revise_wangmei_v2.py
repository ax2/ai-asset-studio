#!/usr/bin/env python3
"""望梅止渴 v2: turn the small patrol into a proper Han-era expedition column.

User feedback: "曹操需要骑马，还需要有一些骑兵，包括副将，然后是步兵，还需要有举旗的，
总之就是需要看上去像是古代正常的出征".

Changes vs v1:
  * Cao Cao is mounted on a warhorse in every panel (his existing campaign
    armour card stays unchanged; only the panel text asks for a horse).
  * A new dedicated '副将' character card + portrait: a cavalry officer in
    iron-grey scale armour and a short crimson cloak, riding beside Cao Cao.
  * Cavalry escort, an infantry column and 2-3 standard bearers with plain
    wordless banners are required in every panel (soft/out-of-focus in the
    two close-ups).
  * Both environment plates are widened to a real campaign road / spring
    clearing and regenerated; the plates themselves stay empty of figures.
  * Superseded panels and scenes are moved to _superseded/ (never deleted);
  * the two existing portraits are kept and reused.

    python tools/revise_wangmei_v2.py --dry-run
    python tools/revise_wangmei_v2.py
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
DEPUTY = "副将"

DEPUTY_PROFILE = {
    "age": "三十至四十岁的成年男性将领",
    "gender": "男",
    "role": "曹操麾下骑兵副将，随军护卫并传令",
    "era": "东汉末年",
    "face": "方正偏长的脸型，颧骨清晰，下颌有力，蓄整齐短须",
    "eyes": "深褐色眼睛，目光警觉锐利",
    "skin": "常年日晒的麦褐肤色",
    "build": "肩宽背厚、体格壮实的成年男性，写实成人头身比 1:7",
    "hair": "乌发束起，戴玄铁武胄（军中胄，非朝会冠），胄下露出整齐鬓发",
    "marks": "短须整齐，左眉尾一道浅疤；无羽饰、无龙纹、无高冠",
    "outfit": "深青灰窄袖战袍，外罩铁灰鱼鳞甲，肩覆兽首护肩，腰束黑褐革带，"
              "肩系赭红短披风（及腰、不拖地），下穿深灰长裤与黑皮行军靴；"
              "六格衣装不变",
    "accessories": "玄铁武胄、黑褐革带、赭红短披风、腰侧带鞘环首刀",
    "weapon": "腰侧带鞘环首刀，全篇不拔出、不挥舞",
    "palette": "深青灰、铁灰、赭红、黑褐、墨黑",
    "temperament": "恭谨果毅，随时留意主将号令",
    "art_style": "premium Three Kingdoms historical-fantasy game character concept art, "
                 "highly detailed painterly digital illustration, fine brushwork on "
                 "iron scale armour and coarse campaign cloth, muted historical pigments",
    "negative": "court robe, ceremonial tall crown, dragon pattern, feather plume, "
                "fur cape, long trailing cape, wide flowing sleeves, feminine face, "
                "small head, elongated nine-head-tall proportions, chibi, photograph, "
                "3D render, plastic, modern clothing, anime, text, watermark, cropped "
                "body, extra characters, different outfit",
}
DEPUTY_POSE = ("standing at attention in full cavalry-officer armour with a short "
               "crimson cloak and an iron helmet, one hand resting on the hilt of his "
               "sheathed sabre, no horse in this portrait sheet, normal 1:7 adult "
               "head-to-body proportion")

# Character anchors describe the DESIGN only; their single-figure composition
# note belongs to portrait sheets, never to the story panels.
CAST = ("Cao Cao is MOUNTED on a dark chestnut warhorse in full campaign armour "
        "(dark narrow-sleeved war robe, bronze lamellar cuirass, black military cap, "
        "marching boots), never on foot and never in a ceremonial court robe, "
        "dragon-patterned gown, tall crown or fur cape. His DEPUTY, the dedicated "
        "'副将' character card, rides a brown horse half a length behind and to his "
        "side in iron-grey scale armour, an iron helmet and a short crimson cloak. "
        "Around them ride 4 to 6 armoured CAVALRY troopers as an escort. Behind the "
        "horse line march 10 to 16 INFANTRYMEN in two loose files, all in the same "
        "ochre tunic, plain lamellar vest and brown headcloth as the recurring "
        "infantryman. 2 or 3 STANDARD BEARERS carry tall plain dark-red and black "
        "command banners with cloth streamers - the banners carry NO writing, NO "
        "characters, NO emblems and NO symbols of any kind, only solid cloth. "
        "Add a low haze of road dust so the whole reads as a normal late Han "
        "expedition column on the move, not a small patrol or a crowd of peasants.")

VISUAL_RULES = (
    "Use premium Three Kingdoms historical-fantasy game concept art, highly detailed "
    "painterly digital illustration, historical pigment palette, fine armour and "
    "textile texture, even lighting. THE RULES BELOW APPLY TO THE SIX STORY PANELS "
    "ONLY. The character anchors describe each person's DESIGN (face, hair, armour, "
    "colours); their 'single full-body character' composition note applies only to "
    "portrait sheets and must NOT limit the number of figures in a story panel. "
    + CAST + " "
    "Every one of the six story panels must read as the SAME ongoing expedition: "
    "the two close-up panels may push the column softly out of focus behind the "
    "featured infantryman, but mounted Cao Cao, the deputy, cavalry, a banner or two "
    "and the infantry files must still be discernible in the background. Keep every "
    "named character's face, armour, accessories and weapons consistent throughout. "
    "Use only scene-01 and scene-02 for all "
    "panels. Never depict an actual plum grove, imagined orchard, thought bubble, "
    "whip gesture or a specifically identified campaign. All human hands must be "
    "anatomically correct: exactly five distinct fingers, a natural thumb, wrists "
    "aligned with the forearms, no mirrored or reversed hands, no fused, missing or "
    "extra fingers. Render each panel as one uninterrupted image. Dialogue and "
    "narration exist only in metadata, never as visible text. "
    "no text, no letters, no captions, no watermark, no panel borders, no split lines"
)

# Scene plates get their OWN rule block. scenes.build_prompt() injects
# visual_rules into the environment plate, while scenes.panel_prompt() injects
# scene["prompt_extra"] into every panel's CONTINUITY line. Keeping both channels
# clean stops "empty plate" wording from suppressing the column in the panels,
# and stops the column wording from putting troops on the empty plates.
SCENE_VISUAL_RULES = (
    "THIS IS A STANDALONE EMPTY ENVIRONMENT CONCEPT PLATE. Depict only the "
    "landscape itself: the broad campaign road, low hills, sparse woodland, "
    "dust, stones, dry grass and water. There must be NO people, NO human "
    "silhouettes, NO hands, NO horses, NO riders, NO banners, NO flags, NO "
    "weapons, NO armour and NO expedition column anywhere in this image; the "
    "troops are added later, only inside the six story panels. "
    "Premium Three Kingdoms historical-fantasy game concept art, highly detailed "
    "painterly digital illustration, historical pigment palette, fine natural "
    "texture, soft even daylight, one uninterrupted wide landscape view; "
    "no text, no letters, no captions, no watermark, no panel borders, no split lines"
)

SCENES = {
    "scene-01": {
        "name": "出征官道",
        "description": "无人环境概念图。宽阔夯土官道穿过缓坡疏林，路面有两道车辙与浮尘，"
                       "宽到足以让骑兵与步兵数路并进；远处弯道隐去前路，路边枯草、碎石与"
                       "稀疏灌木，看不到水源或梅林。",
        "prompt_extra": "Independent environment concept art, a WIDE late Eastern Han "
                        "campaign road through low rolling hills and sparse woodland, "
                        "broad packed-earth surface with two wagon ruts and drifting "
                        "dust, wide enough for several files of cavalry and infantry to "
                        "advance together, dry grass, scattered stones and sparse "
                        "shrubs, a bend concealing the route ahead, no plum grove, no "
                        "visible water; soft even daylight; premium Three "
                        "Kingdoms historical-fantasy game concept art, highly detailed "
                        "painterly digital illustration",
    },
    "scene-02": {
        "name": "山脚清泉",
        "description": "无人环境概念图。官道尽头低洼石坡间涌出清泉，浅水汇入小溪，岸前有"
                       "较开阔的空地可供部队驻足取水；岸边湿石、苔痕与疏草，无建筑、无梅树。",
        "prompt_extra": "Independent environment concept art, an EMPTY natural spring "
                        "at the foot of a low rocky hill with a BROAD open gravelled "
                        "area in front where a column of troops could halt and drink, "
                        "clear shallow water gathering between weathered stones and "
                        "flowing into a narrow stream, damp rocks, subtle moss and "
                        "sparse grass, a wide earth road entering from the left, no "
                        "plum trees, no buildings; soft even "
                        "daylight; premium Three Kingdoms historical-fantasy game "
                        "concept art, highly detailed painterly digital illustration",
    },
}

TAIL = ("Premium Three Kingdoms historical-fantasy game concept art, highly detailed "
        "painterly digital illustration, historical pigment palette, fine armour and "
        "textile texture, even lighting; no text, no letters, no captions, no watermark, "
        "no panel borders, no split lines")

PANELS = {
    1: ("Wide establishing view using scene-01, a full Han expedition column advancing "
        "left to right along the broad dusty campaign road. " + CAST + " In the "
        "foreground the recurring infantryman trudges with lowered shoulders and a "
        "visibly slack waterskin; several infantrymen further back look parched and "
        "drooping. The banners hang heavy in still air. Emphasize the dry road, the "
        "ordered ranks of horse and foot, and the concealed bend ahead. " + TAIL),
    2: ("Close view using scene-01. Only the recurring infantryman is in sharp focus: "
        "he holds his limp uncorked waterskin tilted downward with no drops emerging, "
        "dry lips slightly parted and tired eyes lowered. Keep his face, tied hair, "
        "brown headcloth, ochre tunic, plain lamellar vest and all accessories "
        "identical to his character card. Behind him, softly out of focus, the same "
        "expedition continues: a mounted officer silhouette, two tall wordless dark-red "
        "banners, cavalry horses and a blurred file of infantry. " + TAIL),
    3: ("Medium view using scene-01. Mounted Cao Cao speaks calmly and makes a small "
        "open-handed gesture toward the concealed road bend, without a whip or any "
        "newly introduced prop; his deputy reins in beside him and turns to listen. "
        + CAST + " Several cavalry troopers halt behind them and the leading "
        "infantrymen raise their heads attentively; the banners tilt slightly as the "
        "column slows. Show no actual plum trees, fruit, imaginary orchard or thought "
        "bubbles. " + TAIL),
    4: ("Close portrait using scene-01. Only the recurring infantryman is in sharp "
        "focus: his eyes regain focus, lips gently close and his throat makes a "
        "subtle swallowing motion as the thought of sour plums stimulates saliva. "
        "Convey the change through restrained facial acting, not exaggerated "
        "drooling. Keep the same face, hair, headcloth, clothing and armor. Behind "
        "him, softly out of focus, the expedition is still visible: banner poles, "
        "a horse and rider, and the dusty file of infantry. No visible plums, "
        "orchard, fantasy overlay or thought bubbles. " + TAIL),
    5: ("Wide view using scene-01. The whole expedition resumes with renewed order: "
        "mounted Cao Cao and his deputy lead at a steady walk, the cavalry escort "
        "closes up, the infantry files lengthen their stride, and the standard "
        "bearers lift the wordless dark-red and black banners so they lean forward "
        "with the march. " + CAST + " The recurring infantryman in the foreground "
        "walks upright again, his empty waterskin secured at his belt. Use body "
        "posture to contrast with the exhausted opening panel. No animals other "
        "than the cavalry horses, no plum trees, no visible water. " + TAIL),
    6: ("Medium-wide view using scene-02. The expedition has reached the spring and "
        "halts in good order. Mounted Cao Cao sits composed near the water with his "
        "deputy beside him; several cavalry troopers have dismounted to water their "
        "horses, infantrymen kneel at the shallow stream to drink from cupped hands "
        "and refill waterskins, and a standard bearer plants his wordless banner "
        "at an angle on the bank. The recurring infantryman kneels in the foreground "
        "drinking, visibly relieved. " + CAST + " Keep the clear flowing water "
        "prominent as the actual resolution of thirst. No plum grove, no writing on "
        "banners, no added props. " + TAIL),
}


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
    log(f"剧本 {bid} · {d['title']} · 出征阵列版")

    # 1. add the deputy card spec; keep the two existing specs byte-identical so
    #    their anchors and portraits stay valid.
    if not any(c["name"] == DEPUTY for c in d["characters"]):
        d["characters"].append({
            "name": DEPUTY, "slug": "fu-jiang", "existing": False,
            "role": "曹操麾下骑兵副将，随军护卫并传令",
            "profile": dict(DEPUTY_PROFILE), "pose": DEPUTY_POSE,
        })
    # 2. global rules, environment plates and all six panels
    d["visual_rules"] = VISUAL_RULES
    d["scene_visual_rules"] = SCENE_VISUAL_RULES
    for sc in d["scenes"]:
        sc.update(SCENES[sc["id"]])
    for p in d["panels"]:
        p["visual"] = PANELS[p["no"]]
        p["characters"] = (["行军士卒"] if p["no"] in (2, 4)
                           else ["曹操·行军", DEPUTY, "行军士卒"])
    log(f"人物 {[c['name'] for c in d['characters']]} · 6 格 · 2 场景板已改写")

    cfg = config.load()
    drop = [r for r in store.iter_all(cfg)
            if r.get("batch_id") == bid
            and r.get("asset_kind") in ("comic-panel", "scene")]
    log(f"待归档：{sum(1 for r in drop if r.get('asset_kind')=='comic-panel')} 格分镜 + "
        f"{sum(1 for r in drop if r.get('asset_kind')=='scene')} 张场景板")
    if args.dry_run:
        print("\n--- visual_rules ---\n" + VISUAL_RULES)
        print("\n--- panel 1 ---\n" + PANELS[1])
        log("dry-run 结束，未改动数据")
        return

    PLAN.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    dest = (cfg["local"] / "raw" / "_superseded"
            / f"wangmei-expedition-{datetime.now():%Y%m%d-%H%M%S}")
    dest.mkdir(parents=True, exist_ok=True)
    for r in drop:
        src = cfg["local"] / str(r.get("file") or "")
        if src.is_file():
            shutil.move(str(src), str(dest / Path(str(r["file"])).name))
        with store.connect(cfg) as con:
            con.execute("DELETE FROM asset_tags WHERE asset_id=?", (r["id"],))
            con.execute("DELETE FROM assets WHERE id=?", (r["id"],))
    log(f"已归档 {len(drop)} 张 → {dest}（两张已有立绘保留复用）")

    py = [sys.executable, "-u"]
    # No --cards-ready: generate_comic.py creates the deputy card + portrait,
    # regenerates the two scenes and all six panels; existing portraits are reused.
    run(py + ["tools/generate_comic.py", "--plan", PLAN_REL], "重做望梅止渴（出征版）")
    run(py + ["tools/generate_comic_series.py", "--series", SERIES,
              "--index-only"], "重建 q01 系列索引")

    rows = [r for r in store.iter_all(cfg) if r.get("batch_id") == bid]
    panels = [r for r in rows if r.get("asset_kind") == "comic-panel"]
    bad = [r for r in panels if not r.get("reference_used") or r.get("degraded")]
    log(f"验收：{len(panels)} 格 · 参考图完整 {len(panels)-len(bad)} · 降级 {len(bad)} · "
        f"场景 {sum(1 for r in rows if r.get('asset_kind')=='scene')} · "
        f"立绘 {sum(1 for r in rows if r.get('asset_kind')=='character-portrait')}")
    if len(panels) != 6 or bad:
        raise SystemExit("验收未通过")
    log("全部完成")


if __name__ == "__main__":
    main()
