#!/usr/bin/env python3
"""Generate a complete, reproducible comic batch.

    python tools/generate_comic.py --list          # plan + estimated cost
    python tools/generate_comic.py                # full batch (idempotent)
    python tools/generate_comic.py --export-only   # re-pack, no API calls

Pipeline: script (written by GPT-6.1 Sol) -> scene cards + character cards ->
new character portraits -> standalone scene concept art -> six comic panels ->
ZIP with images, script, cards, style bible, recipe and a readable comic.html.

Every generated PNG is indexed with its exact prompt, model, quality, size,
cost, character_id / anchor_version / scene_id / panel_no and source metadata.
Re-running skips anything already generated, so an interrupted batch never
pays for the same image twice.
"""
from __future__ import annotations

import argparse
import glob
import html
import json
import shutil
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio import (characters, config, exporter, imaging, pricing,  # noqa: E402
                    scenes, store, thumbs)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_plan(path: str | None) -> tuple[dict, Path]:
    if path:
        p = Path(path)
        if not p.is_absolute():
            p = ROOT / p
    else:
        found = sorted(glob.glob(str(ROOT / "batches" / "comic-*.json")))
        if not found:
            raise SystemExit("没有找到连环画剧本，请先运行 python tools/write_comic.py")
        p = Path(found[-1])
    return json.loads(p.read_text(encoding="utf-8")), p


# ---------------------------------------------------------------- asset writer

def write_png(cfg: dict, data: bytes, *, label: str, asset_kind: str,
              prompt: str, model: str, quality: str, size: str,
              source: str, tags: list[str], note: str, batch_id: str,
              character_id: str = "", anchor_version: int = 0,
              scene_id: str = "", panel_no: int = 0,
              extra: dict | None = None) -> dict:
    """Write one PNG and index it with the full reproducible field set."""
    aid = store.new_id()
    day = store.now()[:10].replace("-", "")
    folder = cfg["local"] / "raw" / day
    folder.mkdir(parents=True, exist_ok=True)
    filename = aid + ".png"
    path = folder / filename
    path.write_bytes(data)
    try:
        thumbs.ensure(cfg, f"raw/{day}/{filename}")
    except Exception:
        pass
    rec = {
        "id": aid, "file": f"raw/{day}/{filename}", "filename": filename,
        "label": label, "asset_kind": asset_kind,
        "style_id": "sanguo-comic", "style_name": "三国连环画 · 分镜与场景",
        "prompt": prompt, "model": model, "quality": quality, "size": size,
        "cost_usd": pricing.image_price(model, quality), "demo": False,
        "source": source, "parent_id": "", "character_id": character_id,
        "anchor_version": anchor_version, "scene_id": scene_id,
        "panel_no": panel_no, "batch_id": batch_id,
        "rating": 0, "tags": tags, "favorite": False, "status": "keeper",
        "note": note, "metadata": imaging.check_metadata(path),
        "created_at": store.now(),
    }
    if extra:
        rec.update(extra)
    store.add(cfg, rec)
    return rec


def find(cfg: dict, batch_id: str, **match) -> dict | None:
    for r in store.iter_all(cfg):
        if r.get("batch_id") != batch_id or r.get("demo"):
            continue
        if all(r.get(k) == v for k, v in match.items()):
            return r
    return None


# ---------------------------------------------------------------- character cards

def ensure_cards(cfg: dict, plan: dict) -> dict[str, dict]:
    out = {}
    for spec in plan["characters"]:
        name = spec["name"]
        if spec.get("existing") and spec.get("character_id"):
            card = characters.get_card(cfg, spec["character_id"])
            if not card:
                raise ValueError("已有角色卡不存在：" + spec["character_id"])
            log(f"角色 {name}: 复用已有卡 {card['id']} · 锚点 v{card['anchor_version']}")
        else:
            old = next((c for c in characters.list_cards(cfg)
                        if c["name"] == name), None)
            card = characters.blank_card(name)
            card["profile"] = dict(spec["profile"])
            card["aliases"] = [spec.get("slug") or name]
            card["source"] = plan["story_source"][:200]
            card["mode"] = "comic-curated"
            card["evidence"] = {"story_source": plan["story_source"][:200]}
            card["confidence"] = {"source_of_truth": "comic-script"}
            if old:
                card["id"] = old["id"]
                card["anchor"] = old.get("anchor") or ""
                card["ref_asset_id"] = old.get("ref_asset_id") or ""
            card = characters.put_card(cfg, card)
            log(f"角色 {name}: 新建卡 {card['id']} · 锚点 v{card['anchor_version']}")
        out[name] = card
    return out


