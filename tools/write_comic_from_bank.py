#!/usr/bin/env python3
"""Turn story-bank candidates into a 10-episode comic series plan.

    python tools/write_comic_from_bank.py --series sanguo-tales-b01 --tales 1,2,3...
    python tools/write_comic_from_bank.py --queue --batch 1

Reuses tools/write_comic_series.py's vetted six-panel prompt and validator,
but the stories come from batches/story-bank.json instead of a fresh model
selection. Only the text model is called; no images are generated.

Outputs:
    batches/comic-<episode-id>.json       one six-panel script per tale
    batches/series-<series-id>.json       series manifest for
                                          tools/generate_comic_series.py
The script is idempotent: an existing script file is reused unless --force.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import write_comic as wc  # noqa: E402
import write_comic_series as wcs  # noqa: E402
from studio import characters, config, text  # noqa: E402

BANK = ROOT / "batches" / "story-bank.json"
QUEUE = ROOT / "batches" / "story-queue.json"
IMAGE_MODEL = "gpt-image-2.6-sunburst"


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def known_cards(cfg: dict) -> list[dict]:
    skip = {"林晚", "沈砚"}
    return [c for c in characters.list_cards(cfg) if c["name"] not in skip]


def tale_by_no(no: int) -> dict:
    bank = json.loads(BANK.read_text(encoding="utf-8"))
    for r in bank["stories"]:
        if int(r["no"]) == no:
            return r
    raise KeyError(f"候选库里没有 no={no}")


def story_view(r: dict, known_names: set[str]) -> dict:
    source = str(r.get("source") or "").strip()
    quote = str(r.get("quote") or "").strip()
    if quote and quote != "原文待核":
        source += "；原文：" + quote
    return {
        "title": r["title"],
        "story_source": source,
        "why_not_in_romance": (str(r.get("romance_note") or "").strip()
                              + f"【演义归属：{r.get('romance_group') or '未标'}】"),
        "synopsis": str(r.get("synopsis") or "").strip(),
        "reuse": [c for c in (r.get("characters") or []) if c in known_names][:2],
    }


def write_one(cfg: dict, no: int, tale: dict, sid: str, episode: int,
              cards: list[dict], used_new: list[str], force: bool) -> dict:
    batch_id = f"comic-{sid}-e{episode:02d}-{tale['id']}"
    out = ROOT / "batches" / (batch_id + ".json")
    if out.is_file() and not force:
        d = json.loads(out.read_text(encoding="utf-8"))
        return {"no": episode, "id": batch_id, "title": d["title"],
                "file": out.name, "reused": True,
                "characters": [c["name"] for c in d.get("characters") or []],
                "source": d.get("story_source", "")}

    known_names = {c["name"] for c in cards}
    story = story_view(tale, known_names)
    prompt = wcs.script_prompt(story, cards, used_new, episode, batch_id)
    last = None
    d, meta = None, None
    for attempt in range(3):
        try:
            cand, m = text.chat_json(cfg, prompt, system=wc.SYSTEM,
                                     max_tokens=12000, timeout=420)
            # The model often emits a partial profile for an EXISTING card.
            # Fill it from the asset library instead of paying for a retry;
            # the stored card always wins so the visual anchor stays stable.
            by_name = {c["name"]: c for c in cards}
            for spec in cand.get("characters") or []:
                card = by_name.get(spec.get("name"))
                if not card:
                    continue
                profile = dict(card.get("profile") or {})
                for k, v in (spec.get("profile") or {}).items():
                    if v and not str(profile.get(k) or "").strip():
                        profile[k] = v
                for field, _label in characters.PROFILE_FIELDS:
                    if not str(profile.get(field) or "").strip():
                        profile[field] = "沿用既有角色设定"
                spec["profile"] = profile
            wcs.validate_series_story(cand, batch_id)
            d, meta = cand, m
            break
        except Exception as exc:
            last = exc
            log(f"  第 {episode:02d} 集第 {attempt+1} 次失败："
                f"{type(exc).__name__}: {str(exc)[:120]}")
            prompt += ("\n\n【上一次输出被拒绝】" + str(exc) +
                       "。请重新输出完整的合法 JSON，严格修正后再给。")
    if d is None or meta is None:
        raise RuntimeError(f"第 {episode:02d} 集剧本生成失败：{last}")

    known = {c["name"]: c["id"] for c in cards}
    for c in d["characters"]:
        cid = known.get(c["name"])
        c["existing"] = bool(cid)
        c["character_id"] = cid or ""
    d.update({
        "text_model": str(cfg.get("text_model") or text.DEFAULT_MODEL),
        "image_model": IMAGE_MODEL,
        "style_id": "sanguo-comic", "quality": "high",
        "portrait_size": "1024x1536", "scene_size": "1536x1024",
        "panel_size": "1536x1024", "max_cost_usd": 8.0,
        "series_id": sid, "episode": episode,
        "story_bank_no": int(tale["no"]),
        "_generation": {
            "written_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "text_model": meta.get("model"), "usage": meta.get("usage") or {},
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "source_of_story": "batches/story-bank.json",
        },
    })
    out.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    log(f"  第 {episode:02d} 集剧本就绪：{d['title']} · {out.name}")
    return {"no": episode, "id": batch_id, "title": d["title"],
            "file": out.name, "source": d.get("story_source"),
            "characters": [c["name"] for c in d["characters"]],
            "reused": False}


def build(cfg: dict, sid: str, tale_nos: list[int], *,
          used_new: list[str], force: bool, workers: int) -> dict:
    cards = known_cards(cfg)
    log(f"系列 {sid} · {len(tale_nos)} 集 · 已有角色卡 {len(cards)} 张 · "
        f"文本 {cfg.get('text_model') or text.DEFAULT_MODEL}")
    tales = [(ep, int(n), tale_by_no(int(n)))
             for ep, n in enumerate(tale_nos, 1)]
    results = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(write_one, cfg, n, tale, sid, ep, cards,
                            list(used_new), force): ep
                for ep, n, tale in tales}
        for fu in as_completed(futs):
            ep = futs[fu]
            results[ep] = fu.result()
    episodes = [results[i] for i in sorted(results)]
    manifest = {
        "id": sid,
        "title": f"三国小故事 · {sid.rsplit('-', 1)[-1].upper()} 批（10 集）",
        "planned_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "text_model": str(cfg.get("text_model") or text.DEFAULT_MODEL),
        "image_model": IMAGE_MODEL,
        "style_id": "sanguo-comic", "quality": "high",
        "constraint": "候选库故事；《三国演义》写过的亦收录，逐条标注演义归属",
        "source": "batches/story-bank.json",
        "episodes": episodes,
    }
    mf = ROOT / "batches" / (f"series-{sid}.json")
    mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                  encoding="utf-8")
    log(f"系列清单 {mf.name} · {len(episodes)} 集")
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description="从故事候选库写 10 集连环画剧本")
    ap.add_argument("--series", default="", help="系列 id，如 sanguo-tales-b01")
    ap.add_argument("--tales", default="", help="候选编号，逗号分隔")
    ap.add_argument("--queue", action="store_true", help="从 story-queue.json 取一批")
    ap.add_argument("--batch", type=int, default=0, help="配合 --queue 的批次号")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--force", action="store_true", help="覆盖已存在的剧本")
    args = ap.parse_args()

    cfg = config.load()
    if not config.has_key(cfg):
        raise SystemExit("没有可用的 API key。")
    used_new: list[str] = []
    if args.queue:
        q = json.loads(QUEUE.read_text(encoding="utf-8"))
        b = next((x for x in q["batches"] if int(x["no"]) == args.batch), None)
        if not b:
            raise SystemExit(f"队列里没有第 {args.batch} 批")
        sid, tale_nos = b["id"], [int(x) for x in b["tale_nos"]]
        used_new = list(q.get("used_new_characters") or [])
    else:
        if not args.series or not args.tales:
            raise SystemExit("需要 --series 和 --tales，或使用 --queue --batch N")
        sid = args.series
        tale_nos = [int(x) for x in args.tales.split(",") if x.strip()]

    manifest = build(cfg, sid, tale_nos, used_new=used_new,
                     force=args.force, workers=args.workers)
    print(json.dumps({"ok": True, "series": sid, "episodes": len(manifest["episodes"]),
                      "manifest": f"batches/series-{sid}.json"},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
