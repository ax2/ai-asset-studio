"""Character turnaround: four consistent views + reproducible character card.

Two modes:
  sheet     - one API call producing a 4-panel model sheet, then split into
              four independent PNGs (cheap, strongest internal consistency)
  separate  - one call per view, optionally guided by a reference image via
              /images/edits with input_fidelity=high (best style retention)

Every output carries the exact character anchor and its version, so the same
character can be regenerated later with an identical recipe.
"""
from __future__ import annotations

import html
import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from . import characters, config, imaging, png, pricing, store, thumbs

STYLE_ID = "character-turnaround"
VIEWS = [
    ("front", "正面", "front view, facing the viewer, symmetrical relaxed pose, arms at sides"),
    ("three_quarter", "3/4 侧", "three-quarter view turned about 30 degrees, same relaxed pose"),
    ("side", "正侧面", "exact side profile turned 90 degrees, same relaxed pose"),
    ("back", "背面", "back view facing away from the viewer, same relaxed pose and outfit"),
]
_DEMO_COLORS = [(58, 92, 122), (72, 110, 96), (122, 92, 72), (88, 80, 112)]


def _blocks(cfg: dict, style_id: str = STYLE_ID) -> tuple[str, str]:
    style = imaging.load_style(cfg, style_id) or {}
    text = ". ".join(str(b.get("en") or "").strip().rstrip(".")
                     for b in style.get("blocks", []) if b.get("en"))
    negative = str(style.get("negative") or "").strip()
    return text, negative


def style_name(cfg: dict, style_id: str) -> str:
    style = imaging.load_style(cfg, style_id) or {}
    return str(style.get("name") or style_id)


def build_prompt(cfg: dict, card: dict, view_key: str = "",
                 style_id: str = STYLE_ID) -> str:
    text, negative = _blocks(cfg, style_id)
    anchor = str(card.get("anchor") or "").strip() or characters.build_anchor(card)
    if view_key:
        extra = next((v[2] for v in VIEWS if v[0] == view_key), "")
        parts = [anchor, text,
                 "SINGLE VIEW: " + extra + ". Only one character in the image, "
                 "centered, full body, plain uniform background, no panel borders."]
    else:
        parts = [anchor, text,
                 "FOUR VIEW TURNAROUND SHEET: exactly four equally sized panels "
                 "in one horizontal row, from left to right: front view, "
                 "three-quarter view, side profile, back view. Same height, same "
                 "scale, same pose, same outfit. No borders, no separators, no text."]
    prompt = "\n\n".join(p for p in parts if p)
    if negative:
        prompt += "\n\nAvoid: " + negative
    return prompt


def build_single_prompt(cfg: dict, card: dict, extra: str = "",
                        style_id: str = STYLE_ID) -> str:
    """Prompt for one reproducible character image (not a multi-view sheet)."""
    text, negative = _blocks(cfg, style_id)
    anchor = str(card.get("anchor") or "").strip() or characters.build_anchor(card)
    parts = [anchor, text,
             "SINGLE CHARACTER IMAGE: exactly one full-body character, centered, "
             "orthographic, plain uniform background, evenly lit, no panel borders, "
             "no text, no other characters.",
             str(extra or "").strip()]
    prompt = "\n\n".join(p for p in parts if p)
    if negative:
        prompt += "\n\nAvoid: " + negative
    return prompt


