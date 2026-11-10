"""Character cards: novel -> structured traits -> reproducible prompt anchor.

A "character anchor" is a fixed, versioned block of text that is prepended
verbatim to every generation for that character. Combined with a reference
image sent to /images/edits (input_fidelity=high), it is what keeps a character
recognisable across views, poses and animation frames.

Novel extraction prefers an LLM when a key is available, and always falls back
to a local heuristic so the whole workflow works offline. Every extracted
trait carries the original sentence as evidence and a confidence label,
because automatic extraction must remain human-correctable.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections import Counter

from . import config, imaging, store

# (key, Chinese label) - also consumed by the web form.
PROFILE_FIELDS = [
    ("age", "年龄"), ("gender", "性别"), ("role", "身份 / 职业"),
    ("era", "时代 / 世界观"), ("face", "脸型"), ("eyes", "眼睛"),
    ("skin", "肤色"), ("build", "体型"), ("hair", "发型发色"),
    ("marks", "显著特征"), ("outfit", "服饰"), ("accessories", "配饰"),
    ("weapon", "武器 / 道具"), ("palette", "配色"), ("temperament", "气质"),
    ("art_style", "美术风格"), ("negative", "负面词"),
]

DEFAULT_ART_STYLE = ("flat vector game concept art, clean outlines, cel shading, "
                     "orthographic view, even flat lighting")
DEFAULT_NEGATIVE = ("photorealism, 3D render, realistic human face, celebrity, "
                    "text, letters, watermark, logo, gradient, drop shadow, "
                    "cluttered background, cropped body")

_SYSTEM_PROMPT = """You are a character-design assistant for a game art pipeline.
Read the novel excerpt and extract every DISTINCT named character that has any
visual description. Return ONLY a JSON object, no markdown, no commentary:

{"characters":[{"name":"...","aliases":[],"profile":{
 "age":"","gender":"","role":"","era":"","face":"","eyes":"","skin":"",
 "build":"","hair":"","marks":"","outfit":"","accessories":"","weapon":"",
 "palette":"","temperament":"","art_style":"","negative":""},
 "evidence":{"hair":"<exact short quote from the text>","outfit":"..."},
 "confidence":{"hair":"explicit","outfit":"inferred"}}]}

Rules:
- NEVER invent a trait. Leave a field empty when the text does not support it.
- "explicit" = directly stated; "inferred" = a reasonable reading of mood/era.
- evidence values must be short verbatim quotes from the input text.
- Keep the original language of the excerpt inside the values.
- art_style and negative may be filled with sensible generic guidance for game
  concept art, and must be marked "inferred".
