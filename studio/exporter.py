"""Export asset packs and sync curated keepers to the configured asset root.

H: is a Google Drive mirror. Small files written one by one can stall or conflict
during sync, so everything is copied in one batch folder in a single operation.
"""
from __future__ import annotations

import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from . import config, imaging, store


def export_zip(cfg: dict, rows: list, name: str | None = None,
               extra_files: dict | None = None) -> dict:
    """Bundle images + a full reproducibility manifest.

    extra_files maps arcname -> str content, used for character cards, the
    style bible and the batch recipe so a pack can be regenerated later
    without access to the original database.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c for c in (name or f"asset-pack-{stamp}")
                   if c.isalnum() or c in "-_")[:60] or f"asset-pack-{stamp}"
    out = cfg["local"] / "export" / f"{safe}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)

    # Everything needed to reproduce an image, not just its prompt.
    META_KEYS = ("id", "filename", "style_id", "style_name", "label",
                 "prompt", "model", "quality", "size", "cost_usd",
                 "character_id", "anchor_version", "parent_id", "source",
                 "asset_kind", "note", "rating", "tags", "favorite",
                 "status", "demo", "metadata", "created_at",
                 # comic / scene batches
                 "batch_id", "scene_id", "panel_no", "comic_caption",
                 "comic_dialogue", "comic_characters", "composition",
                 "reference_used", "degraded")

    manifest, lines, copied = [], [], 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for row in rows:
            src = cfg["local"] / str(row.get("file", ""))
            if not src.is_file():
                continue
            zf.write(src, arcname=f"images/{row.get('filename') or src.name}")
            manifest.append({k: row[k] for k in META_KEYS if k in row})
            lines.append(f"[{row.get('id')}] {row.get('style_id')}\n{row.get('prompt')}\n")
            copied += 1
        zf.writestr("manifest.json",
                    json.dumps({"generated_at": store.now(), "count": copied,
                                "assets": manifest}, ensure_ascii=False, indent=2))
        zf.writestr("prompts.txt", "\n".join(lines))
        for arcname, content in (extra_files or {}).items():
            zf.writestr(arcname, content if isinstance(content, str)
                        else json.dumps(content, ensure_ascii=False, indent=2))
    return {"ok": True, "path": str(out), "count": copied, "filename": out.name}


def export_recipe(cfg: dict, style_id: str) -> dict:
    style = imaging.load_style(cfg, style_id)
    if not style:
        raise RuntimeError(f"unknown style: {style_id}")
    return {"style": style, "versions": store.versions(cfg, style_id)}


def sync(cfg: dict, style_id: str | None = None,
         include_demo: bool | None = None) -> dict:
    root = cfg["asset_root_path"]
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / (".probe-" + datetime.now().strftime("%H%M%S%f"))
        probe.mkdir()
        probe.rmdir()
    except OSError as exc:
        config.refresh_asset_root(cfg, False)
        return {
            "ok": False,
            "reason": (f"目标目录当前不可写（{exc}）。请确认该目录存在且有写权限，"
                       f"或在 config.json 里修改 asset_root。"),
            "target": str(root),
            "copied": 0,
        }

    allow_demo = bool(cfg.get("sync_include_demo")) if include_demo is None else bool(include_demo)
    rows, demo_kept = [], 0
    for r in store.iter_all(cfg):
        if r.get("status") == "keeper" and r.get("demo"):
            demo_kept += 1
        if (r.get("status") == "keeper"
                and (not r.get("demo") or allow_demo)
                and (not style_id or r.get("style_id") == style_id)):
            rows.append(r)
    if not rows:
        reason = "没有可同步的精选资产。先在画廊里把资产标为精选。"
        if demo_kept and not allow_demo:
            reason = (f"有 {demo_kept} 张精选演示图，但演示图默认不同步。"
                      f"勾选「包含演示图」可测试同步流程。")
        return {"ok": True, "copied": 0, "target": str(root), "reason": reason}

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = root / (style_id or "all") / f"batch-{stamp}"
    target.mkdir(parents=True, exist_ok=True)

    copied, missing = 0, 0
    for row in rows:
        src = cfg["local"] / str(row.get("file", ""))
        if not src.is_file():
            missing += 1
            continue
        shutil.copy2(src, target / (row.get("filename") or src.name))
        copied += 1

    snapshot = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    (target / "meta.jsonl").write_text(snapshot, encoding="utf-8")
    config.refresh_asset_root(cfg, True)
    return {"ok": True, "copied": copied, "missing": missing,
            "target": str(target), "batch": target.name}


def health(cfg: dict) -> dict:
    issues, checked = [], 0
    for row in store.iter_all(cfg):
        checked += 1
        if row.get("demo"):
            continue
        src = cfg["local"] / str(row.get("file", ""))
        if not src.is_file():
            if len(issues) < 50:
                issues.append({"id": row.get("id"), "issue": "图片文件缺失"})
        elif row.get("metadata") in ("none", "unreadable"):
            if len(issues) < 50:
                issues.append({"id": row.get("id"), "issue": "缺少来源元数据（疑似被剥离）"})
        if not (row.get("prompt") or "").strip() and len(issues) < 50:
            issues.append({"id": row.get("id"), "issue": "提示词为空"})
    return {"checked": checked, "issues": issues[:50],
            "healthy": len(issues) == 0,
            "asset_root_writable": config.asset_root_writable(cfg)}
