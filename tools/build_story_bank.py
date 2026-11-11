#!/usr/bin/env python3
"""Build the offline story bank from collected raw batches.

    python tools/build_story_bank.py              # merge, validate, render, sync
    python tools/build_story_bank.py --no-sync   # skip the Google Drive copy
    python tools/build_story_bank.py --show      # print stats only

Fully offline: no API calls. Reads
    data/story-collect/*.json          (raw model batches, optional)
    tools/write_comic_series.py       (24 curated, source-vetted candidates)
    batches/romance-toc.json          (120 Romance chapter titles)
    batches/series-*.json, comic-*.json (already produced episodes)
and writes
    batches/story-bank.json            (single source of truth)
    data/packs/story-bank/index.html   (self-contained browsable story bank)
    D:/gdrive.photo/ai-assets/all/story-bank/   (cloud copy)

The user relaxed the constraint: tales that DO appear in the Romance are
welcome, they are simply labelled so the bank can be filtered either way.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import write_comic_series as wcs  # noqa: E402
from studio import config  # noqa: E402

RAW_DIR = ROOT / "data" / "story-collect"
OUT_JSON = ROOT / "batches" / "story-bank.json"
OUT_DIR = ROOT / "data" / "packs" / "story-bank"
BATCH_KEYS = ("wei", "shu", "wu", "shishuo", "houhanshu", "artisans",
              "romance", "mixed")
BATCH_LABELS = {
    "wei": "魏书 / 裴注", "shu": "蜀书 / 裴注", "wu": "吴书 / 裴注",
    "shishuo": "世说新语", "houhanshu": "后汉书 / 晋书",
    "artisans": "器物制度", "romance": "三国演义亦载", "mixed": "跨来源补漏",
    "curated": "人工核验基线",
}

# Titles already turned into comics -> they stay in the bank as "used".
USED_ALIASES = {
    "鼠啮马鞍": "鼠啮马鞍", "曹冲智救库吏": "鼠啮马鞍", "曹冲救库吏": "鼠啮马鞍",
    "蔡邕倒屣迎王粲": "蔡邕倒屣迎少年", "倒屣迎王粲": "蔡邕倒屣迎少年",
    "倒屣迎粲": "蔡邕倒屣迎少年", "蔡邕倒屣迎少年": "蔡邕倒屣迎少年",
    "田豫辞归养母": "田豫辞归养母", "田豫归养": "田豫辞归养母",
    "孙登验丸释疑": "孙登验丸释疑", "孙登比丸": "孙登验丸释疑",
    "辛毗引裾": "辛毗牵衣直谏", "辛毗牵裾": "辛毗牵衣直谏",
    "辛毗牵衣直谏": "辛毗牵衣直谏",
    "王粲驴鸣": "一场驴鸣送故人", "驴鸣送葬": "一场驴鸣送故人",
    "一场驴鸣送故人": "一场驴鸣送故人", "文帝临丧作驴鸣": "一场驴鸣送故人",
    "杜微不闻荣禄": "杜微不闻荣禄", "杜微辞征": "杜微不闻荣禄",
    "胡质父子清慎": "胡威千里省父", "胡威省父": "胡威千里省父",
    "胡威千里省父": "胡威千里省父", "胡威问绢": "胡威千里省父",
    "马钧复造指南车": "马钧复造指南车", "马钧造指南车": "马钧复造指南车",
    "费祎围棋办贼": "费祎临军不废棋", "费祎临军不废棋": "费祎临军不废棋",
    "费祎对弈": "费祎临军不废棋",
    "诸葛恪得驴": "诸葛恪得驴", "诸葛恪添字得驴": "诸葛恪得驴",
}

# Manual verdicts from checking the 120-chapter Romance against the curated
# pool. These OVERRIDE any model self-report on the same title.
ROMANCE_VERDICTS = {
    "秦宓论天": ("in_romance", "第八十六回回目「难张温秦宓逞天辩」即此事，演义正面写过。"),
    "秦宓逞天辩": ("in_romance", "第八十六回回目「难张温秦宓逞天辩」即此事。"),
    "望梅止渴": ("in_romance", "第二十一回「曹操煮酒论英雄」中曹操亲口追述征张绣时『以鞭虚指曰：前面有梅林』。"),
    "陆绩怀橘": ("mentioned", "第四十三回舌战群儒，诸葛亮以『公非袁术座间怀橘之陆郎乎』讥陆绩，属典故提及而非正面叙写。"),
    "老牛舐犊": ("in_romance", "第七十二回杨修被杀后，曹操见杨彪问『公何瘦之甚』，彪答『犹怀老牛舐犊之爱』。"),
    "士别三日": ("uncertain", "孙权劝学见《三国志·吴书·吕蒙传》裴注引《江表传》与《资治通鉴》；演义是否正面叙写待复核。"),
    "吴下阿蒙": ("uncertain", "同『士别三日』，演义中吕蒙事迹多，劝学一段是否写入待复核。"),
    "文姬归汉": ("not_in_romance", "演义第九回写蔡邕哭董卓，未写曹操以金璧赎蔡琰归汉。"),
    "管宁割席": ("not_in_romance", "演义写华歆搜捕伏皇后、逼献帝禅让，未写与管宁锄菜割席。"),
    "曲有误周郎顾": ("not_in_romance", "演义写周瑜而未及其醉后听曲必顾的细节。"),
    "曹冲称象": ("not_in_romance", "演义未写曹冲称象；同传『鼠啮马鞍』亦未写，已拍成单集。"),
    "床头捉刀人": ("not_in_romance", "演义写匈奴使者来朝未用此《世说新语·容止》轶事。"),
    "元龙百尺楼": ("not_in_romance", "演义写陈登助曹操破吕布，未写刘备『欲卧百尺楼上』之评。"),
    "简雍讽禁酒具": ("not_in_romance", "演义有简雍而无比照酿具讽谏禁酒的插曲。"),
    "伊籍使吴": ("not_in_romance", "演义有伊籍而未写『一拜一起，未足为劳』的应对。"),
    "刘巴不与张飞共宿": ("not_in_romance", "演义未写刘巴轻武人、诸葛亮为其排解一事。"),
    "枣祗建屯田": ("not_in_romance", "演义写许下屯田之效而未叙枣祗建置之议。"),
    "孙权庱亭射虎": ("not_in_romance", "演义未写孙权亲射虎、投双戟却虎之事。"),
    "罗宪守永安": ("not_in_romance", "演义写蜀亡而未写罗宪以孤城拒吴师。"),
}

GROUP_LABELS = {
    "not_in_romance": "演义未载",
    "in_romance": "演义亦载",
    "mentioned": "仅被提及",
    "similar": "有相似回目",
    "uncertain": "演义归属待考",
}

# Curated 24: region / mood / characters / visual hook.
CURATED_META = {
    "陆绩怀橘": ("吴", "孝行", ["陆绩", "袁术"], "六岁陆绩拜辞时橘子从袖中滚落"),
    "曹冲称象": ("魏", "机智", ["曹冲", "曹操"], "大船刻水痕，换装货物到同一刻线"),
    "孙权庱亭射虎": ("吴", "勇毅", ["孙权"], "虎伤其马，孙权投双戟退虎"),
    "枣祗建屯田": ("魏", "考据", ["枣祗", "曹操"], "许下屯田，谷仓与田垄"),
    "床头捉刀人": ("魏", "机智", ["曹操", "崔琰", "匈奴使者"], "床侧捉刀立者与案前代坐者"),
    "蔡邕倒屣迎王粲": ("汉末", "诙谐", ["蔡邕", "王粲"], "满座宾客中倒穿鞋奔出迎少年"),
    "简雍讽禁酒具": ("蜀", "诙谐", ["简雍", "刘备"], "道上男女与酿酒器具并列一指"),
    "伊籍使吴": ("蜀", "机智", ["伊籍", "孙权"], "拜起之间从容答『未足为劳』"),
    "费祎围棋办贼": ("蜀", "考据", ["费祎", "来敏"], "羽檄交驰中对局不辍"),
    "秦宓论天": ("蜀", "机智", ["秦宓", "张温"], "使者问天，随问而答"),
    "刘巴不与张飞共宿": ("蜀", "诙谐", ["刘巴", "张飞", "诸葛亮"], "同榻不语，一纸书信解围"),
    "老牛舐犊": ("魏", "悲悯", ["杨彪", "曹操"], "丧子后消瘦的老臣与曹操问答"),
    "元龙百尺楼": ("魏", "诙谐", ["刘备", "许汜", "陈登"], "百尺楼上卧客，客卧于地"),
    "管宁割席": ("汉末", "考据", ["管宁", "华歆"], "锄菜见金、同席读书后割席分坐"),
    "诸葛恪得驴": ("吴", "机智", ["诸葛恪", "孙权", "诸葛瑾"], "驴脸长牌上续写『之驴』二字"),
    "曲有误周郎顾": ("吴", "巧艺", ["周瑜"], "三爵之后闻曲误而回顾"),
    "士别三日": ("吴", "考据", ["吕蒙", "孙权", "鲁肃"], "鲁肃过寻阳与吕蒙论议惊叹"),
    "文姬归汉": ("汉末", "悲悯", ["蔡琰", "曹操"], "金璧赎归，车卷与诗简"),
    "马钧复造指南车": ("魏", "巧艺", ["马钧", "高堂隆"], "木人始终南指的试车庭"),
    "辛毗引裾": ("魏", "直谏", ["辛毗", "曹丕"], "追入内殿牵住帝王衣裾"),
    "胡质父子清慎": ("魏", "清廉", ["胡质", "胡威"], "临别一匹绢，追问来处"),
    "望梅止渴": ("魏", "机智", ["曹操"], "以鞭虚指前方梅林，士卒口生唾"),
    "罗宪守永安": ("蜀", "勇毅", ["罗宪"], "蜀亡后孤城拒吴师"),
    "王粲驴鸣": ("魏", "悼亡", ["曹丕", "王粲"], "灵前宾客各作一声驴鸣"),
}


def norm(value: str) -> str:
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(value or "")).lower()


def parse_curated() -> list[dict]:
    out = []
    for line in wcs.CANDIDATES.strip().splitlines():
        m = re.match(r"\s*\d+\.\s*(.+?)\s*——(.+?)：(.*)$", line)
        if not m:
            continue
        title, source, synopsis = (x.strip() for x in m.groups())
        region, mood, chars, hook = CURATED_META.get(
            title, ("", "考据", [], ""))
        verdict = ROMANCE_VERDICTS.get(title)
        out.append({
            "title": title, "source": source,
            "quote": "原文见 " + source,
            "synopsis": synopsis, "characters": chars,
            "region": region, "mood": mood,
            "romance_check": verdict[0] if verdict else "not_in_romance",
            "romance_note": (verdict[1] if verdict else
                             "人工核验基线：见于史料/古注，演义未见正面叙写；仍建议抽查。"),
            "visual_hook": hook, "confidence": "high",
            "origin": "curated", "batch": "curated",
        })
    return out


def load_raw() -> tuple[list[dict], dict]:
    rows, meta = [], {}
    for f in sorted(RAW_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        key = d.get("batch") or f.stem
        stories = d.get("stories") or []
        meta[key] = {"count": len(stories), "model": (d.get("_meta") or {}).get("model"),
                     "usage": (d.get("_meta") or {}).get("usage") or {}}
        for s in stories:
            if not isinstance(s, dict) or not s.get("title"):
                continue
            rows.append({
                "title": str(s.get("title") or "").strip(),
                "source": str(s.get("source") or "").strip(),
                "quote": str(s.get("quote") or "").strip(),
                "synopsis": str(s.get("synopsis") or "").strip(),
                "characters": [str(x).strip() for x in (s.get("characters") or []) if x],
                "region": str(s.get("region") or "").strip(),
                "mood": str(s.get("mood") or "").strip(),
                "romance_check": str(s.get("romance_check") or "uncertain").strip(),
                "romance_note": str(s.get("romance_note") or "").strip(),
                "visual_hook": str(s.get("visual_hook") or "").strip(),
                "confidence": str(s.get("confidence") or "medium").strip(),
                "origin": "model", "batch": key,
            })
    return rows, meta


def used_stories() -> list[dict]:
    out = []
    for f in sorted((ROOT / "batches").glob("comic-*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not d.get("title"):
            continue
        out.append({"title": str(d["title"]), "id": str(d.get("id") or f.stem),
                    "source": str(d.get("story_source") or "")[:160]})
    return out


def toc_index() -> tuple[list[str], dict]:
    f = ROOT / "batches" / "romance-toc.json"
    if not f.is_file():
        return [], {}
    chapters = json.loads(f.read_text(encoding="utf-8")).get("chapters") or []
    return chapters, {"count": len(chapters),
                      "source": "https://sanguo.5000yan.com/"}


def toc_hits(chapters: list[str], chars: list[str], title: str) -> list[str]:
    """Chapter titles mentioning a character or the story title itself."""
    keys = [k for k in (chars or []) if len(k) >= 2]
    keys.append(title[:2] if len(title) >= 2 else title)
    hits = []
    for ch in chapters:
        body = ch.split(" ", 1)[-1]
        if any(k in body for k in keys):
            hits.append(ch.split(" ", 1)[0])
    return hits[:6]


def classify(row: dict) -> dict:
    verdict = ROMANCE_VERDICTS.get(row["title"])
    if verdict:
        row["romance_check"], row["romance_note"] = verdict
        row["verdict_source"] = "manual"
    else:
        row["verdict_source"] = "model" if row["origin"] == "model" else "curated"
    rc = row["romance_check"]
    if rc not in GROUP_LABELS:
        row["romance_check"] = rc = "uncertain"
    row["romance_group"] = GROUP_LABELS[rc]

    used_key = USED_ALIASES.get(row["title"])
    if used_key:
        row["status"], row["used_as"] = "used", used_key
        row["grade"] = "U"
        return row

    weak = (not row["source"]
            or row["quote"] in ("", "原文待核")
            or row["confidence"] == "low"
            or rc == "uncertain")
    if weak:
        row["status"], row["grade"] = "needs_review", "R"
    else:
        row["status"] = "candidate"
        if row["origin"] == "curated":
            row["grade"] = "A"
        elif row["confidence"] == "high":
            row["grade"] = "B"
        else:
            row["grade"] = "C"
    return row


def merge(rows: list[dict], chapters: list[str]) -> tuple[list[dict], list[dict]]:
    """De-duplicate by normalised title / known aliases / substring match."""
    kept, dropped = [], []
    seen: dict[str, dict] = {}
    for r in rows:
        key = norm(r["title"])
        alias = USED_ALIASES.get(r["title"])
        if alias:
            key = norm(alias)
        dup = seen.get(key)
        if not dup:
            # substring match catches e.g. 「秦宓论天」 vs 「秦宓论天辩」
            for prev in kept:
                a, b = norm(prev["title"]), key
                if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
                    dup = prev
                    break
        if dup:
            # Curated / manual verdicts win over a model duplicate.
            better = (r["origin"] == "curated" and dup["origin"] != "curated")
            if better:
                keep_fields = ("origin", "batch", "source", "quote", "characters",
                               "romance_check", "romance_note", "confidence")
                for k in keep_fields:
                    dup[k] = r[k]
            dropped.append({"title": r["title"], "duplicate_of": dup["title"],
                            "batch": r["batch"]})
            continue
        seen[key] = r
        kept.append(r)

    for i, r in enumerate(sorted(
            kept,
            key=lambda x: ({"candidate": 0, "needs_review": 1, "used": 2}[x["status"]],
                           {"A": 0, "B": 1, "C": 2, "R": 3, "U": 4}[x["grade"]],
                           x["batch"], x["title"])), 1):
        r["no"] = i
        r["id"] = f"tale-{i:03d}"
        r["toc_hits"] = toc_hits(chapters, r["characters"], r["title"])
    ordered = sorted(kept, key=lambda x: x["no"])
    return ordered, dropped


def render_html(bank: dict) -> str:
    data = json.dumps(bank["stories"], ensure_ascii=False).replace("</", "<\\/")
    stats = bank["stats"]
    labels = json.dumps(BATCH_LABELS, ensure_ascii=False)
    return HTML_TEMPLATE.replace("__DATA__", data) \
        .replace("__LABELS__", labels) \
        .replace("__STATS__", json.dumps(stats, ensure_ascii=False)) \
        .replace("__BUILT__", bank["built_at"])


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>三国小故事候选库</title>
<style>
:root{--bg:#f6f3ec;--card:#fff;--ink:#26221c;--dim:#6b6255;--line:#e2dbcd;
--accent:#8a3b2e;--accent2:#3c5a52;--chip:#efe9dc}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font-family:"Noto Serif SC","Songti SC",Georgia,serif;line-height:1.7}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 80px}
h1{font-size:30px;margin:0 0 6px;letter-spacing:2px}
.sub{color:var(--dim);font-size:14px;margin-bottom:22px}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:20px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:10px 16px;min-width:110px}
.stat b{display:block;font-size:24px;color:var(--accent)}
.stat span{font-size:12px;color:var(--dim)}
.bar{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:14px;display:flex;flex-wrap:wrap;gap:10px;margin-bottom:18px}
input,select{font:inherit;font-size:14px;padding:8px 10px;border:1px solid var(--line);
border-radius:7px;background:#fff;color:var(--ink)}
input{flex:1;min-width:220px}
.count{color:var(--dim);font-size:13px;padding:8px 2px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:11px;
padding:16px 17px;break-inside:avoid}
.card h3{margin:0 0 8px;font-size:18px;letter-spacing:1px}
.no{color:var(--dim);font-size:13px;font-weight:normal;margin-right:8px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
.chip{font-size:11px;padding:2px 9px;border-radius:20px;background:var(--chip);
color:var(--dim);border:1px solid var(--line)}
.chip.g{background:#eee4d8;color:#6a4326}
.chip.used{background:#e4e9e6;color:#3c5a52}
.chip.review{background:#f3e3df;color:#8a3b2e}
.src{font-size:13px;color:var(--accent2);margin-bottom:6px}
.quote{font-size:13px;color:var(--dim);border-left:2px solid var(--line);
padding-left:10px;margin:8px 0}
.syn{font-size:14px;margin:8px 0}
.row{font-size:12px;color:var(--dim);margin-top:6px}
.row b{color:var(--ink);font-weight:600}
.empty{color:var(--dim);padding:30px;text-align:center}
footer{margin-top:34px;color:var(--dim);font-size:12px;border-top:1px solid var(--line);padding-top:14px}
@media print{.bar{display:none}.card{box-shadow:none}body{background:#fff}}
</style></head><body><div class="wrap">
<h1>三国小故事候选库</h1>
<div class="sub">史料轶事与演义情节一并收录，逐条标注出处与《三国演义》归属，供后续连环画选题。生成于 __BUILT__。</div>
<div class="stats" id="stats"></div>
<div class="bar">
<input id="q" placeholder="搜索标题、人物、出处、梗概…">
<select id="fStatus"><option value="">全部状态</option><option value="candidate">可用候选</option>
<option value="needs_review">待复核</option><option value="used">已拍</option></select>
<select id="fGroup"><option value="">全部演义归属</option><option value="演义未载">演义未载</option>
<option value="演义亦载">演义亦载</option><option value="仅被提及">仅被提及</option>
<option value="有相似回目">有相似回目</option><option value="演义归属待考">归属待考</option></select>
<select id="fBatch"><option value="">全部来源</option></select>
<select id="fRegion"><option value="">全部地域</option></select>
</div>
<div class="count" id="count"></div>
<div class="grid" id="grid"></div>
<footer>唯一数据源：batches/story-bank.json · 由 tools/build_story_bank.py 离线生成 · 无需联网、可直接分享或打印</footer>
</div><script>
const DATA=__DATA__, LABELS=__LABELS__, STATS=__STATS__;
const $=s=>document.querySelector(s);
const esc=v=>String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const statDefs=[['total','总条目'],['candidate','可用候选'],['needs_review','待复核'],
 ['used','已拍成连环画'],['grade_a','A 级·人工核验'],['not_in_romance','演义未载'],
 ['in_romance','演义亦载/提及']];
$('#stats').innerHTML=statDefs.map(([k,l])=>`<div class="stat"><b>${STATS[k]||0}</b><span>${l}</span></div>`).join('');
[...new Set(DATA.map(d=>d.batch))].sort().forEach(b=>{
  const o=document.createElement('option');o.value=b;o.textContent=LABELS[b]||b;$('#fBatch').appendChild(o)});
[...new Set(DATA.map(d=>d.region).filter(Boolean))].sort().forEach(r=>{
  const o=document.createElement('option');o.value=r;o.textContent=r;$('#fRegion').appendChild(o)});
function render(){
  const q=$('#q').value.trim().toLowerCase(), st=$('#fStatus').value,
        gr=$('#fGroup').value, ba=$('#fBatch').value, re=$('#fRegion').value;
  const rows=DATA.filter(d=>{
    if(st&&d.status!==st)return false;
    if(gr&&d.romance_group!==gr)return false;
    if(ba&&d.batch!==ba)return false;
    if(re&&d.region!==re)return false;
    if(q){const t=[d.title,d.source,d.synopsis,d.quote,(d.characters||[]).join(''),
      d.romance_note,d.visual_hook].join(' ').toLowerCase();if(!t.includes(q))return false}
    return true});
  $('#count').textContent=`显示 ${rows.length} / ${DATA.length} 条`;
  $('#grid').innerHTML=rows.map(d=>`
   <div class="card">
    <h3><span class="no">${d.no}</span>${esc(d.title)}</h3>
    <div class="chips">
      <span class="chip g">${esc(d.grade)} 级</span>
      <span class="chip ${d.status==='used'?'used':d.status==='needs_review'?'review':''}">${d.status==='used'?'已拍':d.status==='needs_review'?'待复核':'可用'}</span>
      <span class="chip">${esc(d.romance_group)}</span>
      <span class="chip">${esc(LABELS[d.batch]||d.batch)}</span>
      ${d.region?`<span class="chip">${esc(d.region)}</span>`:''}
      ${d.mood?`<span class="chip">${esc(d.mood)}</span>`:''}
    </div>
    <div class="src">${esc(d.source)}</div>
    ${d.quote&&d.quote!=='原文待核'?`<div class="quote">${esc(d.quote)}</div>`:''}
    <div class="syn">${esc(d.synopsis)}</div>
    <div class="row"><b>演义：</b>${esc(d.romance_note||'—')}</div>
    ${(d.toc_hits||[]).length?`<div class="row"><b>相关回目：</b>${esc((d.toc_hits||[]).join('、'))}</div>`:''}
    ${(d.characters||[]).length?`<div class="row"><b>人物：</b>${esc((d.characters||[]).join('、'))}</div>`:''}
    ${d.visual_hook?`<div class="row"><b>画面：</b>${esc(d.visual_hook)}</div>`:''}
    ${d.used_as?`<div class="row"><b>已用于：</b>${esc(d.used_as)}</div>`:''}
   </div>`).join('')||'<div class="empty">没有符合筛选条件的故事</div>';
}
['q','fStatus','fGroup','fBatch','fRegion'].forEach(id=>{
  $('#'+id).addEventListener(id==='q'?'input':'change',render)});
render();
</script></body></html>
"""