def reference_bytes(cfg: dict, asset: dict | None) -> bytes | None:
    if not asset:
        return None
    p = cfg["local"] / str(asset.get("file") or "")
    if p.is_file() and p.suffix.lower() == ".png":
        return p.read_bytes()
    return None


def portrait(cfg: dict, plan: dict, spec: dict, card: dict) -> dict:
    base = imaging.build_prompt(
        imaging.load_style(cfg, plan["style_id"]),
        extra=", ".join(x for x in (
            str(spec.get("pose") or ""),
            "SINGLE CHARACTER PORTRAIT: exactly one full-body character, centered, "
            "orthographic, plain uniform background, evenly lit",
            "no text, no letters, no captions, no watermark, no border, "
            "no panel, no extra characters",
        ) if x.strip()))
    # Verbatim anchor first, matching the other reproduction paths.
    prompt = ("CHARACTER ANCHOR - keep this block verbatim:\n"
              + str(card.get("anchor") or "") + "\n\nSCENE AND STYLE:\n" + base)
    data, _ext = imaging.call_openai(
        cfg, prompt, plan["image_model"], plan["quality"], plan["portrait_size"])
    rec = write_png(
        cfg, data, label=f"{card['name']} · 连环画角色立绘",
        asset_kind="character-portrait", prompt=prompt,
        model=plan["image_model"], quality=plan["quality"],
        size=plan["portrait_size"], source="comic-portrait",
        tags=[card["name"], "character-portrait", "连环画", plan["title"]],
        note="连环画新人物立绘；prompt 内含角色锚点，可作为后续分镜的参考图。",
        batch_id=plan["id"], character_id=card["id"],
        anchor_version=int(card.get("anchor_version") or 1))
    fresh = characters.get_card(cfg, card["id"])
    fresh["ref_asset_id"] = rec["id"]
    characters.put_card(cfg, fresh)
    return rec


# ---------------------------------------------------------------- scenes

def scene_asset(cfg: dict, plan: dict, scene: dict) -> dict:
    prompt = scenes.build_prompt(cfg, scene, style_id=plan["style_id"],
                                 visual_rules=plan["visual_rules"])
    data, _ext = imaging.call_openai(
        cfg, prompt, plan["image_model"], plan["quality"], plan["scene_size"])
    rec = write_png(
        cfg, data, label=f"{scene['name']} · 场景概念图",
        asset_kind="scene", prompt=prompt,
        model=plan["image_model"], quality=plan["quality"],
        size=plan["scene_size"], source="comic-scene",
        tags=[scene["name"], "scene", "场景", "连环画", plan["title"]],
        note="无人物场景概念图，可复用于其它分镜；画面不含任何文字。",
        batch_id=plan["id"], scene_id=scene["id"])
    card = dict(scene)
    card["scene_key"] = scene["id"]
    card["id"] = f"{plan['id']}--{scene['id']}"
    card.update({"style_id": plan["style_id"], "visual_rules": plan["visual_rules"],
                 "prompt": prompt, "model": plan["image_model"],
                 "quality": plan["quality"], "size": plan["scene_size"],
                 "asset_id": rec["id"], "batch_id": plan["id"]})
    scenes.put(cfg, card)
    return rec


# ---------------------------------------------------------------- panels

