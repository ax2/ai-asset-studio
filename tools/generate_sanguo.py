#!/usr/bin/env python3
"""Generate a reproducible set of Three Kingdoms game character art.

    python tools/generate_sanguo.py                 # full batch
    python tools/generate_sanguo.py --only 关羽     # one character (resume)
    python tools/generate_sanguo.py --list          # show the plan and cost

The batch is idempotent: already-generated portraits / packs are detected and
skipped, so an interrupted run can simply be started again without paying for
the same image twice. Recipe, character cards, style bible and every prompt /
model / size / anchor version are written into the exported ZIP.

No API key is read from the command line or stored here: it comes from
config.json / DPAPI / the environment through studio.config.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio import (characters, config, exporter, imaging, pricing,  # noqa: E402
                    store, turnaround)

MANIFEST = ROOT / "batches" / "sanguo-heroes.json"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_plan() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def existing_portrait(cfg: dict, cid: str):
    rows, _ = store.query_page(cfg, character_id=cid, limit=200)
    for r in rows:
        if (r.get("asset_kind") == "character-portrait"
                and r.get("source") == "character-reproduce"
                and not r.get("demo")):
            return r
    return None


def existing_pack(cfg: dict, cid: str, mode: str):
    for p in characters.list_packs(cfg, "turnaround"):
        d = p.get("data") or {}
        if (d.get("character_id") == cid and d.get("mode") == mode
                and not d.get("demo") and d.get("style_id") == "sanguo-hero"):
            return p
    return None


def make_cards(cfg: dict, plan: dict, skip_extract: bool) -> list:
    """Novel excerpt -> LLM traits -> human-curated override -> versioned card."""
    cards = []
    extracted = {}
    mode = "curated"
    if not skip_extract:
        try:
            res = characters.extract(cfg, plan["novel_excerpt"], mode="llm")
            if res.get("characters"):
                extracted = {c["name"]: c for c in res["characters"]}
                mode = "llm+curated"
                log(f"文本模型提取到 {len(extracted)} 个角色：{', '.join(extracted)}")
            else:
                log("文本模型未返回角色，改用人工校订设定。")
        except Exception as exc:
            log(f"文本模型提取失败（{type(exc).__name__}），改用人工校订设定：{exc}")
    else:
        log("按 --skip-extract 跳过文本模型，只用人工校订设定。")

    for spec in plan["characters"]:
        name = spec["name"]
        ex = extracted.get(name) or {}
        card = characters.blank_card(name)
        card["profile"] = dict(spec["profile"])
        card["evidence"] = dict(ex.get("evidence") or {})
        card["confidence"] = dict(ex.get("confidence") or {})
        card["confidence"]["source_of_truth"] = "curated"
        card["source"] = plan["novel_excerpt"][:160]
        card["mode"] = mode
        card["aliases"] = [spec["slug"]]
        # Reuse an existing card so a resumed run keeps the same character_id
        # and anchor version instead of creating duplicates.
        old = next((c for c in characters.list_cards(cfg) if c["name"] == name), None)
        if old:
            card["id"] = old["id"]
            card["ref_asset_id"] = old.get("ref_asset_id") or ""
            card["anchor"] = old.get("anchor") or ""
            # A resumed / offline run must not wipe the original-text evidence.
            if not card["evidence"]:
                card["evidence"] = dict(old.get("evidence") or {})
                card["confidence"] = dict(old.get("confidence") or {})
                card["mode"] = old.get("mode") or mode
                card["source"] = old.get("source") or card["source"]
        saved = characters.put_card(cfg, card)
        cards.append((spec, saved))
        log(f"角色卡 {name}: {saved['id']} · 锚点 v{saved['anchor_version']} · {len(saved['anchor'])} 字符")
    return cards


def portrait(cfg: dict, spec: dict, card: dict, plan: dict,
             fallback_size: str) -> tuple[dict | None, str]:
    size = fallback_size
    extra = spec.get("pose", "")
    label = f"{card['name']} · 英雄立绘"
    kw = dict(character_id=card["id"], extra=extra, dry_run=False,
              model=plan["model"], quality=plan["quality"], size=size,
              style_id=plan["style_id"], label=label)
    try:
        res = turnaround.reproduce(cfg, **kw)
    except RuntimeError as exc:
        # Some gateways reject non-square sizes; retry once on the safe size.
        if size != "1024x1024" and ("400" in str(exc) or "size" in str(exc).lower()):
            log(f"  {size} 被拒绝（{str(exc)[:120]}），改用 1024x1024 重试。")
            size = "1024x1024"
            kw["size"] = size
            res = turnaround.reproduce(cfg, **kw)
        else:
            raise
    rec = res["asset"]
    store.update(cfg, rec["id"], {
        "status": "keeper", "asset_kind": "character-portrait",
        "favorite": True, "tags": list(spec.get("tags") or []) + ["三国", "高细节"],
        "note": ("三国英雄首批立绘。复现：角色卡 anchor 逐字置于提示词最前，"
                 "style_id=sanguo-hero，model=" + plan["model"] +
                 "，quality=" + plan["quality"] + "，size=" + size +
                 "；把本图作为 /images/edits 参考图（input_fidelity=high）可延续风格。"),
    })
    return store.get(cfg, rec["id"]), size


def set_reference(cfg: dict, card: dict, asset_id: str) -> dict:
    """Attach the portrait as the character's style reference (no version bump)."""
    fresh = characters.get_card(cfg, card["id"])
    fresh["ref_asset_id"] = asset_id
    return characters.put_card(cfg, fresh)


