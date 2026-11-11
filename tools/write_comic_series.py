#!/usr/bin/env python3
"""Use GPT-6.1 Sol to plan and write a 10-episode comic series.

    python tools/write_comic_series.py                 # plan + write 10 scripts
    python tools/write_comic_series.py --show         # plan only, no files
    python tools/write_comic_series.py --force        # rewrite existing scripts

Phase 1 asks the text model for ten distinct, Romance-absent historical tales.
Phase 2 writes one full six-panel script per tale, reusing existing character
cards and giving every new character a complete 17-field profile. Outputs:

    batches/comic-s01-<slug>.json ... batches/comic-s10-<slug>.json
    batches/series-sanguo-tales-10.json     (series manifest for the generator)

Only the text model is called here; no images are generated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import write_comic as wc  # noqa: E402
from studio import characters, config, text  # noqa: E402

SERIES_ID = "sanguo-tales-10"
SERIES_TITLE = "三国史料小故事 · 十部连环画"
COUNT = 10

# Curated, vetted pool: all are attested in histories / note-commentaries and
# are not dramatised in the 120-chapter Romance. The model may add its own with
# a documented source, but must justify the absence explicitly.
CANDIDATES = (
    "1. 陆绩怀橘 ——《三国志·吴书·陆绩传》：绩年六岁见袁术，私藏三枚橘子欲归遗母。\n"
    "2. 曹冲称象 ——《三国志·魏书·邓哀王冲传》：置象大船之上，刻其水痕，称物以载之。\n"
    "3. 孙权庱亭射虎 ——《三国志·吴主传》：权亲乘马射虎，马为虎伤，投双戟却之。\n"
    "4. 枣祗建屯田 ——《三国志·武帝纪》裴注引《魏武故事》：枣祗建置屯田，许下积谷。\n"
    "5. 床头捉刀人 ——《世说新语·容止》：崔琰代曹操见匈奴使，魏武追杀此使。\n"
    "6. 蔡邕倒屣迎王粲 ——《三国志·魏书·王粲传》：粲至长安，邕倒屣迎之，尽以书籍文章相付。\n"
    "7. 简雍讽禁酒具 ——《三国志·蜀书·简雍传》：天旱禁酒，酿具同罪；雍指道上男女曰『彼有其具』。\n"
    "8. 伊籍使吴 ——《三国志·蜀书·伊籍传》：孙权问『劳事无道之君乎』，籍答『一拜一起，未足为劳』。\n"
    "9. 费祎围棋办贼 ——《三国志·蜀书·费祎传》裴注引《祎别传》：来敏求共围棋，羽檄交驰而祎从容对局。\n"
    "10. 秦宓论天 ——《三国志·蜀书·秦宓传》：吴使张温问『天有头乎』『有耳乎』『有足乎』，宓随问而答。\n"
    "11. 刘巴不与张飞共宿 ——《三国志·蜀书·刘巴传》裴注引《零陵先贤传》：巴不与武人共语，诸葛亮为解。\n"
    "12. 老牛舐犊 ——《后汉书·杨彪传》／《魏氏春秋》：操问彪『何瘦之甚』，彪曰『犹怀老牛舐犊之爱』。\n"
    "13. 元龙百尺楼 ——《三国志·魏书·陈登传》：许汜论陈登无客主之礼，备曰『欲卧百尺楼上，卧君于地』。\n"
    "14. 管宁割席 ——《世说新语·德行》：宁与华歆共园锄菜、同席读书，歆慕轩冕，宁割席分坐。\n"
    "15. 诸葛恪得驴 ——《三国志·吴书·诸葛恪传》裴注引《江表传》：孙权题驴面『诸葛子瑜』，恪续『之驴』二字。\n"
    "16. 曲有误周郎顾 ——《三国志·吴书·周瑜传》：瑜少精意于音乐，虽三爵之后，其有阙误必知之。\n"
    "17. 士别三日 ——《三国志·吴书·吕蒙传》裴注引《江表传》：权劝蒙学，鲁肃叹『非复吴下阿蒙』。\n"
    "18. 文姬归汉 ——《后汉书·列女传·董祀妻》：曹操以金璧赎蔡琰归，琰作《悲愤诗》。\n"
    "19. 马钧复造指南车 ——《三国志·魏书·杜夔传》裴注：钧与高堂隆、秦朗争指南车，卒成之。\n"
    "20. 辛毗引裾 ——《三国志·魏书·辛毗传》：文帝欲徙冀州士家十万户，毗引帝裾而谏。\n"
    "21. 胡质父子清慎 ——《三国志·魏书·胡质传》裴注引《晋阳秋》：威省父，父与一匹绢为粮资。\n"
    "22. 望梅止渴 ——《世说新语·假谲》：魏武行役失汲道，乃令曰『前有大梅林，饶子，甘酸可以解渴』。\n"
    "23. 罗宪守永安 ——《三国志·蜀书·霍峻传》裴注引《汉晋春秋》：蜀亡，宪守永安拒吴师。\n"
    "24. 王粲驴鸣 ——《世说新语·伤逝》：粲好驴鸣，既葬，文帝临其丧，令赴客皆作一驴鸣。\n"
)

PLAN_SCHEMA = (
    "{\n"
    '  "stories": [\n'
    '    {"no": 1, "title": "中文短标题", "slug": "lowercase-english-slug",\n'
    '     "story_source": "史料书名篇名 + 一句原文摘录",\n'
    '     "why_not_in_romance": "具体说明为何《三国演义》未写此事",\n'
    '     "synopsis": "80 字以内梗概",\n'
    '     "reuse": ["建议复用的已有角色中文名"],\n'
    '     "new_character_hint": "本故事需要新建的角色中文名（1~2 个）"}\n'
    "  ]\n"
    "}\n"
)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def known_cards(cfg: dict) -> list[dict]:
    """Only the reusable Three Kingdoms cards, not the generic demo ones."""
    skip = {"林晚", "沈砚"}
    return [c for c in characters.list_cards(cfg) if c["name"] not in skip]


def known_text(cards: list[dict]) -> str:
    out = []
    for c in cards:
        p = c.get("profile") or {}
        out.append("- %s（%s）：%s；配色 %s" % (
            c["name"], p.get("role", ""), str(p.get("outfit", ""))[:60],
            str(p.get("palette", ""))[:36]))
    return "\n".join(out) or "（暂无）"


def plan_prompt(cards: list[dict]) -> str:
    return (
        "任务：为一个 10 集的「三国史料小故事」连环画系列选题，每集一个独立故事。\n\n"
        "【硬性要求】\n"
        "1. 恰好 10 个不同故事，全部发生在汉末三国时期，见于《三国志》及裴松之注引史料、"
        "《后汉书》《世说新语》《晋书》等，且【《三国演义》没有写过】。"
        "严禁桃园结义、草船借箭、空城计、赤壁、三顾茅庐、刮骨疗毒、七步成诗、乐不思蜀、"
        "白衣渡江、木牛流马、割发代首、击鼓骂曹、左慈戏曹、蒋干盗书、杨修鸡肋等演义名场面。\n"
        "2. 已经拍过的「鼠啮马鞍 / 曹冲智救库吏」不可再用。\n"
        "3. 优先从下列已核过出处的候选里选，也可另提同样符合条件的故事，"
        "但 why_not_in_romance 必须具体、可核查，不能含糊其辞：\n" + CANDIDATES + "\n"
        "4. 10 个故事在人物、地域（魏/蜀/吴）、情绪（机智/清廉/诙谐/悲悯/勇毅）上尽量分散，"
        "不要全都围着曹操转。\n"
        "5. 资产库里已有下列角色卡（可直接复用，不必重画立绘）。请在 reuse 里优先点名，"
        "每集尽量复用 1~2 个；new_character_hint 里给出本集需要新画立绘的角色（1~2 个），"
        "且【10 集之间新角色名字不得重复】。\n" + known_text(cards) + "\n"
        "6. slug 只用小写英文字母和连字符，长度不超过 28 字符，10 个互不重复。\n\n"
        "【输出 JSON schema（严格，只输出 JSON 对象本身）】\n" + PLAN_SCHEMA
    )


def script_prompt(story: dict, cards: list[dict], series_names: list[str],
                  episode: int, batch_id: str) -> str:
    series_text = "、".join(series_names) or "（暂无）"
    return (
        "任务：把下面这一个三国史料小故事，写成一个可生成的 6 格连环画脚本。\n\n"
        f"【本集】第 {episode} 集，批次 id 必须是 `{batch_id}`\n"
        f"标题：{story['title']}\n"
        f"史料：{story['story_source']}\n"
        f"演义未收录依据：{story['why_not_in_romance']}\n"
        f"梗概：{story.get('synopsis') or ''}\n"
        f"建议复用角色：{'、'.join(story.get('reuse') or []) or '（自行判断）'}\n\n"
        "【硬性要求】\n"
        "1. story_source / why_not_in_romance 请原样保留或补全细节，不要改成别的故事。\n"
        "2. 恰好 6 格分镜，起承转合完整；每格含英文 visual、中文旁白（25 字内）、可选对白，"
        "以及本格出现的人物名。【每格最多 2 个人物】（出图更快，也更聚焦）。\n"
        "3. 场景恰好 2 个，都是不含人物的独立环境概念图；6 格分镜只能引用这两个 scene_id。\n"
        "4. 人物总数不超过 3 个；资产库已有的角色标 existing=true 且不要重造外貌，"
        "新建角色最多 2 个并写全 profile 的 17 个字段。\n"
        "5. 本系列前面几集已经规划了这些新角色，立绘会全系列共用："
        + series_text + "。若本集需要用到其中某人，请【原样使用这个中文名】，"
        "不要另造同名或改名的角色，也不要重复描述成全新人物。\n"
        "6. 资产库已有角色（可复用，不必重画立绘）：\n" + known_text(cards) + "\n"
        "7. 画面里绝对不能出现任何文字、字母、印章、字幕、边框、分格线；"
        "旁白与对白只存在字段里，由 HTML 排版叠加。\n"
        "8. 视觉风格沿用「三国英雄立绘」：premium Three Kingdoms historical-fantasy "
        "game concept art, highly detailed painterly digital illustration, historical "
        "pigment palette, fine armour and textile texture, even lighting。\n"
        "9. visual / prompt_extra / visual_rules 用英文写，且每条自带 "
        "'no text, no letters, no captions, no watermark, no panel borders, no split lines'。"
        "人物外貌要与已有角色卡一致，不换服装、发型或武器。\n\n"
        "【输出 JSON schema（严格遵守键名，不要增删顶层键）】\n" + wc.SCHEMA
    )


def validate_series_story(d: dict, batch_id: str) -> None:
    wc.validate(d)
    if d.get("id") != batch_id:
        d["id"] = batch_id
    if len(d["scenes"]) != 2:
        raise ValueError("scenes 必须恰好 2 个")
    if len(d["characters"]) > 3:
        raise ValueError("characters 不能超过 3 个")
    for p in d["panels"]:
        if len(p.get("characters") or []) > 2:
            raise ValueError("每格最多 2 个人物")
    names = {c["name"] for c in d["characters"]}
    if len(names) != len(d["characters"]):
        raise ValueError("人物名重复")


def chat_json_retry(cfg: dict, prompt: str, *, max_tokens: int,
                    tries: int = 3):
    last = None
    for i in range(tries):
        try:
            return text.chat_json(cfg, prompt, system=wc.SYSTEM,
                                  max_tokens=max_tokens)
        except Exception as exc:  # transient proxy / malformed JSON
            last = exc
            log(f"  文本模型第 {i+1} 次失败：{type(exc).__name__}: {str(exc)[:140]}")
    raise RuntimeError(f"文本模型连续 {tries} 次失败：{last}")


def slugify(value: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return (s[:28].strip("-") or fallback)


def main() -> None:
    ap = argparse.ArgumentParser(description="用 GPT-6.1 Sol 创作 10 集三国连环画")
    ap.add_argument("--show", action="store_true", help="只打印选题，不写文件")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的剧本")
    ap.add_argument("--replan", action="store_true", help="忽略选题缓存，重新让文本模型选题")
    ap.add_argument("--count", type=int, default=COUNT)
    args = ap.parse_args()

    cfg = config.load()
    if not config.has_key(cfg):
        raise SystemExit("没有可用的 API key。")
    cards = known_cards(cfg)
    model = str(cfg.get("text_model") or text.DEFAULT_MODEL)
    log(f"文本模型 {model} · 已有可复用角色卡 {len(cards)} 张")

    cache = ROOT / "batches" / ("series-" + SERIES_ID + "-plan.json")
    if cache.is_file() and not args.replan:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        raw, plan_meta = cached, cached.get("_meta") or {}
        log(f"复用选题缓存 {cache.name}")
    else:
        plan_p = plan_prompt(cards)
        raw, plan_meta = chat_json_retry(cfg, plan_p, max_tokens=12000)
    stories = raw.get("stories") if isinstance(raw, dict) else None
    if not isinstance(stories, list) or not stories:
        raise SystemExit("选题返回格式不对：缺 stories 数组")
    stories = stories[:args.count]
    # Normalise and de-duplicate slugs / titles locally before writing scripts.
    seen_slug, seen_title = set(), set()
    clean = []
    for i, s in enumerate(stories, 1):
        s["no"] = i
        s["slug"] = slugify(s.get("slug") or s.get("title"), f"tale-{i:02d}")
        while s["slug"] in seen_slug:
            s["slug"] = slugify(s["slug"] + "-b", f"tale-{i:02d}-{len(seen_slug)}")
        seen_slug.add(s["slug"])
        if s.get("title") in seen_title:
            s["title"] = str(s.get("title")) + f"（{i}）"
        seen_title.add(s["title"])
        clean.append(s)
    stories = clean
    if not cache.is_file() or args.replan:
        cache.write_text(json.dumps({**raw, "_meta": plan_meta},
                                    ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    log(f"选题 {len(stories)} 集：")
    for s in stories:
        print(f"  {s['no']:>2}. {s['title']} · {s['slug']}", flush=True)
    if args.show:
        print(json.dumps({"stories": stories, "usage": plan_meta["usage"]},
                         ensure_ascii=False, indent=2))
        return

    episodes = []
    used_new: list[str] = []
    for s in stories:
        no = int(s["no"])
        batch_id = f"comic-s{no:02d}-{s['slug']}"
        out = ROOT / "batches" / (batch_id + ".json")
        if out.is_file() and not args.force:
            d = json.loads(out.read_text(encoding="utf-8"))
            log(f"第 {no:>2} 集已存在，跳过：{d.get('title')}")
            for c in d.get("characters") or []:
                if not c.get("existing") and c["name"] not in used_new:
                    used_new.append(c["name"])
            episodes.append({"no": no, "id": batch_id, "title": d.get("title"),
                             "file": out.name, "reused_script": True})
            continue

        p = script_prompt(s, cards, used_new, no, batch_id)
        log(f"第 {no:>2}/{len(stories)} 集写剧本：{s['title']} …")
        known = {c["name"]: c["id"] for c in characters.list_cards(cfg)}
        d, meta = None, None
        for attempt in range(3):
            try:
                cand, m = chat_json_retry(cfg, p, max_tokens=12000)
                validate_series_story(cand, batch_id)
                d, meta = cand, m
                break
            except ValueError as exc:
                log(f"  第 {attempt+1} 次校验不通过：{exc}")
                if attempt == 2:
                    raise
                p += ("\n\n【上一次输出被拒绝】" + str(exc) +
                      "。请重新输出完整的合法 JSON，严格修正后再给。")
        if d is None or meta is None:
            raise RuntimeError("剧本生成失败：" + batch_id)

        for c in d["characters"]:
            cid = known.get(c["name"])
            c["existing"] = bool(cid)
            c["character_id"] = cid or ""
            if not cid and c["name"] not in used_new:
                used_new.append(c["name"])
        d.update({
            "text_model": model,
            "image_model": "gpt-image-2.6-sunburst",
            "style_id": "sanguo-comic", "quality": "high",
            "portrait_size": "1024x1536", "scene_size": "1536x1024",
            "panel_size": "1536x1024", "max_cost_usd": 8.0,
            "series_id": SERIES_ID, "episode": no,
            "_generation": {
                "written_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "text_model": meta["model"], "usage": meta["usage"],
                "raw_chars": meta["raw_chars"],
                "finish_reason": meta["finish_reason"],
                "prompt_sha256": hashlib.sha256(p.encode("utf-8")).hexdigest(),
                "prompt_chars": len(p),
            },
        })
        out.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        log(f"  已写入 {out.name} · 人物 {len(d['characters'])} · 场景 2 · 分镜 6")
        episodes.append({"no": no, "id": batch_id, "title": d["title"],
                         "file": out.name, "source": d["story_source"],
                         "why_not_in_romance": d["why_not_in_romance"],
                         "characters": [c["name"] for c in d["characters"]],
                         "scenes": [x["name"] for x in d["scenes"]],
                         "reused_script": False})

    manifest = {
        "id": SERIES_ID, "title": SERIES_TITLE,
        "planned_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "text_model": model, "image_model": "gpt-image-2.6-sunburst",
        "style_id": "sanguo-comic", "quality": "high",
        "constraint": "《三国演义》未收录、见于史料的汉末三国小故事",
        "plan_usage": plan_meta["usage"],
        "episodes": episodes,
    }
    mf = ROOT / "batches" / ("series-" + SERIES_ID + ".json")
    mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                  encoding="utf-8")
    log(f"系列清单 {mf.name} · {len(episodes)} 集 · 新角色 {len(used_new)} 个")


if __name__ == "__main__":
    main()