def panel_asset(cfg: dict, plan: dict, panel: dict, scene: dict,
                cards: dict[str, dict], portraits: dict[str, dict]) -> dict:
    anchors = []
    refs = []
    for name in panel["characters"]:
        card = cards[name]
        anchors.append(str(card.get("anchor") or ""))
        asset = portraits.get(name)
        if asset is None and card.get("ref_asset_id"):
            asset = store.get(cfg, card["ref_asset_id"])
        raw = reference_bytes(cfg, asset)
        if raw:
            refs.append(raw)
    prompt = scenes.panel_prompt(cfg, panel, scene, style_id=plan["style_id"],
                                 visual_rules=plan["visual_rules"],
                                 anchors=anchors)
    used_edit, degraded = False, ""
    if refs:
        try:
            data = imaging.call_openai_edit(
                cfg, prompt, refs, model=plan["image_model"],
                quality=plan["quality"], size=plan["panel_size"])
            used_edit = True
        except (RuntimeError, OSError, ValueError) as exc:
            degraded = f"参考图接口不可用（{type(exc).__name__}），降级为纯文本生成"
            data, _ext = imaging.call_openai(
                cfg, prompt, plan["image_model"], plan["quality"], plan["panel_size"])
    else:
        data, _ext = imaging.call_openai(
            cfg, prompt, plan["image_model"], plan["quality"], plan["panel_size"])
    rec = write_png(
        cfg, data, label=f"第{panel['no']}格 · {panel['caption']}",
        asset_kind="comic-panel", prompt=prompt,
        model=plan["image_model"], quality=plan["quality"],
        size=plan["panel_size"], source="comic-panel",
        tags=[plan["title"], "comic-panel", f"第{panel['no']}格"] + panel["characters"],
        note=f"第 {panel['no']} 格分镜；" +
             ("参考图 /images/edits" if used_edit else "纯文本生成") +
             (f"；{degraded}" if degraded else ""),
        batch_id=plan["id"], scene_id=panel["scene_id"], panel_no=panel["no"],
        extra={"comic_caption": panel["caption"],
               "comic_dialogue": panel.get("dialogue") or [],
               "comic_characters": panel["characters"],
               "composition": panel.get("composition") or "wide",
               "reference_used": used_edit, "degraded": degraded})
    return rec


# ---------------------------------------------------------------- HTML comic

