#!/usr/bin/env python3
"""Collect Three Kingdoms short-story candidates with the text model.

    python tools/collect_stories.py                      # all batches, 3 workers
    python tools/collect_stories.py --only wei,shu       # selected batches
    python tools/collect_stories.py --workers 1          # strictly serial
    python tools/collect_stories.py --show               # cached batches, no API
    python tools/collect_stories.py --force              # ignore caches

Only the text model (GPT-6.1 Sol by default) is called; no images are
generated. Every batch is cached separately under data/story-collect/, so
re-running costs nothing and a failed batch never loses the others.

Batches are deliberately small: a single long reasoning request tends to hang
on the local proxy, so several short ones are sent concurrently instead.
The offline build step (tools/build_story_bank.py) merges and de-duplicates.

The user relaxed the constraint: tales that DO appear in the Romance are
welcome too, as long as romance_check / romance_note records that honestly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import write_comic_series as wcs  # noqa: E402  (curated CANDIDATES lives here)
from studio import config, text  # noqa: E402

CACHE_DIR = ROOT / "data" / "story-collect"
PER_BATCH = 8
SYSTEM = (
    "你是汉末三国史料与《三国演义》回目的严谨考据助手。"
    "只依据真实史籍、古注与《三国演义》通行本，宁可少写也不要编造书名、"
    "卷次、回目或原文。所有输出必须是严格 JSON 对象本身，不要 Markdown、不要解释。"
)

# (cache key, source scope, editorial focus)
BATCHES = (
    ("wei", "《三国志·魏书》各本传，及裴松之注引《魏略》《典略》《魏氏春秋》"
            "《傅子》《世语》《魏武故事》《曹瞒传》《先贤行状》",
     "曹魏政权与汉末中原名士：君臣应对、吏治、孝行、清俭、用人、军中轶事。"
     "尽量避开曹操本人被反复书写的桥段，多写二线人物。"),
    ("shu", "《三国志·蜀书》各本传，及裴注引《襄阳记》《华阳国志》《蜀记》"
            "《零陵先贤传》《诸葛亮集》《益部耆旧传》",
     "蜀汉与益州人物：使节应对、学者风骨、隐逸辞征、治政细节、边地风俗。"),
    ("wu", "《三国志·吴书》各本传，及裴注引《江表传》《吴录》《吴书》"
           "《会稽典录》《吴历》《志林》",
     "孙吴与江东士族：少年捷悟、宴饮机辩、太子与宗室、地方循吏、海贸与交州。"),
    ("shishuo", "《世说新语》三十六门中涉及汉末三国人物的条目，及刘孝标注所引诸书",
     "名士言行：德行、言语、政事、文学、方正、雅量、识鉴、赏誉、品藻、"
     "规箴、捷悟、夙惠、容止、伤逝、巧艺、假谲、排调。每条须注明门类名。"),
    ("houhanshu", "《后汉书》《晋书》《资治通鉴》中汉末至三国人物列传与注文，"
                  "及《三国志》裴注引《汉晋春秋》《献帝春秋》《英雄记》",
     "汉末士人、党锢名士、经学与书法、医方、女性与家族、丧葬与交游，"
     "以及三国归入晋初的人物的早年轶事。"),
    ("artisans", "跨史料：各书《方技传》相关记载、裴注引《马先生传》"
                 "《博物志》《异物志》，以及《水经注》《齐民要术》所引汉魏旧事",
     "制度、器物与日常生活：屯田水利、机械织机、指南车与龙骨水车、医药、"
     "文书符节、饮食、儿童游戏、市井与工匠。人物可以不是名人。"),
    ("romance", "通行毛宗岗评改一百二十回本《三国演义》正文（含回目、人物追述、"
                "插叙的小故事），以及《三国志》等史籍中与之对应的记载",
     "专门收集【演义里也写过或提到过】的小故事，与前面几批互补。"
     "优先选非顶级名场面的次要情节：一句对白带出的往事、插叙补叙的旧事、"
     "小人物的一个动作、宴席或行军中的细节。每条须写明大致回目或情形。"),
    ("mixed", "《三国志》及裴注、《世说新语》《后汉书》《晋书》《华阳国志》"
              "《襄阳记》《江表传》《魏略》《典略》等，以及《三国演义》正文",
     "跨来源补漏：孝行、家风、师生、友朋、书信、器物、动物与市井、"
     "临终与托付、使者机辩、地方风物。演义写过的和没写过的都要，比例大致各半。"),
)

SCHEMA = (
    "{\n"
    '  "stories": [\n'
    '    {"title": "四到八字中文短标题",\n'
    '     "source": "史籍全称·卷/传名；若为裴注引书须写明『裴注引《X》』；'
    '若主要见于演义则写『《三国演义》第N回』并可附对应史料",\n'
    '     "quote": "一句可核查的原文摘录；无法确证时写『原文待核』",\n'
    '     "synopsis": "60 字以内梗概，说清谁做了什么、结果如何",\n'
    '     "characters": ["人物中文名"],\n'
    '     "region": "魏 / 蜀 / 吴 / 汉末 / 跨政权 之一",\n'
    '     "mood": "机智 / 诙谐 / 清廉 / 孝行 / 悲悯 / 勇毅 / 巧艺 / 直谏 / 悼亡 / 考据 之一",\n'
    '     "romance_check": "not_in_romance / in_romance / mentioned / similar / uncertain 之一",\n'
    '     "romance_note": "通行毛本120回《三国演义》是否写过：'
    'in_romance 要指出回目或情节位置；mentioned 要说明被谁的哪句话提及；'
    'similar 要说明相似但不同的回目；not_in_romance 要说明为何未载；'
    '不确定就标 uncertain，绝不许把不确定说成确定",\n'
    '     "visual_hook": "一句可供分镜的画面核心：地点、动作、关键道具",\n'
    '     "confidence": "high / medium / low 之一"}\n'
    "  ]\n"
    "}\n"
)

# Not a ban list: the user explicitly allows Romance-attested tales. These are
# just the most over-exposed set pieces, kept from crowding out smaller ones.
WELL_KNOWN = (
    "桃园结义、草船借箭、空城计、赤壁之战、三顾茅庐、刮骨疗毒、七步成诗、"
    "乐不思蜀、白衣渡江、木牛流马、割发代首、击鼓骂曹、左慈戏曹、蒋干盗书、"
    "杨修鸡肋、青梅煮酒、千里走单骑、单刀会、火烧连营、六出祁山、"
    "借东风、苦肉计、连环计、辕门射戟、赵云救阿斗、据水断桥、"
    "诸葛亮骂死王朗、死诸葛吓走生仲达、司马懿装病"
)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def parse_curated() -> list[str]:
    """Titles of the 24 already-vetted candidates, used to avoid duplicates."""
    out = []
    for line in wcs.CANDIDATES.strip().splitlines():
        m = re.match(r"\s*\d+\.\s*(.+?)\s*——", line)
        if m:
            out.append(m.group(1).strip())
    return out


def used_titles() -> list[str]:
    """Stories already turned into comics must not be collected again."""
    out = ["鼠啮马鞍", "曹冲智救库吏"]
    for f in sorted((ROOT / "batches").glob("comic-*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if d.get("title"):
            out.append(str(d["title"]))
    return list(dict.fromkeys(out))


def build_prompt(key: str, scope: str, focus: str, exclude: list[str]) -> str:
    ex = "、".join(exclude) if exclude else "（无）"
    return (
        "任务：为「三国小故事」连环画系列扩充选题候选库。"
        f"本批只负责【{scope}】范围内的来源。\n\n"
        "【硬性要求】\n"
        f"1. 恰好 {PER_BATCH} 个互不相同的故事，全部发生在汉末至三国时期"
        "（含紧接其后的魏晋之际人物早年事），且见于你负责的这批来源。\n"
        f"2. 取材侧重：{focus}\n"
        "3. 【《三国演义》里写过的故事同样可以收】，不再排除。"
        "但每一条都必须如实标注 romance_check："
        "in_romance / mentioned / similar / not_in_romance / uncertain，"
        "并在 romance_note 里写清回目或情形；不确定就标 uncertain。\n"
        "   下面这些是最家喻户晓的桥段，本批最多收 1 个，"
        "把名额留给更小、更有画面感的细节：\n"
        + WELL_KNOWN + "\n"
        "4. 每条 source 必须写全史籍与篇名（或演义回目）；quote 尽量给一句原文，"
        "记不准就老实写『原文待核』并把 confidence 标 low。"
        "【严禁编造书名、传名、回目或原文】。\n"
        "5. 优先小故事、有画面感的细节；人物尽量分散，"
        "不要集中在曹操、诸葛亮、关羽身上。\n"
        "6. 下列标题已收集或已拍成连环画，不要重复（同一人物的不同故事可以）：\n"
        + ex + "\n\n"
        f"【输出 JSON schema（严格，只输出 JSON 对象本身；stories 必须 {PER_BATCH} 条）】\n"
        + SCHEMA
    )


def call_json(cfg: dict, prompt: str, tries: int = 2):
    last = None
    for i in range(tries):
        try:
            return text.chat_json(cfg, prompt, system=SYSTEM,
                                  max_tokens=8000, timeout=240)
        except Exception as exc:
            last = exc
            log(f"  第 {i+1} 次失败：{type(exc).__name__}: {str(exc)[:160]}")
            if i < tries - 1:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"文本模型连续 {tries} 次失败：{last}")


def collect_one(cfg: dict, key: str, scope: str, focus: str,
                exclude: list[str], force: bool) -> tuple[str, int]:
    out = CACHE_DIR / f"{key}.json"
    if out.is_file() and not force:
        try:
            d = json.loads(out.read_text(encoding="utf-8"))
            log(f"[{key}] 复用缓存 {len(d.get('stories') or [])} 条")
            return key, len(d.get("stories") or [])
        except (ValueError, OSError):
            pass
    prompt = build_prompt(key, scope, focus, exclude)
    log(f"[{key}] 请求文本模型 …")
    data, meta = call_json(cfg, prompt)
    stories = data.get("stories") if isinstance(data, dict) else None
    if not isinstance(stories, list) or not stories:
        raise RuntimeError(f"[{key}] 返回格式不对：缺 stories 数组")
    payload = {
        "batch": key, "scope": scope, "focus": focus, "stories": stories,
        "_meta": {
            "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "model": meta.get("model"), "usage": meta.get("usage") or {},
            "raw_chars": meta.get("raw_chars"),
            "finish_reason": meta.get("finish_reason"),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "prompt_chars": len(prompt),
        },
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    u = meta.get("usage") or {}
    log(f"[{key}] 收到 {len(stories)} 条 · tokens {u.get('total_tokens', '?')} "
        f"· 已写 {out.name}")
    return key, len(stories)


def main() -> None:
    ap = argparse.ArgumentParser(description="用 GPT-6.1 Sol 收集三国小故事候选")
    ap.add_argument("--only", default="",
                    help="只采集指定批次，逗号分隔，如 romance,mixed")
    ap.add_argument("--workers", type=int, default=3, help="并发批次数，默认 3")
    ap.add_argument("--force", action="store_true", help="忽略缓存重新采集")
    ap.add_argument("--show", action="store_true", help="只报告缓存，不调用模型")
    args = ap.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if args.show:
        for key, _, _ in BATCHES:
            f = CACHE_DIR / f"{key}.json"
            if f.is_file():
                d = json.loads(f.read_text(encoding="utf-8"))
                print(f"{key:>12}: {len(d.get('stories') or [])} 条  "
                      f"{d.get('_meta', {}).get('model', '?')}")
            else:
                print(f"{key:>12}: （未采集）")
        return

    cfg = config.load()
    if not config.has_key(cfg):
        raise SystemExit("没有可用的 API key。")
    model = str(cfg.get("text_model") or text.DEFAULT_MODEL)
    curated = parse_curated()
    used = used_titles()
    only = {s.strip() for s in args.only.split(",") if s.strip()} if args.only else set()
    batches = [b for b in BATCHES if not only or b[0] in only]
    # Static exclusion list: concurrent batches must not depend on each other's
    # half-written caches. De-duplication happens in build_story_bank.py.
    exclude = list(dict.fromkeys(curated + used))
    log(f"文本模型 {model} · 目标 {len(batches)} 批 × {PER_BATCH} 条 · "
        f"并发 {args.workers} · 基线 {len(curated)} 条 / 已拍 {len(used)} 个")

    results: dict[str, int] = {}
    if args.workers <= 1:
        for b in batches:
            try:
                k, n = collect_one(cfg, *b, exclude, args.force)
                results[k] = n
            except Exception as exc:
                log(f"[{b[0]}] 失败：{type(exc).__name__}: {str(exc)[:200]}")
                results[b[0]] = 0
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(collect_one, cfg, *b, exclude, args.force): b[0]
                    for b in batches}
            for fu in as_completed(futs):
                key = futs[fu]
                try:
                    k, n = fu.result()
                    results[k] = n
                except Exception as exc:
                    log(f"[{key}] 失败：{type(exc).__name__}: {str(exc)[:200]}")
                    results[key] = 0

    ok = [k for k, n in results.items() if n]
    bad = [k for k, n in results.items() if not n]
    log(f"完成 {len(ok)}/{len(batches)} 批 · 原始候选 {sum(results.values())} 条")
    if bad:
        log("未完成批次：" + "、".join(sorted(bad)) +
            "（可重跑：python tools/collect_stories.py --only " +
            ",".join(sorted(bad)) + "）")


if __name__ == "__main__":
    main()
