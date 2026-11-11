#!/usr/bin/env python3
"""Queue controller: turn every story-bank candidate into comics, 10 at a time.

    python tools/run_story_queue.py --prepare       # build the queue (no API)
    python tools/run_story_queue.py --status        # progress + spend
    python tools/run_story_queue.py --run-next      # write scripts + generate images
    python tools/run_story_queue.py --batch 3       # a specific batch
    python tools/run_story_queue.py --all           # keep going until all done

Everything is resumable: batches/story-queue.json records per-batch status,
cost and timestamps; existing scripts and indexed images are always skipped,
so a killed run never pays for the same image twice.

Each batch reuses the already-proven pipeline:
    write_comic_from_bank.py  ->  10 six-panel scripts + series manifest
    generate_comic_series.py  ->  shared portraits, scenes, panels, ZIP, cloud sync
    build_story_bank.py       ->  mark newly produced tales as "used"
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BANK = ROOT / "batches" / "story-bank.json"
QUEUE = ROOT / "batches" / "story-queue.json"
BATCH_SIZE = 10
SERIES_PREFIX = "sanguo-tales-q"


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load() -> dict:
    return json.loads(QUEUE.read_text(encoding="utf-8"))


def save(q: dict) -> None:
    tmp = QUEUE.with_suffix(".tmp")
    tmp.write_text(json.dumps(q, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(QUEUE)


def prepare(reset: bool = False) -> dict:
    bank = json.loads(BANK.read_text(encoding="utf-8"))
    todo = [r for r in bank["stories"] if r["status"] == "candidate"]
    todo.sort(key=lambda r: int(r["no"]))
    old = {}
    if QUEUE.is_file() and not reset:
        old = {int(b["no"]): b for b in load().get("batches") or []}
    batches = []
    for i in range(0, len(todo), BATCH_SIZE):
        chunk = todo[i:i + BATCH_SIZE]
        no = i // BATCH_SIZE + 1
        sid = f"{SERIES_PREFIX}{no:02d}"
        prev = old.get(no) or {}
        batches.append({
            "no": no, "id": sid,
            "series_file": f"batches/series-{sid}.json",
            "tale_nos": [int(r["no"]) for r in chunk],
            "titles": [r["title"] for r in chunk],
            "status": prev.get("status", "pending"),
            "cost_usd": float(prev.get("cost_usd") or 0),
            "assets": int(prev.get("assets") or 0),
            "finished_at": prev.get("finished_at", ""),
            "error": prev.get("error", ""),
        })
    q = {
        "id": "sanguo-tales-full", "title": "三国小故事 · 全量候选连环画",
        "created_at": (load().get("created_at") if QUEUE.is_file() and not reset
                       else datetime.now().astimezone().isoformat(timespec="seconds")),
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "batch_size": BATCH_SIZE,
        "total_tales": len(todo), "total_batches": len(batches),
        "used_new_characters": (load().get("used_new_characters") or [])
                               if QUEUE.is_file() and not reset else [],
        "budget_spent_usd": round(sum(b["cost_usd"] for b in batches), 4),
        "batches": batches,
    }
    save(q)
    log(f"队列就绪：{len(todo)} 条候选 · {len(batches)} 批 × {BATCH_SIZE} 集")
    return q


def status(q: dict) -> None:
    done = [b for b in q["batches"] if b["status"] == "done"]
    running = [b for b in q["batches"] if b["status"] == "running"]
    failed = [b for b in q["batches"] if b["status"] == "failed"]
    print(json.dumps({
        "total_tales": q["total_tales"],
        "total_batches": q["total_batches"],
        "done_batches": len(done), "running": [b["no"] for b in running],
        "failed": [b["no"] for b in failed],
        "episodes_done": len(done) * q["batch_size"],
        "budget_spent_usd": q.get("budget_spent_usd", 0),
        "assets": sum(b.get("assets") or 0 for b in done),
        "next": next((b["no"] for b in q["batches"]
                      if b["status"] in ("pending", "failed", "running")), None),
    }, ensure_ascii=False, indent=2))


def run(cmd: list[str], what: str) -> int:
    log("执行：" + " ".join(cmd))
    rc = subprocess.run(cmd, cwd=str(ROOT)).returncode
    if rc:
        log(f"!! {what} 返回 rc={rc}")
    return rc


def run_batch(q: dict, b: dict, workers: int, skip_scripts: bool) -> bool:
    sid = b["id"]
    no = int(b["no"])
    b["status"] = "running"
    b["started_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    save(q)
    log(f"===== 第 {no}/{q['total_batches']} 批 {sid} 开始 · "
        f"{len(b['tale_nos'])} 集 =====")

    if not skip_scripts:
        rc = run([sys.executable, "-u", "tools/write_comic_from_bank.py",
                  "--queue", "--batch", str(no), "--workers", "3"], "写剧本")
        if rc:
            b["status"], b["error"] = "failed", "剧本生成失败"
            save(q)
            return False
    series_file = f"batches/series-{sid}.json"
    rc = run([sys.executable, "-u", "tools/generate_comic_series.py",
              "--series", series_file, "--workers", str(workers),
              "--fix-degraded"], "生成资产")
    if rc:
        b["status"], b["error"] = "failed", f"资产生成 rc={rc}"
        save(q)
        return False

    # Read back the series manifest for cost / asset totals.
    cost, assets = 0.0, 0
    target = Path("D:/gdrive.photo/ai-assets/all") / sid / "series-manifest.json"
    try:
        sm = json.loads(target.read_text(encoding="utf-8"))
        cost = float(sm.get("total_cost_usd") or 0)
        assets = int(sm.get("total_assets") or 0)
    except (OSError, ValueError):
        log("警告：系列 manifest 未找到，成本记为 0")
    b.update({
        "status": "done", "cost_usd": round(cost, 4), "assets": assets,
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "error": "", "series_manifest": str(target),
        "read": f"http://127.0.0.1:8777/pack/{sid}/index.html",
    })
    q["budget_spent_usd"] = round(
        sum(x.get("cost_usd") or 0 for x in q["batches"]), 4)
    # Keep a cross-batch character-name ledger so later scripts reuse the same
    # names (and therefore the same portrait assets) for recurring figures.
    names = q.setdefault("used_new_characters", [])
    sf = ROOT / "batches" / f"series-{sid}.json"
    if sf.is_file():
        for ep in (json.loads(sf.read_text(encoding="utf-8")).get("episodes") or []):
            for name in ep.get("characters") or []:
                if name not in names:
                    names.append(name)
    save(q)

    # Rebuild the story bank so the 10 newly produced tales become "used".
    run([sys.executable, "tools/build_story_bank.py"], "重建故事库")
    log(f"===== 第 {no} 批完成 · {assets} 张资产 · ${cost:.3f} · "
        f"累计 ${q['budget_spent_usd']:.3f} =====")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="80 集候选全量分批队列")
    ap.add_argument("--prepare", action="store_true", help="生成/刷新队列，不调用 API")
    ap.add_argument("--reset", action="store_true", help="配合 --prepare 清空进度")
    ap.add_argument("--status", action="store_true", help="只打印进度与花费")
    ap.add_argument("--run-next", action="store_true", help="跑下一个未完成批次")
    ap.add_argument("--batch", type=int, default=0, help="跑指定批次")
    ap.add_argument("--all", action="store_true", help="依次跑完全部批次")
    ap.add_argument("--workers", type=int, default=3, help="图像生成并发数")
    ap.add_argument("--skip-scripts", action="store_true",
                    help="剧本已存在时跳过写作阶段")
    args = ap.parse_args()

    if args.prepare or not QUEUE.is_file():
        q = prepare(reset=args.reset)
        if args.prepare and not (args.status or args.run_next or args.all or args.batch):
            status(q)
            return
    else:
        q = load()
    if args.status and not (args.run_next or args.all or args.batch):
        status(q)
        return

    todo = []
    if args.batch:
        b = next((x for x in q["batches"] if int(x["no"]) == args.batch), None)
        if not b:
            raise SystemExit(f"没有第 {args.batch} 批")
        todo = [b]
    elif args.run_next or args.all:
        nxt = next((x for x in q["batches"]
                    if x["status"] in ("pending", "failed", "running")), None)
        if not nxt:
            log("全部批次已完成")
            status(q)
            return
        todo = [nxt] if not args.all else [
            x for x in q["batches"]
            if x["status"] in ("pending", "failed", "running")]
    if not todo:
        status(q)
        return

    for b in todo:
        ok = run_batch(q, b, args.workers, args.skip_scripts)
        if not ok:
            log(f"第 {b['no']} 批失败，停止队列。修正后可用 "
                f"--batch {b['no']} 单独重试。")
            break
    status(q)


if __name__ == "__main__":
    main()
