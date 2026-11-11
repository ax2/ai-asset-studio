#!/usr/bin/env python3
"""Review and complete the source material of story-bank candidates.

    python tools/review_stories.py                # review entries with gaps
    python tools/review_stories.py --force        # ignore caches, re-review
    python tools/review_stories.py --show         # report cached reviews

Zero image cost: only the text model is used. Reviews are cached per small
batch under data/story-reviews/, then merged into batches/story-reviews.json
for the fully offline build_story_bank.py to consume.

Only entries that actually need work are reviewed:
  * the 24 curated baseline entries (their quote is a placeholder), and
  * entries currently marked needs_review.
Everything else already carries a source, quote and Romance verdict from the
collection pass and is left untouched, so a review can never corrupt a quote
that was already good.
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

from studio import config, text  # noqa: E402

BANK = ROOT / "batches" / "story-bank.json"
TOC = ROOT / "batches" / "romance-toc.json"
CACHE = ROOT / "data" / "story-reviews"
OUT = ROOT / "batches" / "story-reviews.json"
PER_BATCH = 6
SYSTEM = (
    "你是汉末三国史料与《三国演义》回目的严谨考据助手。"
    "宁可写『原文待核』，也绝不编造书名、卷次、传名、回目或原文。"
    "只输出严格 JSON 对象本身，不要 Markdown、不要解释。"
)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_bank() -> list[dict]:
    return json.loads(BANK.read_text(encoding="utf-8"))["stories"]


def needs_review(r: dict) -> bool:
    placeholder = (r.get("origin") == "curated"
                   or str(r.get("quote") or "").startswith("原文见")
                   or r.get("quote") in ("", "原文待核"))
    return (placeholder or r.get("status") == "needs_review"
            or r.get("romance_check") == "uncertain")


def related_chapters(chapters: list[str], r: dict) -> str:
    keys = [c for c in (r.get("characters") or []) if len(c) >= 2]
    keys.append(r["title"][:2])
    hits = [c for c in chapters if any(k in c.split(" ", 1)[-1] for k in keys)]
    return "；".join(hits[:8]) or "（120 回回目中未见相关字样）"


def prompt_for(rows: list[dict], chapters: list[str]) -> str:
    entries = []
    for r in rows:
        entries.append({
            "no": r["no"], "title": r["title"], "source": r["source"],
            "quote_now": r["quote"], "synopsis": r["synopsis"],
            "characters": r.get("characters") or [],
            "romance_check_now": r["romance_check"],
            "romance_note_now": r["romance_note"],
        })
    toc = "；".join(related_chapters(chapters, r) or "" for r in rows)
    return (
        "任务：逐条复核并补齐下列三国小故事候选的【史料出处】与【《三国演义》归属】。\n\n"
        "【待复核条目（JSON）】\n" + json.dumps(entries, ensure_ascii=False, indent=1) + "\n\n"
        "【120 回中与这些条目相关的回目片段】\n" + toc[:2400] + "\n\n"
        "【硬性要求】\n"
        "1. 每条都要给一条 reviews 记录，字段名严格如下，条数与输入相同。\n"
        "2. `source`：原样保留，只有当书名/传名确有明显错误时才修正。\n"
        "3. `quote`：若输入 quote_now 以「原文见」开头、为「原文待核」或为空，"
        "请补一句该史料中可核查的原文摘录（不超过 45 字，尽量逐字）；"
        "确实记不准就填「原文待核」并把 quote_status 设为 unverified。"
        "【若 quote_now 已是正常原文摘录，请原样返回，不要改写】。\n"
        "4. `quote_status`：attested（有具体原文）/ unverified（待核）。\n"
        "5. `romance_check` 只能是 not_in_romance / in_romance / mentioned / similar / uncertain。"
        "in_romance 要指出回目；mentioned 要说明被谁提及；not_in_romance 要说明为何未载。"
        "实在无法判定就填 uncertain，并在 romance_note 里写清待核的具体点，不要含糊。\n"
        "6. `romance_note`：一句到两句具体依据，禁止「未详」「待考」这类空话。\n"
        "7. `confidence`：high / medium / low，自评这一条出处与判定的可靠程度。\n"
        "8. `verification`：一句话说明这次核了什么、改了什么。\n"
        "9. 不要改 title、no、synopsis、characters。\n\n"
        "【输出 JSON schema，只输出对象本身】\n"
        '{"reviews":[\n'
        '  {"no":1,"title":"...","source":"...","quote":"...",\n'
        '   "quote_status":"attested","romance_check":"not_in_romance",\n'
        '   "romance_note":"...","confidence":"high","verification":"..."}\n'
        "]}\n"
    )


def call(prompt: str, cfg: dict, tries: int = 2):
    last = None
    for i in range(tries):
        try:
            return text.chat_json(cfg, prompt, system=SYSTEM,
                                  max_tokens=7000, timeout=240)
        except Exception as exc:
            last = exc
            log(f"  第 {i+1} 次失败：{type(exc).__name__}: {str(exc)[:150]}")
    raise RuntimeError(f"文本模型连续 {tries} 次失败：{last}")


def review_batch(cfg: dict, idx: int, rows: list[dict],
                 chapters: list[str], force: bool) -> tuple[int, int]:
    out = CACHE / f"rev-{idx:02d}.json"
    if out.is_file() and not force:
        return idx, len(json.loads(out.read_text(encoding="utf-8")).get("reviews") or [])
    p = prompt_for(rows, chapters)
    log(f"[rev-{idx:02d}] 复核 {len(rows)} 条 …")
    data, meta = call(p, cfg)
    reviews = data.get("reviews") if isinstance(data, dict) else None
    if not isinstance(reviews, list) or not reviews:
        raise RuntimeError(f"[rev-{idx:02d}] 返回格式不对")
    payload = {
        "batch": idx, "reviews": reviews,
        "_meta": {
            "reviewed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "model": meta.get("model"), "usage": meta.get("usage") or {},
            "prompt_sha256": hashlib.sha256(p.encode("utf-8")).hexdigest(),
        },
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    log(f"[rev-{idx:02d}] 完成 {len(reviews)} 条 · 已写 {out.name}")
    return idx, len(reviews)


def main() -> None:
    ap = argparse.ArgumentParser(description="用文本模型复核候选故事资料")
    ap.add_argument("--force", action="store_true", help="忽略缓存重新复核")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--show", action="store_true", help="只报告缓存")
    args = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    if args.show:
        for f in sorted(CACHE.glob("rev-*.json")):
            print(f.name, len(json.loads(f.read_text(encoding="utf-8")).get("reviews") or []))
        return

    rows = [r for r in load_bank() if needs_review(r)]
    chapters = json.loads(TOC.read_text(encoding="utf-8")).get("chapters") or []
    chunks = [rows[i:i + PER_BATCH] for i in range(0, len(rows), PER_BATCH)]
    log(f"需复核 {len(rows)} 条 · {len(chunks)} 批 · 并发 {args.workers}")
    cfg = config.load()
    if not config.has_key(cfg):
        raise SystemExit("没有可用的 API key。")

    results = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = {pool.submit(review_batch, cfg, i + 1, c, chapters, args.force): i + 1
                for i, c in enumerate(chunks)}
        for fu in as_completed(futs):
            idx = futs[fu]
            try:
                k, n = fu.result()
                results[k] = n
            except Exception as exc:
                log(f"[rev-{idx:02d}] 失败：{type(exc).__name__}: {str(exc)[:180]}")
                results[idx] = 0

    merged = {}
    usage_total = 0
    for f in sorted(CACHE.glob("rev-*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        for r in d.get("reviews") or []:
            merged[str(r.get("no"))] = r
        usage_total += int(((d.get("_meta") or {}).get("usage") or {}).get("total_tokens") or 0)
    OUT.write_text(json.dumps({
        "reviewed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "model": str(cfg.get("text_model") or text.DEFAULT_MODEL),
        "scope": "curated baseline + needs_review entries",
        "target_count": len(rows), "review_count": len(merged),
        "usage_total_tokens": usage_total,
        "reviews": merged,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ok = sum(1 for v in results.values() if v)
    log(f"完成 {ok}/{len(chunks)} 批 · 汇总 {len(merged)} 条到 {OUT.name}")
    bad = [k for k, v in results.items() if not v]
    if bad:
        log("未完成批次：" + "、".join(f"rev-{k:02d}" for k in sorted(bad)) +
            "（可重跑本脚本，已缓存的批次会跳过）")


if __name__ == "__main__":
    main()
