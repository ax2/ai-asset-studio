#!/usr/bin/env python3
"""Controller for the 10-episode Three Kingdoms comic series.

    python tools/generate_comic_series.py --list
    python tools/generate_comic_series.py --prepare-only   # cards + shared portraits
    python tools/generate_comic_series.py                 # full series
    python tools/generate_comic_series.py --index-only    # rebuild index + sync
    python tools/generate_comic_series.py --episodes 1,2,3 --workers 3
    python tools/generate_comic_series.py --fix-degraded

Design notes
------------
* Character cards and their portraits are shared across the whole series. All
  cards are created once, and every missing portrait is generated once, before
  any episode worker starts. That removes cross-episode dependencies and stops
  two workers from racing on the same card row.
* Episode workers are separate processes running generate_comic.py with
  --cards-ready (read-only card access). SQLite WAL + busy_timeout make the
  concurrent asset writes safe; each episode owns its own ZIP, scene cards and
  sync directory, so there is no shared output file.
* Everything is idempotent: rerunning skips already-indexed assets, so an
  interrupted or partially failed series never pays for the same image twice.
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import generate_comic as gc  # noqa: E402
from studio import characters, config, exporter, pricing, scenes, store  # noqa: E402

DEFAULT_SERIES = "batches/series-sanguo-tales-10.json"


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_series(path: str) -> tuple[dict, list[tuple[int, dict, str]]]:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    sm = json.loads(p.read_text(encoding="utf-8"))
    out = []
    for ep in sm["episodes"]:
        rel = "batches/" + ep["file"]
        f = ROOT / rel
        if not f.is_file():
            raise SystemExit(f"剧本文件不存在：{f}")
        out.append((int(ep["no"]), json.loads(f.read_text(encoding="utf-8")), rel))
    out.sort(key=lambda x: x[0])
    return sm, out


# ------------------------------------------------------------------ phase A: cards

def prepare_cards(cfg: dict, items) -> dict:
    """Create every character card exactly once, before any worker starts."""
    by_name = {c["name"]: c for c in characters.list_cards(cfg)}
    entries = {}
    created = 0
    for _no, plan, _rel in items:
        for spec in plan["characters"]:
            name = spec["name"]
            if name in entries:
                continue
            card = by_name.get(name)
            if card is None:
                fresh = characters.blank_card(name)
                fresh["profile"] = dict(spec.get("profile") or {})
                fresh["aliases"] = [spec.get("slug") or name]
                fresh["source"] = plan["story_source"][:200]
                fresh["mode"] = "comic-series-curated"
                fresh["evidence"] = {
                    "story_source": plan["story_source"][:200],
                    "episode": plan.get("episode"),
                }
                fresh["confidence"] = {"source_of_truth": "comic-script"}
                fresh["series_id"] = plan.get("series_id") or ""
                card = characters.put_card(cfg, fresh)
                by_name[name] = card
                created += 1
            # First episode introducing this character owns its portrait asset.
            entries[name] = (card, spec, plan)
    log(f"角色卡就绪：共 {len(entries)} 人（新建 {created} 张，复用 {len(entries)-created} 张）")
    return entries


def _valid_ref(cfg: dict, card: dict) -> bool:
    ref = card.get("ref_asset_id")
    if not ref:
        return False
    asset = store.get(cfg, ref)
    return bool(asset and (cfg["local"] / str(asset.get("file") or "")).is_file())


def prepare_portraits(cfg: dict, entries: dict, workers: int) -> list[str]:
    """Generate one shared full-body portrait per card that lacks a valid one."""
    todo = [(name, card, spec, plan)
            for name, (card, spec, plan) in entries.items()
            if not _valid_ref(cfg, card)]
    if not todo:
        log("共享人物立绘已齐备，跳过")
        return []
    unit = pricing.image_price(
        str(cfg.get("default_image_model") or "gpt-image-2.6-sunburst"), "high")
    log(f"需补 {len(todo)} 张共享人物立绘 · 约 ${len(todo)*unit:.3f} · {workers} 并发")

    def one(item):
        name, card, spec, plan = item
        t0 = time.time()
        try:
            rec = gc.portrait(cfg, plan, spec, card)
            return name, rec["id"], time.time() - t0, ""
        except Exception as exc:
            return name, "", time.time() - t0, f"{type(exc).__name__}: {exc}"

    failures = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for name, aid, secs, err in pool.map(one, todo):
            if err:
                failures.append(f"{name}: {err}")
                log(f"  !! {name} 立绘失败：{err[:140]}")
            else:
                log(f"  ✓ {name} · {aid} · {secs:.1f}s")
    return failures


# ------------------------------------------------------------------ phase B: episodes

def run_episodes(cfg: dict, items, workers: int, extra: list[str]) -> list[dict]:
    logdir = cfg["local"] / "series-logs"
    logdir.mkdir(parents=True, exist_ok=True)
    queue = list(items)
    running = {}
    results = []
    log(f"启动 {len(queue)} 集生成 · {workers} 进程并发 · 日志 {logdir}")
    while queue or running:
        while queue and len(running) < workers:
            no, plan, rel = queue.pop(0)
            lf = logdir / f"s{no:02d}-{plan['id']}.log"
            fh = lf.open("w", encoding="utf-8")
            cmd = [sys.executable, "-u", str(ROOT / "tools" / "generate_comic.py"),
                   "--plan", rel, "--cards-ready", *extra]
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=fh,
                                    stderr=subprocess.STDOUT)
            running[proc] = (no, plan, rel, fh, lf, time.time())
            log(f"  → 第 {no:>2} 集启动：{plan['title']}")
        if not running:
            break
        time.sleep(5)
        for proc, (no, plan, rel, fh, lf, t0) in list(running.items()):
            rc = proc.poll()
            if rc is None:
                continue
            fh.close()
            tail = ""
            try:
                lines = [x.strip() for x in lf.read_text(
                    encoding="utf-8", errors="replace").splitlines() if x.strip()]
                tail = lines[-1][:150] if lines else ""
            except OSError:
                pass
            ok = rc == 0
            log(f"  {'✓' if ok else '✗'} 第 {no:>2} 集结束 rc={rc} · "
                f"{time.time()-t0:.0f}s · {tail}")
            results.append({"no": no, "id": plan["id"], "title": plan["title"],
                            "ok": ok, "returncode": rc,
                            "log": str(lf), "tail": tail})
            del running[proc]
    return results


def degraded_episodes(cfg: dict, items) -> list[tuple[int, dict, str]]:
    out = []
    for no, plan, rel in items:
        rows = [r for r in store.iter_all(cfg)
                if r.get("batch_id") == plan["id"] and not r.get("demo")]
        panels = [r for r in rows if r.get("asset_kind") == "comic-panel"]
        bad = [r for r in panels
               if not r.get("reference_used") or r.get("degraded")]
        if len(panels) < len(plan["panels"]) or bad:
            out.append((no, plan, rel))
    return out


# ------------------------------------------------------------------ phase C: index

def episode_stats(cfg: dict, plans) -> dict:
    by_batch = {}
    for r in store.iter_all(cfg):
        if r.get("demo"):
            continue
        by_batch.setdefault(str(r.get("batch_id") or ""), []).append(r)
    out = {}
    for plan in plans:
        rows = by_batch.get(plan["id"], [])
        panels = sorted([r for r in rows if r.get("asset_kind") == "comic-panel"],
                        key=lambda r: int(r.get("panel_no") or 0))
        scenes_n = sum(1 for r in rows if r.get("asset_kind") == "scene")
        portraits_n = sum(1 for r in rows if r.get("asset_kind") == "character-portrait")
        out[plan["id"]] = {
            "rows": rows, "panels": panels, "scenes": scenes_n,
            "portraits": portraits_n,
            "cost": round(sum(float(r.get("cost_usd") or 0) for r in rows), 4),
            "metadata": sorted({str(r.get("metadata") or "") for r in rows}),
            "refs_ok": all(r.get("reference_used") and not r.get("degraded")
                           for r in panels) if panels else False,
            "thumb": ("../" + plan["id"] + "/images/" + panels[0]["filename"])
                     if panels else "",
        }
    return out


def series_html(sm: dict, plans, stats: dict) -> str:
    parts = [
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{html.escape(sm['title'])}</title>",
        "<style>",
        "*{box-sizing:border-box}body{margin:0;background:#efe4cc;color:#2b2118;"
        "font-family:'Noto Serif SC','Songti SC','SimSun',serif}",
        ".wrap{max-width:1080px;margin:0 auto;padding:38px 20px 90px}",
        "header{text-align:center;border-bottom:2px solid #b08b57;padding-bottom:30px}",
        "h1{margin:0;font-size:36px;letter-spacing:.12em}",
        ".sub{margin-top:12px;color:#7a5c3a;font-size:15px;line-height:1.8}",
        ".rule{margin:14px auto 0;max-width:760px;font-size:13px;color:#6b563d;line-height:1.8}",
        ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));"
        "gap:22px;margin-top:34px}",
        ".card{background:#f8f1e2;border:1px solid #d3bd97;border-radius:4px;"
        "overflow:hidden;box-shadow:0 5px 18px rgba(80,55,25,.12);display:flex;"
        "flex-direction:column}",
        ".card img{display:block;width:100%;height:186px;object-fit:cover;background:#e2d4b8}",
        ".no{background:#3c2c1c;color:#e9d6ae;font-size:12px;letter-spacing:.2em;"
        "padding:8px 16px}",
        ".body{padding:16px 18px 20px;flex:1}",
        ".body h2{margin:0 0 8px;font-size:21px}",
        ".src{font-size:12px;color:#6b563d;line-height:1.75;margin:0 0 10px}",
        ".why{font-size:12px;color:#8b5e3c;line-height:1.7;margin:0 0 12px}",
        ".meta{font-size:11px;color:#8b7457;letter-spacing:.04em;margin-bottom:14px}",
        ".acts{display:flex;gap:12px;flex-wrap:wrap;align-items:center}",
        ".acts a{color:#8b3a24;text-decoration:none;border-bottom:1px solid #b08b57;"
        "font-size:13px}",
        "footer{text-align:center;margin-top:48px;font-size:12px;color:#8b7457;line-height:1.9}",
        "@media print{body{background:#fff}.card{box-shadow:none;break-inside:avoid}}",
        "</style></head><body><div class='wrap'><header>",
        f"<h1>{html.escape(sm['title'])}</h1>",
        "<div class='sub'>剧本由 GPT-6.1 Sol 依据史料创作，画面由 "
        "gpt-image-2.6-sunburst / high 生成；画面不含文字，旁白与对白由阅读页叠加。</div>",
        f"<div class='rule'>{html.escape(sm.get('constraint') or '')}。"
        "每集含 6 格分镜、2 张无人物场景概念图、角色卡与完整可复现元数据。</div>",
        "</header><div class='grid'>",
    ]
    total_assets = total_cost = 0
    for plan in plans:
        s = stats[plan["id"]]
        total_assets += len(s["rows"])
        total_cost += s["cost"]
        badge = "参考图完整" if s["refs_ok"] else "部分降级"
        parts.append("<article class='card'>")
        if s["thumb"]:
            parts.append(f"<img src='{html.escape(s['thumb'])}' alt='第{plan['episode']}集'>")
        parts.append(f"<div class='no'>第 {plan['episode']:02d} 集 · "
                     f"{html.escape(plan['title'])}</div>")
        parts.append("<div class='body'>")
        parts.append(f"<h2>{html.escape(plan['title'])}</h2>")
        parts.append(f"<p class='src'>{html.escape(plan['story_source'][:150])}</p>")
        parts.append(f"<p class='why'>演义未收录："
                     f"{html.escape(plan['why_not_in_romance'][:120])}</p>")
        parts.append(f"<div class='meta'>{len(s['panels'])} 格分镜 · "
                     f"{s['scenes']} 场景 · {s['portraits']} 立绘 · "
                     f"${s['cost']:.3f} · {html.escape(', '.join(s['metadata']))} · "
                     f"{badge}</div>")
        parts.append("<div class='acts'>")
        parts.append(f"<a href='../{html.escape(plan['id'])}/comic.html'>▶ 阅读本集</a>")
        parts.append(f"<a href='zips/{html.escape(plan['id'])}.zip'>↓ 素材包 ZIP</a>")
        parts.append(f"<a href='../{html.escape(plan['id'])}/manifest.json'>≡ manifest</a>")
        parts.append("</div></div></article>")
    parts.append("</div>")
    parts.append(
        f"<footer>共 {len(plans)} 集 · {total_assets} 张入库资产 · "
        f"估算成本 ${total_cost:.3f}（gpt-image-2.6-sunburst 官方单价未公开，"
        "按上一代同档估算，见 studio/pricing.py）<br>"
        "人物立绘为全系列共享资产；场景卡与角色卡保存在 SQLite，"
        "完整参数见各集 recipe.json / manifest.json。</footer>")
    parts.append("</div></body></html>")
    return "\n".join(parts)


def sync_series(cfg: dict, sm: dict, plans, stats: dict) -> dict:
    sid = sm["id"]
    page = series_html(sm, plans, stats)
    scene_cards = scenes.list_cards(cfg)
    manifest = {
        "id": sid, "title": sm["title"], "generated_at": store.now(),
        "text_model": sm.get("text_model"), "image_model": sm.get("image_model"),
        "quality": sm.get("quality"), "style_id": sm.get("style_id"),
        "constraint": sm.get("constraint"),
        "pricing_note": "gpt-image-2.6-sunburst 官方单价未公开，成本为估算值。",
        "scene_cards_total": len(scene_cards),
        "character_cards_total": len(characters.list_cards(cfg)),
        "episodes": [],
    }
    for plan in plans:
        s = stats[plan["id"]]
        ep_scenes = [x for x in scene_cards if x.get("batch_id") == plan["id"]]
        manifest["episodes"].append({
            "no": plan["episode"], "id": plan["id"], "title": plan["title"],
            "subtitle": plan.get("subtitle"),
            "story_source": plan["story_source"],
            "why_not_in_romance": plan["why_not_in_romance"],
            "characters": [c["name"] for c in plan["characters"]],
            "scenes": [x["name"] for x in plan["scenes"]],
            "scene_cards": [x["id"] for x in ep_scenes],
            "assets": len(s["rows"]),
            "panels": len(s["panels"]),
            "reference_images_complete": s["refs_ok"],
            "metadata": s["metadata"], "cost_usd": s["cost"],
            "comic_html": f"../{plan['id']}/comic.html",
            "zip": f"zips/{plan['id']}.zip",
        })
    manifest["total_assets"] = sum(x["assets"] for x in manifest["episodes"])
    manifest["total_cost_usd"] = round(sum(x["cost_usd"] for x in manifest["episodes"]), 4)
    readme = (
        f"{sm['title']}\n{'=' * 40}\n"
        f"生成于 {manifest['generated_at']}\n"
        f"文本 {manifest['text_model']} · 图像 {manifest['image_model']} / {manifest['quality']}\n\n"
        "index.html            本目录首页，10 集卡片与阅读/下载入口\n"
        "series-manifest.json  全系列清单：史料出处、人物、场景卡、资产数、成本\n"
        "zips/                 每集完整素材包（图片 + 剧本 + 卡片 + recipe + comic.html）\n"
        "../comic-sNN-*/       每集解压后的目录，含可直接双击阅读的 comic.html\n\n"
        "复现\n----\n"
        "python tools/write_comic_series.py            # 重写剧本\n"
        "python tools/generate_comic_series.py        # 幂等生成全系列\n"
        "python tools/generate_comic.py --plan batches/comic-sNN-xxx.json  # 单独重跑一集\n\n"
        "画面不含任何文字；旁白与对白由 comic.html 排版叠加。人物立绘与场景卡为\n"
        "全系列共享的可复现资产，锚点版本变化意味着人物设定改过，勿混用新旧图。\n"
    )
    root = cfg["asset_root_path"] / "all" / sid
    local = cfg["local"] / "packs" / sid
    copied, missing = [], []
    for folder in (root, local):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "index.html").write_text(page, encoding="utf-8")
        (folder / "series-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (folder / "README.txt").write_text(readme, encoding="utf-8")
        (folder / "zips").mkdir(exist_ok=True)
    for plan in plans:
        src = cfg["local"] / "export" / (plan["id"] + ".zip")
        if not src.is_file():
            missing.append(plan["id"])
            continue
        for folder in (root, local):
            shutil.copyfile(src, folder / "zips" / src.name)
        copied.append(src.name)
    log(f"系列目录已同步：{root}")
    log(f"素材包 {len(copied)} 个；缺失 {len(missing)} 个"
        + ("：" + ", ".join(missing) if missing else ""))
    return {"series_dir": str(root), "workbench_dir": str(local),
            "zips": copied, "missing_zips": missing, "manifest": manifest}


# ------------------------------------------------------------------ main

def main() -> None:
    ap = argparse.ArgumentParser(description="生成 10 集三国史料小故事连环画")
    ap.add_argument("--series", default=DEFAULT_SERIES, help="系列清单 JSON")
    ap.add_argument("--workers", type=int, default=3, help="并发生成进程数")
    ap.add_argument("--list", action="store_true", help="只打印计划与预估成本")
    ap.add_argument("--prepare-only", action="store_true", help="只建角色卡与共享立绘")
    ap.add_argument("--index-only", action="store_true", help="只重建索引并同步")
    ap.add_argument("--skip-portraits", action="store_true", help="跳过共享立绘阶段")
    ap.add_argument("--episodes", default="", help="只跑指定集，逗号分隔，如 1,2,3")
    ap.add_argument("--fix-degraded", action="store_true",
                    help="生成后自动补跑降级/缺图的分镜")
    args = ap.parse_args()

    cfg = config.load()
    sm, items = load_series(args.series)
    if args.episodes:
        wanted = {int(x) for x in args.episodes.split(",") if x.strip()}
        items = [x for x in items if x[0] in wanted]
    plans = [p for _n, p, _r in items]
    unit = pricing.image_price(plans[0]["image_model"], plans[0]["quality"])
    log(f"系列 {sm['title']} · {len(items)} 集 · 图像 {plans[0]['image_model']} / "
        f"{plans[0]['quality']} · ${unit}/张（估算）")
    if args.list:
        for no, plan, rel in items:
            print(f"  {no:>2}. {plan['title']} · {plan['id']} · "
                  f"{len(plan['characters'])} 人 / {len(plan['scenes'])} 景 / "
                  f"{len(plan['panels'])} 格", flush=True)
        return
    if not config.has_key(cfg) and not args.index_only:
        raise SystemExit("没有可用的 API key。")

    if not args.index_only:
        entries = prepare_cards(cfg, items)
        if not args.skip_portraits:
            failures = prepare_portraits(cfg, entries, max(1, args.workers))
            if failures:
                log(f"警告：{len(failures)} 张共享立绘失败，相关分镜可能降级")
        if not args.prepare_only:
            results = run_episodes(cfg, items, max(1, args.workers), [])
            bad = [r for r in results if not r["ok"]]
            if bad:
                log(f"{len(bad)} 集进程异常：" + ", ".join(str(r["no"]) for r in bad))
            if args.fix_degraded:
                todo = degraded_episodes(cfg, items)
                if todo:
                    log(f"补跑 {len(todo)} 集的降级/缺失分镜")
                    run_episodes(cfg, todo, max(1, min(2, args.workers)),
                                 ["--redo-degraded-panels"])
    else:
        log("--index-only：跳过生成，只重建索引与同步")

    stats = episode_stats(cfg, plans)
    info = sync_series(cfg, sm, plans, stats)
    health = exporter.health(cfg)
    summary = {
        "ok": True, "series": sm["id"], "episodes": len(plans),
        "total_assets": info["manifest"]["total_assets"],
        "total_cost_usd": info["manifest"]["total_cost_usd"],
        "scene_cards": info["manifest"]["scene_cards_total"],
        "character_cards": info["manifest"]["character_cards_total"],
        "zips": len(info["zips"]), "missing_zips": info["missing_zips"],
        "series_dir": info["series_dir"],
        "read": "http://127.0.0.1:8777/pack/" + sm["id"] + "/index.html",
        "health_checked": health["checked"], "health_issues": len(health["issues"]),
    }
    for plan in plans:
        s = stats[plan["id"]]
        log(f"  第 {plan['episode']:02d} 集 {plan['title']}："
            f"{len(s['rows'])} 张（{len(s['panels'])} 格 / {s['scenes']} 景）"
            f" · ${s['cost']:.3f} · 参考图{'完整' if s['refs_ok'] else '有降级'}")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