def comic_html(plan: dict, rows: list[dict]) -> str:
    by_panel = {int(r.get("panel_no") or 0): r for r in rows
                if r.get("asset_kind") == "comic-panel"}
    parts = [
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{html.escape(plan['title'])} · 三国连环画</title>",
        "<style>",
        "*{box-sizing:border-box}body{margin:0;background:#efe4cc;color:#2b2118;"
        "font-family:'Noto Serif SC','Songti SC','SimSun',serif}",
        ".wrap{max-width:920px;margin:0 auto;padding:36px 20px 80px}",
        "header{text-align:center;padding:28px 0 34px;border-bottom:2px solid #b08b57}",
        "h1{margin:0;font-size:34px;letter-spacing:.14em}",
        ".sub{margin-top:10px;color:#7a5c3a;font-size:15px;letter-spacing:.06em}",
        ".source{margin:18px auto 0;max-width:700px;font-size:13px;line-height:1.8;"
        "color:#6b563d}",
        ".panel{margin:38px 0;background:#f8f1e2;border:1px solid #d3bd97;"
        "box-shadow:0 6px 22px rgba(80,55,25,.13);border-radius:3px;overflow:hidden}",
        ".panel img{display:block;width:100%;height:auto;background:#e8dcc3}",
        ".no{margin:0;padding:10px 20px;background:#3c2c1c;color:#e9d6ae;"
        "font-size:12px;letter-spacing:.22em}",
        ".text{padding:18px 22px 22px}",
        ".caption{font-size:19px;line-height:1.7;margin:0 0 12px}",
        ".line{margin:7px 0;font-size:15px;line-height:1.65;color:#4b3a28}",
        ".line b{color:#8b3a24;margin-right:8px}",
        "footer{text-align:center;margin-top:50px;font-size:12px;color:#8b7457;"
        "line-height:1.9}",
        "@media print{body{background:#fff}.panel{box-shadow:none;break-inside:avoid}}",
        "</style></head><body><div class='wrap'>",
        "<header>",
        f"<h1>{html.escape(plan['title'])}</h1>",
        f"<div class='sub'>{html.escape(plan.get('subtitle') or '')}</div>",
        f"<div class='source'>{html.escape(plan['story_source'])}</div>",
        "</header>",
    ]
    for p in sorted(plan["panels"], key=lambda x: x["no"]):
        rec = by_panel.get(p["no"])
        src = ("images/" + rec["filename"]) if rec else ""
        parts.append("<section class='panel'>")
        parts.append(f"<div class='no'>第 {p['no']} 格 · {html.escape(p['composition'])}</div>")
        if src:
            parts.append(f"<img src='{html.escape(src)}' alt='第{p['no']}格'>")
        parts.append("<div class='text'>")
        parts.append(f"<p class='caption'>{html.escape(p['caption'])}</p>")
        for d in p.get("dialogue") or []:
            parts.append("<p class='line'><b>%s</b>%s</p>" % (
                html.escape(str(d.get("speaker") or "")),
                html.escape(str(d.get("line") or ""))))
        parts.append("</div></section>")
    parts.append(
        "<footer>画面由 gpt-image-2.6-sunburst 生成，不含任何文字；"
        "旁白与对白由本页排版叠加。<br>剧本由 GPT-6.1 Sol 依据史料创作，"
        "完整参数见 recipe.json / manifest.json。</footer>")
    parts.append("</div></body></html>")
    return "\n".join(parts)


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description="生成完整可复现的三国连环画")
    ap.add_argument("--plan", default="", help="剧本 JSON 路径（默认 batches/comic-*.json 最新）")
    ap.add_argument("--list", action="store_true", help="只打印计划与预估成本")
    ap.add_argument("--force", action="store_true", help="忽略已有资产重新生成（会重复计费）")
    ap.add_argument("--export-only", action="store_true", help="不调用 API，只重新打包")
    ap.add_argument("--skip-panels", action="store_true", help="只生成角色与场景")
    ap.add_argument("--max-cost", type=float, default=0.0, help="覆盖单次成本上限（美元）")
    args = ap.parse_args()

    cfg = config.load()
    plan, plan_path = load_plan(args.plan)
    cfg["edit_model"] = plan["image_model"]
    unit = pricing.image_price(plan["image_model"], plan["quality"])
    new_chars = [c for c in plan["characters"] if not c.get("existing")]
    planned = len(new_chars) + len(plan["scenes"]) + len(plan["panels"])
    cap = args.max_cost or float(plan.get("max_cost_usd") or 8)
    log(f"剧本 {plan['id']} · {plan['title']}")
    log(f"图像 {plan['image_model']} / {plan['quality']} · 文本 {plan.get('text_model')}")
    log(f"新人物 {len(new_chars)} · 场景 {len(plan['scenes'])} · 分镜 {len(plan['panels'])}"
        f" · 预计 {planned} 张 API 图 × ${unit} = ${planned * unit:.3f} · 上限 ${cap}")
    if args.list:
        for c in plan["characters"]:
            print("  人物", c["name"], "已有" if c.get("existing") else "新建")
        for s in plan["scenes"]:
            print("  场景", s["id"], s["name"])
        for p in plan["panels"]:
            print(f"  第{p['no']}格", p["caption"])
        return
    if not config.has_key(cfg) and not args.export_only:
        raise SystemExit("没有可用的 API key。")
    if planned * unit > cap and not args.export_only:
        raise SystemExit(f"预估 ${planned * unit:.3f} 超过上限 ${cap}")

    started = time.time()
    failures: list[str] = []
    created = 0
    cards: dict[str, dict] = {}
    portraits: dict[str, dict] = {}

    if not args.export_only:
        cards = ensure_cards(cfg, plan)

        # 1. New character portraits (existing characters reuse their own art).
        for spec in plan["characters"]:
            name, card = spec["name"], cards[spec["name"]]
            existing = None if args.force else find(
                cfg, plan["id"], asset_kind="character-portrait",
                character_id=card["id"])
            if existing:
                portraits[name] = existing
                log(f"立绘已存在，跳过：{name}")
                continue
            if spec.get("existing") and card.get("ref_asset_id"):
                portraits[name] = store.get(cfg, card["ref_asset_id"]) or {}
                continue
            t0 = time.time()
            log(f"生成新人物立绘：{name} …")
            try:
                portraits[name] = portrait(cfg, plan, spec, card)
                created += 1
                log(f"  完成 {portraits[name]['id']} · {time.time() - t0:.1f}s")
            except Exception as exc:
                failures.append(f"{name} 立绘: {type(exc).__name__}: {exc}")
                log(f"  !! {name} 立绘失败：{type(exc).__name__}: {exc}")

        # 2. Standalone scene concept art.
        for sc in plan["scenes"]:
            existing = None if args.force else find(
                cfg, plan["id"], asset_kind="scene", scene_id=sc["id"])
            if existing:
                log(f"场景已存在，跳过：{sc['name']}")
                continue
            t0 = time.time()
            log(f"生成场景概念图：{sc['name']} …")
            try:
                rec = scene_asset(cfg, plan, sc)
                created += 1
                log(f"  完成 {rec['id']} · {time.time() - t0:.1f}s")
            except Exception as exc:
                failures.append(f"{sc['name']} 场景: {type(exc).__name__}: {exc}")
                log(f"  !! {sc['name']} 场景失败：{type(exc).__name__}: {exc}")

        # 3. Six comic panels.
        if not args.skip_panels:
            scene_map = {s["id"]: s for s in plan["scenes"]}
            for p in plan["panels"]:
                existing = None if args.force else find(
                    cfg, plan["id"], asset_kind="comic-panel", panel_no=p["no"])
                if existing:
                    log(f"第 {p['no']} 格已存在，跳过")
                    continue
                t0 = time.time()
                log(f"生成第 {p['no']}/6 格：{p['caption']} …")
                try:
                    rec = panel_asset(cfg, plan, p, scene_map[p["scene_id"]],
                                      cards, portraits)
                    created += 1
                    log(f"  完成 {rec['id']} · {time.time() - t0:.1f}s · "
                        f"参考图={'是' if rec.get('reference_used') else '否'}")
                except Exception as exc:
                    failures.append(f"第{p['no']}格: {type(exc).__name__}: {exc}")
                    log(f"  !! 第 {p['no']} 格失败：{type(exc).__name__}: {exc}")

    if failures:
        log(f"本轮 {len(failures)} 项失败；重跑本脚本会自动跳过已完成项：")
        for f in failures:
            log("   - " + f)

    # Export every asset of this batch (not only this run's new ones).
    rows = [r for r in store.iter_all(cfg)
            if r.get("batch_id") == plan["id"] and not r.get("demo")]
    if not rows:
        raise SystemExit("没有可导出的资产。")
    rows.sort(key=lambda r: (r.get("asset_kind") or "", r.get("panel_no") or 0,
                             r.get("created_at") or ""))
    style = imaging.load_style(cfg, plan["style_id"])
    recipe = {
        "batch_id": plan["id"], "title": plan["title"],
        "generated_at": store.now(), "script": str(plan_path.relative_to(ROOT)).replace("\\", "/"),
        "text_model": plan.get("text_model"), "image_model": plan["image_model"],
        "quality": plan["quality"], "style_id": plan["style_id"],
        "portrait_size": plan["portrait_size"], "scene_size": plan["scene_size"],
        "panel_size": plan["panel_size"],
        "story_source": plan["story_source"],
        "why_not_in_romance": plan["why_not_in_romance"],
        "pricing_note": "gpt-image-2.6-sunburst 的官方单价未由 /v1/models 公开，"
                        "成本按上一代同档估算值记账，见 studio/pricing.py 的 ESTIMATED_MODELS。",
        "regenerate_command": "python tools/generate_comic.py",
        "reproduce_note": "角色卡 anchor 逐字置于提示词最前，分镜走 /images/edits 并"
                          "以该角色立绘作参考图；场景卡保存了完整 prompt 与环境描述。",
        "asset_count": len(rows),
        "total_cost_usd": round(sum(float(r.get("cost_usd") or 0) for r in rows), 4),
    }
    scene_cards = [s for s in scenes.list_cards(cfg)
                   if s.get("batch_id") == plan["id"]]
    all_cards = {c["name"]: c for c in characters.list_cards(cfg)}
    char_cards = [all_cards[c["name"]] for c in plan["characters"]
                  if c["name"] in all_cards]
    extra = {
        "script.json": plan,
        "recipe.json": recipe,
        "character-cards.json": char_cards,
        "scene-cards.json": scene_cards,
        "style-bible.json": style,
        "comic.html": comic_html(plan, rows),
        "README.txt": (
            f"{plan['title']} · 三国连环画\n"
            "================================\n"
            f"剧本 {plan['id']} · 生成于 {recipe['generated_at']}\n"
            f"文本 {plan.get('text_model')} · 图像 {plan['image_model']} / {plan['quality']}\n"
            f"史料 {plan['story_source'][:120]}\n\n"
            "comic.html        可直接双击阅读的连环画（图片不含文字，旁白/对白由页面叠加）\n"
            "images/           人物立绘、场景概念图、6 格分镜原图，保留来源元数据\n"
            "script.json       完整剧本：6 格旁白、对白、画面描述、场景与人物设定\n"
            "manifest.json     每张图的 prompt / model / quality / size / cost / metadata /\n"
            "                  character_id / anchor_version / scene_id / panel_no\n"
            "character-cards.json  角色卡全量快照，含逐字复用的 anchor 与版本号\n"
            "scene-cards.json      场景卡：环境描述、完整 prompt、模型参数、资产编号\n"
            "style-bible.json      画风配方（分块提示词 + 负面词）\n"
            "recipe.json           本次运行的全部参数与复现命令\n\n"
            "复现\n----\n"
            "python tools/generate_comic.py            # 幂等，已生成图自动跳过\n"
            "python tools/generate_comic.py --export-only  # 只重新打包，不调用 API\n"
            "python tools/write_comic.py              # 用文本模型重写剧本\n\n"
            "注意：画风沿用「三国英雄立绘」；锚点版本变化意味着人物设定改过，"
            "旧图与新图不要混用。图片可能含平台 AIGC 标识，请按发布平台要求标注。\n"),
    }
    out = exporter.export_zip(cfg, rows, plan["id"], extra)
    log(f"导出 {out['count']} 张 → {out['path']}")

    # Unpack to the synced drive and to the local packs dir, so the readable
    # comic is also served at /pack/<batch_id>/comic.html by the workbench.
    root = cfg["asset_root_path"]
    target = root / "all" / plan["id"]
    local_page = cfg["local"] / "packs" / plan["id"]
    for folder in (target, local_page):
        folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out["path"]) as zf:
        zf.extractall(target)
        zf.extractall(local_page)
    log(f"已同步到云盘目录：{target}")
    log(f"阅读页 http://127.0.0.1:8777/pack/{plan['id']}/comic.html")

    health = exporter.health(cfg)
    log(f"元数据检查 {health['checked']} 项，问题 {len(health['issues'])} 处")
    print(json.dumps({
        "ok": True, "batch_id": plan["id"], "title": plan["title"],
        "assets": len(rows),
        "by_kind": {k: sum(1 for r in rows if r.get("asset_kind") == k)
                    for k in ("character-portrait", "scene", "comic-panel")},
        "scene_cards": len(scene_cards), "character_cards": len(char_cards),
        "zip": out["path"], "comic_html": str(target / "comic.html"),
        "cost_usd": recipe["total_cost_usd"],
        "metadata_healthy": health["healthy"],
        "failures": failures, "new_assets_this_run": created,
        "elapsed_s": round(time.time() - started),
    }, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