def existing_views(cfg: dict, cid: str, style_id: str) -> dict:
    """Views already generated by an interrupted separate-mode pack."""
    rows, _ = store.query_page(cfg, character_id=cid, limit=300)
    out = {}
    for r in rows:
        kind = str(r.get("asset_kind") or "")
        if (r.get("source") == "turnaround-view" and r.get("style_id") == style_id
                and kind.startswith("view-") and not r.get("demo")):
            out.setdefault(kind[5:], r)
    return out


def turnaround_pack(cfg: dict, spec: dict, card: dict, plan: dict, mode: str) -> dict:
    size = plan["sheet_size"] if mode == "sheet" else plan["view_size"]
    kw = {}
    if mode != "sheet":
        reuse = existing_views(cfg, card["id"], plan["style_id"])
        if reuse:
            log(f"  复用上次已生成的 {len(reuse)} 张视图：{'、'.join(reuse)}")
        kw["reuse_views"] = reuse
    return turnaround.build(
        cfg, character_id=card["id"], mode=mode, dry_run=False,
        model=plan["model"], quality=plan["quality"], size=size,
        style_id=plan["style_id"], name=f"{card['name']}-四视图", **kw)


def main() -> None:
    ap = argparse.ArgumentParser(description="三国英雄角色美术资产批量生成")
    ap.add_argument("--only", default="", help="只生成指定角色（可重复调用用于断点续跑）")
    ap.add_argument("--list", action="store_true", help="只打印计划与预估成本")
    ap.add_argument("--force", action="store_true", help="忽略已有资产，重新生成（会重复计费）")
    ap.add_argument("--skip-extract", action="store_true", help="不调用文本模型，直接用人工校订设定")
    ap.add_argument("--skip-turnaround", action="store_true", help="只生成立绘，不做多视角")
    ap.add_argument("--export-only", action="store_true", help="不调用 API，只重新导出素材包")
    ap.add_argument("--max-cost", type=float, default=0.0, help="覆盖清单里的单次成本上限（美元）")
    args = ap.parse_args()

    cfg = config.load()
    plan = load_plan()
    cfg["edit_model"] = plan["model"]      # reference-image edits use the same top model
    unit = pricing.image_price(plan["model"], plan["quality"])
    specs = plan["characters"]
    if args.only:
        wanted = [s for s in specs if s["name"] == args.only]
        if not wanted:
            raise SystemExit(f"清单里没有角色：{args.only}")
        specs = wanted
        plan["characters"] = specs       # make_cards consumes the same filtered plan
    planned = sum(1 + (4 if s.get("turnaround") == "separate" else
                       1 if s.get("turnaround") == "sheet" else 0)
                  for s in specs)
    cap = args.max_cost or float(plan.get("max_cost_usd") or 5)
    log(f"配方 {plan['id']} · {plan['style_id']} · {plan['model']} / {plan['quality']}")
    log(f"角色 {len(specs)} 个 · 预计最多 {planned} 张 API 图 · 单价 ${unit} · 上限 ${cap}")
    if args.list:
        for s in specs:
            print(f"  {s['name']:<4} 立绘 1 张 + 多视图 {s.get('turnaround') or '无'}")
        return
    if not config.has_key(cfg) and not args.export_only:
        raise SystemExit("没有可用的 API key。请先把它写进 config.json 的 api_key 字段。")
    if planned * unit > cap and not args.export_only:
        raise SystemExit(f"预估 ${planned * unit:.4f} 超过上限 ${cap}，已中止。可用 --max-cost 调整。")

    started = time.time()
    created_ids: list[str] = []
    cards = make_cards(cfg, plan, args.skip_extract) if not args.export_only else []
    if args.export_only:
        cards = [(s, next((c for c in characters.list_cards(cfg)
                           if c["name"] == s["name"]), None)) for s in specs]
        cards = [(s, c) for s, c in cards if c]

    portrait_size = plan["portrait_size"]
    failures: list[str] = []
    for idx, (spec, card) in enumerate(cards, 1):
        if not card or args.export_only:
            continue
        log(f"[{idx}/{len(cards)}] === {card['name']} ===")
        try:
            existing = None if args.force else existing_portrait(cfg, card["id"])
            if existing:
                rec = existing
                log(f"  立绘已存在，跳过：{rec['id']}")
            else:
                t0 = time.time()
                log(f"  生成立绘 {portrait_size} / {plan['quality']} …")
                rec, _used_size = portrait(cfg, spec, card, plan, portrait_size)
                log(f"  完成 {rec['id']} · {time.time() - t0:.1f}s · ${rec['cost_usd']}")
                set_reference(cfg, card, rec["id"])
            created_ids.append(rec["id"])

            mode = spec.get("turnaround")
            if mode and not args.skip_turnaround:
                if not args.force and existing_pack(cfg, card["id"], mode):
                    log(f"  四视图包（{mode}）已存在，跳过")
                else:
                    t0 = time.time()
                    n = 1 if mode == "sheet" else 4
                    log(f"  生成四视图（{mode}，{n} 张 API 图）…")
                    pack = turnaround_pack(cfg, spec, card, plan, mode)
                    log(f"  完成 {pack['id']} · {time.time() - t0:.1f}s · {len(pack['views'])} 视图")
                    for v in pack["views"]:
                        if v.get("asset_id"):
                            created_ids.append(v["asset_id"])
        except Exception as exc:
            # One failing character must not discard the rest of a 30-minute run.
            failures.append(f"{card['name']}: {type(exc).__name__}: {exc}")
            log(f"  !! {card['name']} 失败：{type(exc).__name__}: {exc}")

    if failures:
        log(f"本轮有 {len(failures)} 个角色失败；重跑本脚本会自动跳过已完成的图并只补失败项：")
        for f in failures:
            log("   - " + f)

    # Export every asset of this style, not just the ones created in this run:
    # a resumed run must still produce a complete, reproducible pack.
    rows, _ = store.query_page(cfg, style=plan["style_id"], limit=2000)
    rows = [r for r in rows if not r.get("demo")]
    if not rows:
        raise SystemExit("没有可导出的资产。")

    style = imaging.load_style(cfg, plan["style_id"])
    recipe = {
        "batch_id": plan["id"], "generated_at": store.now(),
        "style_id": plan["style_id"], "model": plan["model"],
        "quality": plan["quality"], "portrait_size": portrait_size,
        "sheet_size": plan["sheet_size"], "view_size": plan["view_size"],
        "text_model": plan.get("text_model"),
        "novel_excerpt": plan["novel_excerpt"],
        "source_note": plan.get("source_note"),
        "manifest": str(MANIFEST.relative_to(ROOT)).replace("\\", "/"),
        "regenerate_command": "python tools/generate_sanguo.py --skip-extract",
        "reproduce_note": ("逐字复用 character-cards.json 的 anchor，配合 sanguo-hero.json "
                           "的 blocks/negative 与上方 model/quality/size；"
                           "把该角色的一张立绘作为 /images/edits 参考图并设 "
                           "input_fidelity=high 可延续人物长相与画风。"),
        "asset_count": len(rows),
        "total_cost_usd": round(sum(float(r.get("cost_usd") or 0) for r in rows), 4),
    }
    extra = {
        "recipe.json": recipe,
        "character-cards.json": [characters.get_card(cfg, c["id"]) for _, c in cards],
        "style-bible.json": style,
        "README.txt": (
            "三国英雄 · 首批角色美术资产\n"
            "============================\n"
            f"批次 {plan['id']} · 生成于 {recipe['generated_at']}\n"
            f"模型 {plan['model']} / {plan['quality']} · 立绘 {portrait_size} · 配方 {plan['style_id']}\n\n"
            "images/                所有原始 PNG（保留 API 返回的来源元数据，未剥离）\n"
            "manifest.json          每张图的完整参数：prompt、model、quality、size、\n"
            "                       character_id、anchor_version、cost、metadata、tags\n"
            "character-cards.json   角色卡全量快照，含逐字复用的 anchor 与版本号\n"
            "style-bible.json       画风配方（分块提示词 + 负面词）\n"
            "recipe.json            本次批量运行的全部参数与复现命令\n"
            "prompts.txt            纯文本提示词清单\n\n"
            "复现 / 换姿势 / 换视角\n"
            "----------------------\n"
            "1. 打开工作台 http://127.0.0.1:8777 →「角色与游戏资产」，角色卡已入库。\n"
            "2. 点「复现单张角色图」：系统自动把 anchor 放在提示词最前面，\n"
            "   并把该角色立绘作为 /images/edits 参考图（input_fidelity=high）。\n"
            "3. 点「生成四视图包」可出正面 / 3-4 侧 / 正侧面 / 背面。\n"
            "4. 命令行原样重跑：python tools/generate_sanguo.py --skip-extract\n"
            "   （已生成的图会自动跳过，不会重复计费）\n\n"
            "注意：锚点版本变化意味着设定改过，旧图与新图不要混用。\n"
            "图片可能含平台 AIGC 显式标识，请按发布平台要求标注。\n"),
    }
    out = exporter.export_zip(cfg, rows, f"{plan['id']}-{datetime.now():%Y%m%d}", extra)
    log(f"导出 {out['count']} 张 → {out['path']}")
    log(f"成本合计 ${recipe['total_cost_usd']} · 耗时 {time.time() - started:.0f}s")

    health = exporter.health(cfg)
    log(f"元数据检查 {health['checked']} 项，问题 {len(health['issues'])} 处")
    print(json.dumps({
        "ok": True, "assets": len(rows), "zip": out["path"],
        "cost_usd": recipe["total_cost_usd"],
        "characters": [c["name"] for _, c in cards],
        "metadata_healthy": health["healthy"],
        "failures": failures,
        "new_assets_this_run": len(created_ids),
    }, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