def reproduce(cfg: dict, *, character_id: str, extra: str = "",
              ref_asset_id: str = "", dry_run: bool = True,
              model: str | None = None, quality: str | None = None,
              size: str | None = None, style_id: str = STYLE_ID,
              label: str = "") -> dict:
    """Regenerate one image of an existing character with its frozen anchor.

    With a reference asset the call goes through /images/edits and
    input_fidelity=high, which is the strongest available style-retention
    lever. Any edit failure degrades to plain text-to-image instead of
    aborting the whole run.
    """
    card = characters.get_card(cfg, character_id)
    if not card:
        raise ValueError("角色不存在，请先保存角色卡。")
    prompt = build_single_prompt(cfg, card, extra, style_id)
    model = model or str(cfg.get("default_model") or "gpt-image-1.5")
    quality = quality or str(cfg.get("default_quality") or "low")
    size = size or str(cfg.get("default_size") or "1024x1024")
    if dry_run or not config.has_key(cfg):
        res = imaging.generate(
            cfg, style_id=style_id, prompt=prompt, model=model,
            quality=quality, size=size, dry_run=True, source="character-reproduce",
            asset_kind="character", character_id=card["id"],
            anchor_version=int(card.get("anchor_version") or 1),
            note="Dry run 占位图；未调用 API。")
        res.update({"prompt": prompt, "reference_used": False, "degraded": ""})
        return res

    unit = pricing.estimate(model, quality, 1)
    ref_bytes, degraded, used_edit = [], "", False
    ref_id = ref_asset_id or card.get("ref_asset_id") or ""
    if ref_id:
        ref = store.get(cfg, ref_id)
        if ref:
            path = cfg["local"] / str(ref.get("file") or "")
            if path.is_file() and path.suffix.lower() == ".png":
                ref_bytes = [path.read_bytes()]
    if ref_bytes:
        try:
            data = imaging.call_openai_edit(
                cfg, prompt, ref_bytes,
                model=cfg.get("edit_model") or model,
                quality=quality, size=size)
            used_edit = True
        except (RuntimeError, OSError, ValueError) as exc:
            degraded = f"参考图接口不可用（{exc}），已降级为纯文本生成。"
            data, _ext = imaging.call_openai(cfg, prompt, model, quality, size)
    else:
        data, _ext = imaging.call_openai(cfg, prompt, model, quality, size)

    note = ("锚点 v%d 复现；%s" % (int(card.get("anchor_version") or 1),
                                "参考图 input_fidelity=high" if used_edit else "纯锚点"))
    if degraded:
        note += "。" + degraded
    rec = _write_asset(cfg, data, card=card, kind="character",
                       label=label or f"{card['name']} · 复现", prompt=prompt,
                       model=model, quality=quality, size=size, cost=unit,
                       source="character-reproduce", note=note, style_id=style_id)
    return {"asset": rec, "prompt": prompt, "estimate_usd": unit,
            "demo": False, "reference_used": used_edit, "degraded": degraded}


def _write_asset(cfg: dict, data: bytes, *, card: dict, kind: str, label: str,
                 prompt: str, model: str, quality: str, size: str,
                 cost: float, source: str, note: str = "",
                 metadata: str = "", parent_id: str = "",
                 style_id: str = STYLE_ID) -> dict:
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
        pass  # thumbnails are an optimisation, never fail an asset write
    rec = {
        "id": aid, "file": f"raw/{day}/{filename}", "filename": filename,
        "style_id": style_id, "style_name": style_name(cfg, style_id),
        "label": label, "asset_kind": kind, "prompt": prompt,
        "model": model, "quality": quality, "size": size,
        "cost_usd": round(float(cost), 4), "demo": False, "source": source,
        "parent_id": parent_id, "character_id": card["id"],
        "anchor_version": int(card.get("anchor_version") or 1),
        "rating": 0, "tags": [card.get("name") or "角色", kind],
        "favorite": False, "status": "raw", "note": note,
        "metadata": metadata or imaging.check_metadata(path),
        "created_at": store.now(),
    }
    store.add(cfg, rec)
    return rec


def _preview(card: dict, views: list, demo: bool) -> str:
    cards = "".join(
        '<figure><img src="views/%s.png" alt="%s"><figcaption>%s</figcaption></figure>'
        % (html.escape(v["key"]), html.escape(v["zh"]), html.escape(v["zh"]))
        for v in views)
    banner = ("<div class=warn>演示模式：以下为布局占位图，未调用 API。配置 API key 后可生成真实多视图。</div>"
              if demo else "")
    return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>%(name)s - 人物多视角</title><style>