def sync(bank: dict, html: str) -> Path:
    cfg = config.load()
    root = Path(str(cfg.get("asset_root") or "D:/gdrive.photo/ai-assets"))
    dest = root / "all" / "story-bank"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "story-bank.json").write_text(
        json.dumps(bank, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (dest / "index.html").write_text(html, encoding="utf-8")
    (dest / "README.txt").write_text(
        "三国小故事候选库\n"
        "================\n"
        f"生成时间：{bank['built_at']}\n"
        f"总条目 {bank['stats']['total']} · 可用候选 {bank['stats']['candidate']} · "
        f"待复核 {bank['stats']['needs_review']} · 已拍 {bank['stats']['used']}\n\n"
        "index.html  可离线打开、搜索、筛选、打印的故事库（数据已内嵌）\n"
        "story-bank.json  唯一数据源，字段含出处、原文摘录、演义归属判定、回目提示、状态与等级\n\n"
        "口径：用户已放宽约束，《三国演义》写过的故事同样收录，"
        "只是用 romance_group 标注为「演义亦载 / 仅被提及 / 有相似回目」。\n"
        "A 级为 tools/write_comic_series.py 中人工核验过的基线；"
        "B/C 级为 gpt-6.1-sol 采集、建议抽查出处后再进入剧本创作。\n",
        encoding="utf-8")
    return dest


def main() -> None:
    ap = argparse.ArgumentParser(description="离线合并三国小故事候选库")
    ap.add_argument("--no-sync", action="store_true", help="不同步到云盘")
    ap.add_argument("--show", action="store_true", help="只打印统计")
    args = ap.parse_args()

    curated = parse_curated()
    raw, batch_meta = load_raw()
    chapters, toc_meta = toc_index()
    used = used_stories()
    rows = [classify(r) for r in (curated + raw)]
    stories, dropped = merge(rows, chapters)

    counts = Counter(r["status"] for r in stories)
    grades = Counter(r["grade"] for r in stories)
    groups = Counter(r["romance_group"] for r in stories)
    batches = Counter(r["batch"] for r in stories)
    stats = {
        "total": len(stories),
        "candidate": counts["candidate"],
        "needs_review": counts["needs_review"],
        "used": counts["used"],
        "grade_a": grades["A"], "grade_b": grades["B"], "grade_c": grades["C"],
        "not_in_romance": groups["演义未载"],
        "in_romance": (groups["演义亦载"] + groups["仅被提及"]
                       + groups["有相似回目"]),
        "curated": batches["curated"],
        "model": len(stories) - batches["curated"],
        "raw_model_rows": len(raw),
        "duplicates_dropped": len(dropped),
        "by_batch": dict(sorted(batches.items())),
        "by_group": dict(sorted(groups.items())),
    }
    bank = {
        "id": "story-bank-01",
        "title": "三国小故事候选库",
        "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "constraint": "汉末三国小故事；《三国演义》写过的同样收录，按 romance_group 标注",
        "text_model": str(config.load().get("text_model") or "gpt-6.1-sol"),
        "sources": {
            "curated": "tools/write_comic_series.py 中 24 条人工核验候选",
            "model_batches": batch_meta,
            "romance_toc": toc_meta,
        },
        "romance_toc_note": ("回目命中仅作参考，不代表该故事被正面叙写；"
                             "确定归属见每条 romance_check / romance_note，"
                             "人工复核结论见 ROMANCE_VERDICTS。"),
        "levels": {
            "A": "人工核验基线，出处可信度最高",
            "B": "文本模型采集，自评高置信且有原文摘录",
            "C": "文本模型采集，中置信，建议抽查",
            "R": "待复核：出处/原文存疑或演义归属不确定",
            "U": "已拍成连环画",
        },
        "stats": stats,
        "used_stories": used,
        "duplicates_dropped": dropped,
        "stories": stories,
    }
    OUT_JSON.write_text(json.dumps(bank, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    html = render_html(bank)
    (OUT_DIR / "index.html").write_text(html, encoding="utf-8")

    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print("已写", OUT_JSON)
    print("已写", OUT_DIR / "index.html")
    if args.show:
        return
    if not args.no_sync:
        dest = sync(bank, html)
        print("已同步", dest)


if __name__ == "__main__":
    main()
