#!/usr/bin/env python3
"""Scene cards: reusable, versioned environment definitions for comic panels.

A scene is a first-class asset, just like a character: its definition, the
exact prompt, model/quality/size and the generated image id are all stored so
the same environment can be reproduced or reused in another story.
"""
from __future__ import annotations

import json
from typing import Iterable

from . import imaging, store

NO_TEXT = ("no text, no letters, no captions, no subtitles, no seal, no watermark, "
           "no logo, no panel borders, no split lines, no comic frame")
NO_PEOPLE = ("no people, no human silhouettes, no figures, no hands, "
             "standalone empty environment")


def put(cfg: dict, scene: dict) -> dict:
    """Insert or update a scene card (keeps the original created_at)."""
    sid = str(scene.get("id") or "").strip()
    if not sid:
        raise ValueError("场景卡缺少 id")
    old = get(cfg, sid) or {}
    rec = dict(old)
    rec.update(scene)
    rec["id"] = sid
    rec["created_at"] = old.get("created_at") or store.now()
    rec["updated_at"] = store.now()
    with store.connect(cfg) as con:
        con.execute(
            "INSERT OR REPLACE INTO scenes(id,name,created_at,data) VALUES(?,?,?,?)",
            (sid, str(rec.get("name") or sid), rec["created_at"],
             json.dumps(rec, ensure_ascii=False)))
    return rec


def get(cfg: dict, sid: str) -> dict | None:
    with store.connect(cfg) as con:
        row = con.execute("SELECT data FROM scenes WHERE id=?", (sid,)).fetchone()
    return json.loads(row[0]) if row else None


def list_cards(cfg: dict) -> list[dict]:
    with store.connect(cfg) as con:
        rows = con.execute(
            "SELECT data FROM scenes ORDER BY created_at DESC, id DESC").fetchall()
    return [json.loads(r[0]) for r in rows]


def delete(cfg: dict, sid: str) -> None:
    with store.connect(cfg) as con:
        con.execute("DELETE FROM scenes WHERE id=?", (sid,))


def find_asset(cfg: dict, sid: str) -> dict | None:
    """Return the indexed scene image for a scene card, if it exists."""
    for row in store.iter_all(cfg):
        if row.get("asset_kind") == "scene" and row.get("scene_id") == sid \
                and not row.get("demo"):
            return row
    return None


def build_prompt(cfg: dict, scene: dict, *, style_id: str,
                 visual_rules: str = "") -> str:
    """Style bible + scene description + hard no-text/no-people constraints."""
    style = imaging.load_style(cfg, style_id)
    if not style:
        raise ValueError("未知画风：" + style_id)
    extra = ", ".join(x.strip().rstrip(".") for x in (
        str(scene.get("prompt_extra") or ""),
        str(visual_rules or ""),
        NO_PEOPLE, NO_TEXT,
    ) if x.strip())
    return imaging.build_prompt(
        style,
        slots={"asset_kind": "standalone environment concept art, empty location",
               "subject": str(scene.get("name") or ""),
               "mood": str(scene.get("mood") or "")},
        extra=extra)


def panel_prompt(cfg: dict, panel: dict, scene: dict, *, style_id: str,
                 visual_rules: str = "", anchors: Iterable[str] = ()) -> str:
    """Style bible + character anchors + scene continuity + one-frame rules."""
    style = imaging.load_style(cfg, style_id)
    if not style:
        raise ValueError("未知画风：" + style_id)
    anchor_text = "\n".join(a for a in anchors if a)
    parts = [
        str(visual_rules or ""),
        "SINGLE COMIC PANEL: exactly one continuous illustration, one moment in time, "
        "no panel borders, no split lines, no multi-panel layout, no inset frames",
        "CONTINUITY: reuse the established environment - " +
        str(scene.get("prompt_extra") or scene.get("description") or ""),
        str(panel.get("visual") or ""),
        "Composition: " + str(panel.get("composition") or "wide"),
        NO_TEXT,
    ]
    base = imaging.build_prompt(
        style, extra=", ".join(p.strip().rstrip(".") for p in parts if p.strip()))
    if not anchor_text:
        return base
    # Anchors must be verbatim. Keep them as a prefix so the style builder's
    # rstrip(".") can never clip the last character of a frozen character card.
    return ("CHARACTER ANCHORS - keep these character designs exactly:\n"
            + anchor_text + "\n\nSCENE AND STYLE:\n" + base)