- At most 6 characters.
"""

_VERBS = ("低声道", "轻声说", "沉声道", "说道", "问道", "答道", "笑道",
          "说", "道", "问", "答", "点头", "摇头", "抬眼", "皱眉",
          "转身", "推开", "把", "将", "却", "又", "便", "是", "的")
_STOPWORDS = {"他们", "我们", "你们", "自己", "忽然", "此时", "于是", "只听",
             "那人", "众人", "有人", "对方", "此人", "来人", "少年", "女子",
             "男子", "姑娘", "公子", "随即", "接着", "然后", "一面", "一面"}
_SURNAMES = set("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华"
                "金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方"
                "俞任袁柳鲍史唐费廉岑薛雷贺倪汤罗毕郝邬安常乐于时傅皮卞齐康"
                "伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈"
                "宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛"
                "林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支柯咎管卢莫房解应宗丁宣贲"
                "邓郁单杭洪包诸左石崔吉钮龚程嵇邢滑裴陆荣翁荀羊於惠甄曲家封"
                "芮羿储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋"
                "仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟薄印宿"
                "白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴鬱胥能苍双闻莘党翟谭贡劳"
                "逄姬申扶堵冉宰郦雍卻璩桑桂濮牛寿通边扈燕冀郏浦尚农温别庄晏"
                "柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡"
                "国文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简"
                "饶空曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公")

_PATTERNS = {
    "hair": r"[^，。；！？]{0,14}(?:长发|短发|青丝|白发|银发|乌发|黑发|发髻|束发|披散|散发|马尾|卷发|金发|红发|褐发|双髻|木簪|玉簪|簪|鬓)[^，。；！？]{0,14}",
    "eyes": r"[^，。；！？]{0,14}(?:杏眼|凤眼|丹凤|桃花眼|眸|瞳|眼)[^，。；！？]{0,14}",
    "face": r"[^，。；！？]{0,14}(?:鹅蛋脸|瓜子脸|圆脸|方脸|面容|脸庞|面庞)[^，。；！？]{0,14}",
    "build": r"[^，。；！？]{0,14}(?:纤瘦|清瘦|瘦削|高挑|魁梧|壮硕|身形|身量|个子)[^，。；！？]{0,14}",
    "skin": r"[^，。；！？]{0,14}(?:肤色|肌肤|皮肤|白皙|黝黑|苍白)[^，。；！？]{0,14}",
    "marks": r"[^，。；！？]{0,14}(?:疤痕|伤疤|胎记|纹身|痣|胎印|断指|左眉|右眉)[^，。；！？]{0,14}",
    "outfit": r"[^，。；！？]{0,18}(?:长衫|长袍|衣裙|裙|袍|衣|衫|甲|披风|斗篷|劲装|制服|靴|袖口|腰间束)[^，。；！？]{0,18}",
    "accessories": r"[^，。；！？]{0,14}(?:玉佩|吊坠|耳环|发带|腰牌|香囊|戒指|项链|铜钱)[^，。；！？]{0,14}",
    "weapon": r"[^，。；！？]{0,14}(?:长剑|短刀|佩剑|折扇|弓|枪|匕首|法杖|扇|刀|剑)[^，。；！？]{0,14}",
    "temperament": r"[^，。；！？]{0,12}(?:沉静|清冷|狡黠|温润|凌厉|怯懦|爽朗|寡言|警觉|淡漠|倔强|温和|孤傲)[^，。；！？]{0,12}",
    "palette": r"(?:靛蓝|月白|朱红|墨绿|玄黑|素白|鸦青|绛紫|杏黄|银灰|藏青|赭石)(?:[、与和](?:靛蓝|月白|朱红|墨绿|玄黑|素白|鸦青|绛紫|杏黄|银灰|藏青|赭石))*",
}


def _find_names(text: str) -> list:
    """Find likely character names: surname + 1-2 chars followed by a verb."""
    counts = Counter()
    for i, ch in enumerate(text):
        if ch not in _SURNAMES:
            continue
        for length in (2, 3):
            cand = text[i:i + length]
            if len(cand) < length or not all("\u4e00-\u9fa5" <= c <= "\u9fff" for c in cand):
                continue
            if cand in _STOPWORDS:
                continue
            tail = text[i + length:i + length + 4]
            if any(tail.startswith(v) for v in _VERBS):
                counts[cand] += 1
    ranked = [n for n, _ in counts.most_common(8)]
    # Drop longer candidates that merely extend a shorter, more frequent one.
    out = []
    for name in ranked:
        if any(name != other and name.startswith(other) for other in ranked[:3]):
            continue
        out.append(name)
    return out[:4] or ["未命名角色"]


def now() -> str:
    return store.now()


def new_id(prefix: str = "char") -> str:
    return prefix + "-" + store.new_id()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def list_cards(cfg: dict) -> list:
    with store.connect(cfg) as con:
        return [json.loads(r[0]) for r in con.execute(
            "SELECT data FROM characters ORDER BY updated_at DESC")]


def get_card(cfg: dict, cid: str):
    with store.connect(cfg) as con:
        row = con.execute("SELECT data FROM characters WHERE id=?", (cid,)).fetchone()
    return json.loads(row[0]) if row else None


def blank_card(name: str = "") -> dict:
    return {
        "id": "", "name": name, "aliases": [], "source": "",
        "profile": {k: "" for k, _ in PROFILE_FIELDS},
        "evidence": {}, "confidence": {},
        "anchor": "", "anchor_version": 0, "anchor_history": [],
        "ref_asset_id": "", "mode": "manual",
    }


def _normalise(card: dict) -> dict:
    base = blank_card()
    for key in base:
        if key in card:
            base[key] = card[key]
    profile = {k: "" for k, _ in PROFILE_FIELDS}
    profile.update(base.get("profile") or {})
    profile["art_style"] = profile.get("art_style") or DEFAULT_ART_STYLE
    profile["negative"] = profile.get("negative") or DEFAULT_NEGATIVE
    base["profile"] = profile
    base["name"] = str(base.get("name") or "").strip() or "未命名角色"
    return base


def put_card(cfg: dict, card: dict) -> dict:
    """Insert or update. Changing the anchor bumps anchor_version."""
    card = _normalise(card)
    cid = str(card.get("id") or "").strip()
    existing = get_card(cfg, cid) if cid else None
    if existing:
        card["created_at"] = existing.get("created_at") or now()
        card["anchor_history"] = existing.get("anchor_history") or []
        card["anchor_version"] = int(existing.get("anchor_version") or 0)
        if not card.get("ref_asset_id"):
            card["ref_asset_id"] = existing.get("ref_asset_id") or ""
    else:
        card["id"] = new_id()
        card["created_at"] = now()

    supplied = str(card.get("anchor") or "").strip()
    profile_changed = bool(existing) and card.get("profile") != existing.get("profile")
    anchor_edited = bool(existing) and supplied and supplied != str(existing.get("anchor") or "")
    # Rebuild when the profile changed, unless the user hand-edited the anchor.
    desired = supplied if (supplied and not (profile_changed and not anchor_edited)) \
        else build_anchor(card)
    card["anchor"] = desired
    if not existing or desired != str(existing.get("anchor") or ""):
        if existing and existing.get("anchor"):
            card["anchor_history"] = (card["anchor_history"] + [{
                "version": card["anchor_version"],
                "anchor": existing["anchor"], "saved_at": now(),
            }])[-20:]
        card["anchor_version"] = card["anchor_version"] + 1
    card["updated_at"] = now()
    with store.connect(cfg) as con:
        con.execute(
            "INSERT OR REPLACE INTO characters(id,name,created_at,updated_at,data) "
            "VALUES(?,?,?,?,?)",
            (card["id"], card["name"], card["created_at"], card["updated_at"],
             json.dumps(card, ensure_ascii=False)))
    return card


def delete_card(cfg: dict, cid: str) -> bool:
    with store.connect(cfg) as con:
        cur = con.execute("DELETE FROM characters WHERE id=?", (cid,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Reproducible anchor
# ---------------------------------------------------------------------------
_ANCHOR_LABELS = {
    "age": "Age", "gender": "Gender", "role": "Role", "era": "Setting / era",
    "face": "Face shape", "eyes": "Eyes", "skin": "Skin", "build": "Build",
    "hair": "Hair", "marks": "Distinctive marks", "outfit": "Outfit",
    "accessories": "Accessories", "weapon": "Weapon / prop",
    "palette": "Color palette", "temperament": "Temperament",
    "art_style": "Art direction", "negative": "Avoid",
}


def build_anchor(card: dict) -> str:
    p = card.get("profile") or {}
    lines = ["CHARACTER ANCHOR - keep this block verbatim in every image for this character.",
             "Character: " + str(card.get("name") or "unnamed")]
    if card.get("aliases"):
        lines.append("Also known as: " + ", ".join(map(str, card["aliases"])))
    for key, label in _ANCHOR_LABELS.items():
        value = str(p.get(key) or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    lines += [
        "CONSISTENCY RULES: the exact same face, hairstyle, outfit, accessories, "
        "body proportions and color palette in every view, pose and frame; "
        "do not redesign or reinterpret the character.",
        "COMPOSITION: single full-body character, centered, orthographic, plain "
        "uniform background, evenly lit, no text, no watermark, no logo, no border.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Novel -> traits
# ---------------------------------------------------------------------------
def _text_endpoint(cfg: dict) -> str:
    base = str(cfg.get("api_base") or "https://api.openai.com/v1").rstrip("/")
    return base + "/chat/completions"


def _llm_extract(cfg: dict, text: str) -> list:
    key = config.api_key(cfg)
    payload = {
        "model": str(cfg.get("text_model") or "gpt-5.4-mini"),
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": "小说文本：\n\n" + text[:14000]},
        ],
    }
    req = urllib.request.Request(
        _text_endpoint(cfg),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST")
    with imaging._opener(cfg).open(req, timeout=180) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    content = body["choices"][0]["message"]["content"]
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("LLM did not return a JSON object")
    data = json.loads(content[start:end + 1])
    return data.get("characters") or []


def _clean_quote(raw: str, name: str = "") -> str:
    """Trim punctuation / pronouns so an extracted trait reads like a trait."""
    quote = re.sub(r"\s+", "", raw).strip(" ，。；：！？、\n\"'“”‘’《》()（）")
    if name:
        quote = re.sub(r"^" + re.escape(name), "", quote)
    quote = re.sub(r"^(?:她|他|它|那人|这人|只见|却是|又是|便是|一身|穿着|披着)+",
                   "", quote)
    return quote.strip(" ，。；：！？、\n\"'“”‘’")


def _heuristic_extract(text: str) -> list:
    """Offline fallback: surname+verb name detection + trait dictionaries."""
    ranked = _find_names(text)
    sentences = [s.strip() for s in re.split(r"[。！？\n]", text) if s.strip()]
    out = []
    for name in ranked:
        card = blank_card(name)
        card["mode"] = "heuristic"
        profile, evidence, confidence = card["profile"], card["evidence"], card["confidence"]
        # Only sentences naming this character, plus the one immediately after
        # (Chinese novels usually put the visual description right after).
        keep = []
        for i, sent in enumerate(sentences):
            if name in sent:
                keep.append(sent)
                if i + 1 < len(sentences):
                    keep.append(sentences[i + 1])
        blob = "；".join(keep) or text[:600]
        for field, pattern in _PATTERNS.items():
            found = re.search(pattern, blob)
            if found:
                quote = _clean_quote(found.group(0), name)
                if quote:
                    profile[field] = quote[:110]
                    evidence[field] = quote[:110]
                    confidence[field] = "explicit"
        for field in ("age", "gender", "role", "era", "skin", "build"):
            if not profile.get(field):
                confidence[field] = "missing"
        profile["art_style"] = DEFAULT_ART_STYLE
        profile["negative"] = DEFAULT_NEGATIVE
        confidence["art_style"] = "inferred"
        confidence["negative"] = "inferred"
        card["source"] = text[:160]
        out.append(card)
    return out


def extract(cfg: dict, text: str, mode: str = "auto") -> dict:
    text = (text or "").strip()
    if len(text) < 20:
        return {"characters": [], "mode": "empty",
                "note": "文本太短，请粘贴至少一段小说正文。"}
    wanted = mode if mode in ("llm", "local") else "auto"
    note = "本地启发式提取；请人工核对并补全字段。"
    if wanted in ("auto", "llm") and config.has_key(cfg) and wanted != "local":
        try:
            chars = _llm_extract(cfg, text)
            if chars:
                normalised = []
                for raw in chars[:6]:
                    card = _normalise({
                        "name": raw.get("name", ""),
                        "aliases": raw.get("aliases") or [],
                        "profile": raw.get("profile") or {},
                        "evidence": raw.get("evidence") or {},
                        "confidence": raw.get("confidence") or {},
                        "source": text[:160], "mode": "llm",
                    })
                    normalised.append(card)
                return {"characters": normalised, "mode": "llm",
                        "note": "由文本模型提取；请核对「原文证据」后再保存。"}
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError,
                ValueError, OSError) as exc:
            note = f"文本模型调用失败（{type(exc).__name__}），已自动改用本地启发式。"
    else:
        note = ("未配置 API key，使用本地启发式提取；结果比较粗，"
                "建议配置 key 后用文本模型精提，或直接在表单里补全。")
    chars = []
    for card in _heuristic_extract(text):
        card["source"] = text[:160]
        chars.append(card)
    return {"characters": chars, "mode": "heuristic", "note": note}


# ---------------------------------------------------------------------------
# Asset packs (shared by spritesheets and turnarounds)
# ---------------------------------------------------------------------------
def add_pack(cfg: dict, kind: str, name: str, path: str, data: dict,
             pid: str = "") -> dict:
    rec = {"id": pid or new_id("pack"), "kind": kind, "name": name,
           "path": path, "data": data, "created_at": now()}
    with store.connect(cfg) as con:
        con.execute(
            "INSERT OR REPLACE INTO packs(id,kind,name,created_at,path,data) "
            "VALUES(?,?,?,?,?,?)",
            (rec["id"], kind, name, rec["created_at"], path,
             json.dumps(rec, ensure_ascii=False)))
    return rec


def list_packs(cfg: dict, kind: str = "") -> list:
    with store.connect(cfg) as con:
        if kind:
            cur = con.execute(
                "SELECT data FROM packs WHERE kind=? ORDER BY created_at DESC", (kind,))
        else:
            cur = con.execute("SELECT data FROM packs ORDER BY created_at DESC")
        return [json.loads(r[0]) for r in cur][:100]


def get_pack(cfg: dict, pid: str):
    with store.connect(cfg) as con:
        row = con.execute("SELECT data FROM packs WHERE id=?", (pid,)).fetchone()
    return json.loads(row[0]) if row else None
