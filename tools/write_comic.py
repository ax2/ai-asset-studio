#!/usr/bin/env python3
"""Use GPT-6.1 Sol to choose a Three Kingdoms story absent from the Romance
and write a reproducible six-panel comic script.

    python tools/write_comic.py            # generate batches/<id>.json
    python tools/write_comic.py --show     # print the plan without writing

Only the text model is called here; no images and no money beyond tokens.
The output is a plain JSON manifest consumed by tools/generate_comic.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio import characters, config, text  # noqa: E402

PROFILE_KEYS = [k for k, _ in characters.PROFILE_FIELDS]

SYSTEM = (
    "你是一位精通汉末三国史料与连环画脚本的历史小说作者。你严格区分"
    "《三国演义》（明代小说）与《三国志》《魏略》《世说新语》等史料。"
    "你只输出一个严格的 JSON 对象，不输出解释文字、markdown 标题或代码块标记。"
    "所有 JSON 字符串必须合法转义，不要出现未闭合的引号。"
)

CANDIDATES = (
    "1. 曹冲称象 ——《三国志·魏书·邓哀王冲传》：孙权致巨象，太祖欲知其斤重，"
    "群臣莫能出其理；冲曰『置象大船之上，而刻其水痕所至，称物以载之，则校可知矣』。\n"
    "2. 陆绩怀橘 ——《三国志·吴书·陆绩传》：绩年六岁见袁术，私藏三枚橘子欲归遗母。\n"
    "3. 曹冲智救库吏 —— 曹操马鞍在库为鼠所啮，库吏惧死，曹冲以刀穿单衣如鼠啮状，"
    "诱曹操自释之。\n"
    "4. 孙权庱亭射虎 ——《三国志·吴主传》：权亲乘马射虎于庱亭，马为虎所伤，"
    "乃投以双戟，虎却废。\n"
)

SCHEMA = (
    "{\n"
    '  "id": "comic-xxx",                        // 小写英文+连字符，作为批次 id\n'
    '  "title": "中文标题",\n'
    '  "subtitle": "一句副标题",\n'
    '  "story_source": "史料出处 + 一段原文摘录",\n'
    '  "why_not_in_romance": "明确说明为何《三国演义》未收录此故事，不要含糊",\n'
    '  "synopsis": "150 字以内梗概",\n'
    '  "characters": [\n'
    '    {"name":"中文名", "slug":"pinyin-slug", "existing": false,\n'
    '     "role":"身份",\n'
    '     "profile": {"age":"","gender":"","role":"","era":"","face":"","eyes":"",'
    '"skin":"","build":"","hair":"","marks":"","outfit":"","accessories":"",'
    '"weapon":"","palette":"","temperament":"","art_style":"","negative":""},\n'
    '     "pose":"英文立绘姿势描述", "tags":["三国","..."]}\n'
    '  ],\n'
    '  "scenes": [\n'
    '    {"id":"scene-01", "name":"中文场景名", "description":"中文场景描述",\n'
    '     "prompt_extra":"英文场景提示词补充（环境、光线、构图）",\n'
    '     "time_of_day":"", "mood":""}\n'
    '  ],\n'
    '  "panels": [\n'
    '    {"no":1, "scene_id":"scene-01", "caption":"中文旁白（25字内）",\n'
    '     "dialogue":[{"speaker":"人物名","line":"中文对白"}],\n'
    '     "visual":"英文画面描述，供图像模型直接使用",\n'
    '     "characters":["本格出现的人物名"], "composition":"wide|medium|close"}\n'
    '  ],\n'
    '  "visual_rules":"英文全局画面规则",\n'
    '  "notes":"创作说明：史料骨架与文学化扩写的边界"\n'
    "}\n"
)


def build_prompt(cards: list[dict]) -> str:
    known = []
    for c in cards:
        if c.get("name") not in ("刘备", "关羽", "张飞", "诸葛亮", "赵云", "曹操"):
            continue
        p = c.get("profile") or {}
        known.append("- %s（%s）：%s；配色 %s" % (
            c["name"], p.get("role", ""), str(p.get("outfit", ""))[:70],
            str(p.get("palette", ""))[:40]))
    known_text = "\n".join(known) or "（暂无）"
    return (
        "任务：为一个「三国」题材的连环画创作完整脚本。\n\n"
        "【硬性要求】\n"
        "1. 故事必须发生在汉末三国时期，必须见于《三国志》或其注引史料、"
        "《世说新语》等，且【《三国演义》里没有写过】。绝对不要选桃园结义、草船借箭、"
        "空城计、赤壁、三顾茅庐、刮骨疗毒、七步成诗、乐不思蜀、白衣渡江、"
        "木牛流马等演义名场面。\n"
        "2. 优先从下列候选中选一个（也可另提一个同样符合条件的），在 "
        "why_not_in_romance 里说清依据：\n" + CANDIDATES + "\n"
        "3. 尽量复用下面这些【已有角色资产】，其中曹操尤其适合与曹冲故事搭配；"
        "已在资产库里的角色请标 existing=true 并且不要重造外貌。\n" + known_text + "\n"
        "4. 需要出现的、资产库里没有的人物，请新建完整角色设定（profile 字段见 schema），"
        "人物数量控制在 3 个以内，避免过多立绘成本。\n"
        "5. 场景 2~3 个，每个场景是一张独立的、不含人物的场景概念图。\n"
        "6. 恰好 6 格分镜，起承转合完整；每格要有画面描述、中文旁白、可选对白。"
        "旁白与对白单独存字段，【画面里绝对不能出现任何文字、字母、印章、字幕、边框、分格线】"
        "，文字后期由 HTML 排版叠加。\n"
        "7. 视觉风格沿用已有「三国英雄立绘」：premium Three Kingdoms historical-fantasy "
        "game concept art, highly detailed painterly digital illustration, historical "
        "pigment palette, warm parchment / plain uniform background, even lighting, "
        "fine armour and textile texture, orthographic full-body character treatment。\n"
        "8. visual 与 prompt_extra 用英文写，且每条都自带 "
        "'no text, no letters, no captions, no watermark, no panel borders, no split lines'。"
        "人物外貌要与已有角色一致，不要换服装、发型或武器。\n\n"
        "【输出 JSON schema（严格遵守键名，不要增删顶层键）】\n" + SCHEMA
    )


def validate(d: dict) -> None:
    required = ("id", "title", "story_source", "why_not_in_romance", "synopsis",
                "characters", "scenes", "panels", "visual_rules")
    for k in required:
        if not d.get(k):
            raise ValueError("剧本缺少字段：" + k)
    if not isinstance(d["characters"], list) or not d["characters"]:
        raise ValueError("characters 不能为空")
    if not (2 <= len(d["scenes"]) <= 4):
        raise ValueError("scenes 需要 2~4 个")
    if len(d["panels"]) != 6:
        raise ValueError("panels 必须恰好 6 格")
    for i, p in enumerate(d["panels"], 1):
        if p.get("no") != i:
            raise ValueError("分镜编号不连续：%r" % p.get("no"))
        for k in ("scene_id", "caption", "visual", "characters"):
            if not p.get(k):
                raise ValueError("第 %d 格缺少 %s" % (i, k))
    scene_ids = {s["id"] for s in d["scenes"]}
    for p in d["panels"]:
        if p["scene_id"] not in scene_ids:
            raise ValueError("分镜引用了不存在的场景：" + p["scene_id"])
    names = {c["name"] for c in d["characters"]}
    for p in d["panels"]:
        for n in p["characters"]:
            if n not in names:
                raise ValueError("分镜出现未定义的人物：" + n)
    for c in d["characters"]:
        for k in PROFILE_KEYS:
            if k not in (c.get("profile") or {}):
                raise ValueError("%s 的 profile 缺少 %s" % (c["name"], k))


def main() -> None:
    ap = argparse.ArgumentParser(description="用 GPT-6.1 Sol 创作连环画脚本")
    ap.add_argument("--show", action="store_true", help="只打印，不写文件")
    ap.add_argument("--out", default="", help="输出文件名（默认用剧本 id）")
    args = ap.parse_args()

    cfg = config.load()
    if not config.has_key(cfg):
        raise SystemExit("没有可用的 API key。")
    prompt = build_prompt(characters.list_cards(cfg))
    print("调用文本模型 %s 创作剧本…" % (cfg.get("text_model") or text.DEFAULT_MODEL),
          flush=True)
    data, meta = text.chat_json(cfg, prompt, system=SYSTEM, max_tokens=12000)
    validate(data)

    # Enrich with the execution context so the manifest is self-contained.
    known = {c["name"]: c["id"] for c in characters.list_cards(cfg)}
    for c in data["characters"]:
        cid = known.get(c["name"])
        c["existing"] = bool(cid)
        c["character_id"] = cid or ""
    data["text_model"] = str(cfg.get("text_model") or text.DEFAULT_MODEL)
    data["image_model"] = "gpt-image-2.6-sunburst"
    data["style_id"] = "sanguo-comic"
    data["quality"] = "high"
    data["portrait_size"] = "1024x1536"
    data["scene_size"] = "1536x1024"
    data["panel_size"] = "1536x1024"
    data["max_cost_usd"] = 8.0
    data["_generation"] = {
        "written_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "text_model": meta["model"], "usage": meta["usage"],
        "raw_chars": meta["raw_chars"], "finish_reason": meta["finish_reason"],
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "prompt_chars": len(prompt),
    }

    print(json.dumps({
        "id": data["id"], "title": data["title"],
        "source": data["story_source"][:160],
        "why_not_in_romance": data["why_not_in_romance"][:160],
        "characters": [(c["name"], "已有" if c["existing"] else "新建")
                       for c in data["characters"]],
        "scenes": [s["name"] for s in data["scenes"]],
        "panels": len(data["panels"]),
        "usage": meta["usage"], "finish_reason": meta["finish_reason"],
    }, ensure_ascii=False, indent=2))

    if not args.show:
        name = args.out or (data["id"] + ".json")
        out = ROOT / "batches" / name
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        print("已写入", out)


if __name__ == "__main__":
    main()
