"""Append-friendly JSONL index for assets and prompt versions."""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path

_LOCK = threading.RLock()


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _index(cfg: dict) -> Path:
    return cfg["local"] / "meta.jsonl"


def _versions(cfg: dict) -> Path:
    return cfg["local"] / "prompt_versions.jsonl"


def _read(path: Path) -> list:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _write(path: Path, rows: list) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def load(cfg: dict) -> list:
    with _LOCK:
        return _read(_index(cfg))


def save(cfg: dict, rows: list) -> list:
    with _LOCK:
        _write(_index(cfg), rows)
    return rows


def add(cfg: dict, record: dict) -> dict:
    with _LOCK:
        rows = _read(_index(cfg))
        rows.append(record)
        _write(_index(cfg), rows)
    return record


def get(cfg: dict, asset_id: str):
    return next((r for r in load(cfg) if r.get("id") == asset_id), None)


_ALLOWED_PATCH = {
    "rating", "tags", "favorite", "note", "status", "prompt", "blocks",
    "slots", "style_id", "asset_kind", "prompt_version",
}


def update(cfg: dict, asset_id: str, patch: dict):
    with _LOCK:
        rows = _read(_index(cfg))
        for row in rows:
            if row.get("id") == asset_id:
                for key, value in patch.items():
                    if key in _ALLOWED_PATCH:
                        row[key] = value
                row["updated_at"] = now()
                _write(_index(cfg), rows)
                return row
    return None


def query(cfg: dict, *, q="", style="", tag="", status="", favorite=False,
          min_rating=0, sort="newest", limit=500) -> list:
    rows = load(cfg)
    needle = (q or "").strip().lower()
    out = []
    for r in rows:
        if style and r.get("style_id") != style:
            continue
        if status and r.get("status") != status:
            continue
        if tag and tag not in (r.get("tags") or []):
            continue
        if favorite and not r.get("favorite"):
            continue
        if min_rating and int(r.get("rating") or 0) < int(min_rating):
            continue
        if needle:
            hay = " ".join(str(r.get(k) or "") for k in
                           ("prompt", "note", "style_id", "asset_kind", "id"))
            hay += " " + " ".join(r.get("tags") or [])
            if needle not in hay.lower():
                continue
        out.append(r)

    reverse = True
    key = "created_at"
    if sort == "oldest":
        reverse = False
    elif sort == "rating":
        key, reverse = "rating", True
    elif sort == "cost":
        key, reverse = "cost_usd", True
    out.sort(key=lambda r: (r.get(key) or 0 if key != "created_at" else r.get(key) or ""),
             reverse=reverse)
    return out[:limit]


def add_version(cfg: dict, *, style_id: str, asset_id: str | None, prompt: str,
                blocks: dict, note: str = "") -> dict:
    rec = {
        "id": new_id(),
        "style_id": style_id,
        "asset_id": asset_id,
        "prompt": prompt,
        "blocks": blocks,
        "note": note,
        "created_at": now(),
    }
    with _LOCK:
        rows = _read(_versions(cfg))
        rows.append(rec)
        _write(_versions(cfg), rows)
    return rec


def versions(cfg: dict, style_id: str | None = None) -> list:
    rows = _read(_versions(cfg))
    if style_id:
        rows = [r for r in rows if r.get("style_id") == style_id]
    return list(reversed(rows))


def stats(cfg: dict) -> dict:
    rows = load(cfg)
    by_style, by_model, by_month = {}, {}, {}
    real_cost = 0.0
    for r in rows:
        sid = r.get("style_id") or "unknown"
        by_style[sid] = by_style.get(sid, 0) + 1
        model = r.get("model") or "unknown"
        by_model[model] = by_model.get(model, 0) + 1
        month = str(r.get("created_at") or "")[:7]
        if month:
            by_month[month] = round(by_month.get(month, 0) + float(r.get("cost_usd") or 0), 4)
        if not r.get("demo"):
            real_cost += float(r.get("cost_usd") or 0)
    return {
        "total": len(rows),
        "keepers": sum(1 for r in rows if r.get("status") == "keeper"),
        "demos": sum(1 for r in rows if r.get("demo")),
        "rejected": sum(1 for r in rows if r.get("status") == "rejected"),
        "favorites": sum(1 for r in rows if r.get("favorite")),
        "real_cost_usd": round(real_cost, 4),
        "by_style": by_style,
        "by_model": by_model,
        "by_month": by_month,
        "budget_usd": cfg.get("monthly_budget_usd", 100),
        "remaining_usd": round(float(cfg.get("monthly_budget_usd", 100)) - real_cost, 4),
    }