body{font-family:system-ui,sans-serif;background:#15171c;color:#e8e8e8;margin:0;padding:24px}
h1{font-size:17px;font-weight:500;margin:0 0 4px}.sub{color:#8b8f99;font-size:12px;margin-bottom:18px}
.row{display:flex;gap:12px;flex-wrap:wrap}
figure{margin:0;background:#20242c;border:1px solid #333944;border-radius:9px;padding:10px;text-align:center}
img{width:240px;height:320px;object-fit:contain;background:#2b303a;display:block;border-radius:5px}
figcaption{font-size:12px;color:#b9bdc7;margin-top:8px}
pre{background:#20242c;border:1px solid #333944;border-radius:9px;padding:14px;white-space:pre-wrap;
 font-size:11px;color:#b9bdc7;line-height:1.7;max-width:1000px}
.warn{background:#4a3a20;color:#f0cf8e;padding:10px 14px;border-radius:8px;font-size:12px;margin-bottom:16px}
h2{font-size:13px;font-weight:500;margin:22px 0 8px}
</style></head><body>
<h1>%(name)s · 人物多视角设定图</h1>
<div class="sub">锚点版本 v%(ver)d · 生成于 %(time)s</div>
%(banner)s
<div class="row">%(cards)s</div>
<h2>角色锚点（复现时必须逐字保留）</h2>
<pre>%(anchor)s</pre>
</body></html>
""" % dict(name=html.escape(card.get("name") or "角色"),
           ver=int(card.get("anchor_version") or 1),
           time=datetime.now().astimezone().isoformat(timespec="seconds"),
           banner=banner, cards=cards,
           anchor=html.escape(card.get("anchor") or ""))


def _finish(cfg: dict, root: Path, pack_id: str, card: dict, *,
            mode: str, model: str, views: list, images: dict,
            demo: bool, note: str, safe: str, master: bytes = b"",
            style_id: str = STYLE_ID) -> dict:
    (root / "views").mkdir(exist_ok=True)
    for key, data in images.items():
        (root / "views" / (key + ".png")).write_bytes(data)
    if master:
        (root / "turnaround.png").write_bytes(master)
    elif all(k in images for k, _, _ in VIEWS):
        cells = [png.decode(images[k]) for k, _, _ in VIEWS]
        if all(cells):
            h = min(c.height for c in cells)
            w = min(c.width for c in cells)
            resized = [png.resize(png.to_rgba(c), w, h) for c in cells]
            (root / "turnaround.png").write_bytes(png.encode(png.sheet(resized, 4, w, h)))
    meta = {
        "character": {"id": card.get("id"), "name": card.get("name"),
                      "anchor_version": card.get("anchor_version")},
        "mode": mode, "model": model, "style_id": style_id, "demo": demo,
        "note": note,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "views": views, "anchor": card.get("anchor") or "",
    }
    (root / "views.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "character-card.json").write_text(
        json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "preview.html").write_text(_preview(card, views, demo), encoding="utf-8")
    (root / "README.txt").write_text(
        ("AI 资产工作台 · 人物多视角包\n============================\n"
         "角色: %s    锚点版本: v%d    模式: %s\n\n"
         "views/           四张独立视图 PNG，可直接作为立绘 / 参考图使用\n"
         "turnaround.png   四视图并排合成图\n"
         "character-card.json  角色卡完整快照，含锚点与版本，用于复现\n"
         "views.json       每张视图的 prompt、资产编号与来源\n"
         "preview.html     双击查看并排效果\n\n"
         "复现方法\n--------\n"
         "把 character-card.json 的 anchor 字段逐字放进新生成的提示词最前面，\n"
         "并把该角色的一张已有图片作为 /images/edits 参考图（input_fidelity=high）。\n"
         "锚点版本变化意味着外貌设定被改过，旧图与新图不应混用。\n\n"
         "说明\n----\n%s\n") % (card.get("name"), card.get("anchor_version") or 1,
                            mode, note), encoding="utf-8")
    zip_path = cfg["local"] / "packs" / (pack_id + ".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(root.rglob("*")):
            if f.is_file():
                zf.write(f, arcname=str(f.relative_to(root)))
    data = {
        "id": pack_id, "character_id": card.get("id"),
        "character_name": card.get("name"), "mode": mode, "model": model,
        "style_id": style_id, "demo": demo, "note": note, "views": views,
        "preview": f"/pack/{pack_id}/preview.html",
        "turnaround": f"/pack/{pack_id}/turnaround.png",
        "download": f"/api/packs/{pack_id}/zip",
        "zip": str(zip_path), "dir": str(root),
    }
    characters.add_pack(cfg, "turnaround", safe, str(zip_path), data, pid=pack_id)
    return data


def _demo(cfg: dict, card: dict, name: str, reason: str,
          style_id: str = STYLE_ID) -> dict:
    pack_id = characters.new_id("turn")
    safe = "".join(c for c in (name or card.get("name") or "turnaround")
                   if c.isalnum() or c in "-_")[:40] or "turnaround"
    root = cfg["local"] / "packs" / pack_id
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    images, views = {}, []
    w, h = 256, 384
    for (key, zh, prompt), color in zip(VIEWS, _DEMO_COLORS):
        img = png.make(w, h, 4)
        r, g, b = color
        for y in range(h):
            for x in range(w):
                inside = ((96 <= x < 160 and 56 <= y < 128) or
                          (72 <= x < 184 and 140 <= y < 300) or
                          (88 <= x < 120 and 300 <= y < 352) or
                          (136 <= x < 168 and 300 <= y < 352))
                if inside:
                    img.rows[y][x * 4:(x + 1) * 4] = bytes((r, g, b, 255))
        images[key] = png.encode(img)
        views.append({"key": key, "zh": zh, "file": f"views/{key}.png",
                      "prompt": build_prompt(cfg, card, key, style_id), "asset_id": ""})
    return _finish(cfg, root, pack_id, card, mode="demo", model="local",
                   views=views, images=images, demo=True, note=reason,
                   safe=safe, style_id=style_id)


def build(cfg: dict, *, character_id: str, mode: str = "sheet",
          ref_asset_id: str = "", dry_run: bool = True,
          model: str | None = None, quality: str | None = None,
          size: str | None = None, name: str = "",
          style_id: str = STYLE_ID,
          reuse_views: dict | None = None) -> dict:
    """Build a view pack.

    ``reuse_views`` maps a view key to an already-indexed asset record from a
    previous, interrupted run; those views are read from disk instead of being
    generated again, so resuming a separate-mode pack never double-charges.
    """
    card = characters.get_card(cfg, character_id)
    if not card:
        raise ValueError("角色不存在，请先在角色工作台保存角色卡。")
    safe = "".join(c for c in (name or card.get("name") or "turnaround")
                   if c.isalnum() or c in "-_")[:40] or "turnaround"
    if dry_run or not config.has_key(cfg):
        return _demo(cfg, card, name or safe,
                     "未配置 API key 或选择了演示模式：只产出布局占位，未调用 API、未写入资产库。",
                     style_id)

    model = model or str(cfg.get("default_model") or "gpt-image-1.5")
    quality = quality or str(cfg.get("default_quality") or "low")
    size = size or ("1536x1024" if mode == "sheet" else "1024x1024")
    unit = pricing.estimate(model, quality, 1)
    pack_id = characters.new_id("turn")
    root = cfg["local"] / "packs" / pack_id
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    if mode == "sheet":
        prompt = build_prompt(cfg, card, style_id=style_id)
        data, _ext = imaging.call_openai(cfg, prompt, model, quality, size)
        master = _write_asset(cfg, data, card=card, kind="turnaround",
                              label=f"{card['name']} · 四视图设定图",
                              prompt=prompt, model=model, quality=quality,
                              size=size, cost=unit, source="turnaround",
                              style_id=style_id)
        img = png.decode(data)
        images, views = {}, []
        if img is None:
            note = "API 返回的 PNG 无法用内置解码器切分，已保留原始设定图；请用外部工具按四等分切图。"
        else:
            cells = png.split_grid(img, 4, 1)
            for (key, zh, _p), cell in zip(VIEWS, cells):
                images[key] = png.encode(cell)
                p = build_prompt(cfg, card, key, style_id=style_id)
                rec = _write_asset(cfg, images[key], card=card,
                                   kind="view-" + key, label=f"{card['name']} · {zh}",
                                   prompt=p, model=model, quality=quality,
                                   size=f"{cell.width}x{cell.height}", cost=0,
                                   source="turnaround-split",
                                   note="由四视图设定图等分切出，成本记在原图上。",
                                   metadata="derived", parent_id=master["id"],
                                   style_id=style_id)
                views.append({"key": key, "zh": zh, "file": f"views/{key}.png",
                              "prompt": p, "asset_id": rec["id"]})
            note = ("一次生成四视图设定图后等分切分；成本 $%.4f 只记在原图资产上。"
                    % unit)
    else:
        ref_bytes = []
        ref_id = ref_asset_id or card.get("ref_asset_id") or ""
        if ref_id:
            ref = store.get(cfg, ref_id)
            if ref:
                p = cfg["local"] / str(ref.get("file") or "")
                if p.is_file() and p.suffix.lower() == ".png":
                    ref_bytes = [p.read_bytes()]
        images, views, fallbacks, reused = {}, [], [], []
        for key, zh, _p in VIEWS:
            prompt = build_prompt(cfg, card, key, style_id=style_id)
            prior = (reuse_views or {}).get(key)
            if prior:
                prior_path = cfg["local"] / str(prior.get("file") or "")
                if prior_path.is_file() and prior_path.suffix.lower() == ".png":
                    images[key] = prior_path.read_bytes()
                    views.append({"key": key, "zh": zh, "file": f"views/{key}.png",
                                  "prompt": prompt, "asset_id": prior["id"],
                                  "reference_used": "参考图" in str(prior.get("note") or ""),
                                  "reused": True})
                    reused.append(zh)
                    continue
            used_edit = False
            if ref_bytes:
                try:
                    data = imaging.call_openai_edit(cfg, prompt, ref_bytes,
                                                    model=cfg.get("edit_model") or model,
                                                    quality=quality, size=size)
                    used_edit = True
                except (RuntimeError, OSError, ValueError) as exc:
                    fallbacks.append(f"{zh}: {exc}")
                    data, _ext = imaging.call_openai(cfg, prompt, model, quality, size)
            else:
                data, _ext = imaging.call_openai(cfg, prompt, model, quality, size)
            images[key] = data
            rec = _write_asset(cfg, data, card=card, kind="view-" + key,
                               label=f"{card['name']} · {zh}", prompt=prompt,
                               model=model, quality=quality, size=size,
                               cost=unit, source="turnaround-view",
                               note=("参考图模式" if used_edit else "锚点模式"),
                               style_id=style_id)
            views.append({"key": key, "zh": zh, "file": f"views/{key}.png",
                          "prompt": prompt, "asset_id": rec["id"],
                          "reference_used": used_edit})
        fresh = len(VIEWS) - len(reused)
        note = ("逐视图生成，%s；本轮新生成 %d 张 × $%.4f = $%.4f。"
                % ("已使用参考图（input_fidelity=high）" if ref_bytes else "仅用角色锚点",
                   fresh, unit, unit * fresh))
        if reused:
            note += " 复用上次中断前已完成的视图（未重复计费）：" + "、".join(reused)
        if fallbacks:
            note += " 降级记录：" + "；".join(fallbacks)[:300]
    return _finish(cfg, root, pack_id, card, mode=mode, model=model,
                   views=views, images=images, demo=False, note=note, safe=safe,
                   master=data if mode == "sheet" else b"", style_id=style_id)
