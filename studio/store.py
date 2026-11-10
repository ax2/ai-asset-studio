"""SQLite-backed asset index.

Why SQLite instead of JSONL: every write used to rewrite the whole index and
every query loaded every asset into Python. That is O(n) per image and falls
over at a few thousand pictures. Here the full record is still stored as JSON
in `data` (so no field is ever lost), but the filterable columns are indexed
and filtering / sorting / counting / pagination are pushed into SQL.

The public function signatures match the original JSONL implementation, so
server.py, exporter.py and the tools keep working unchanged.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

_LOCK = threading.RLock()
_ENSURED: set[str] = set()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
  id          TEXT PRIMARY KEY,
  created_at  TEXT NOT NULL DEFAULT '',
  style_id    TEXT NOT NULL DEFAULT '',
  status      TEXT NOT NULL DEFAULT 'raw',
  rating      INTEGER NOT NULL DEFAULT 0,
  favorite    INTEGER NOT NULL DEFAULT 0,
  demo        INTEGER NOT NULL DEFAULT 0,
  cost_usd    REAL NOT NULL DEFAULT 0,
  model       TEXT NOT NULL DEFAULT '',
  file        TEXT NOT NULL DEFAULT '',
  search      TEXT NOT NULL DEFAULT '',
  data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assets_created ON assets(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_assets_style   ON assets(style_id);
CREATE INDEX IF NOT EXISTS idx_assets_status  ON assets(status);
CREATE INDEX IF NOT EXISTS idx_assets_rating  ON assets(rating);
CREATE INDEX IF NOT EXISTS idx_assets_cost    ON assets(cost_usd);
CREATE INDEX IF NOT EXISTS idx_assets_demo    ON assets(demo);
CREATE TABLE IF NOT EXISTS asset_tags (
  asset_id TEXT NOT NULL,
  tag      TEXT NOT NULL,
  PRIMARY KEY (asset_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON asset_tags(tag);
CREATE TABLE IF NOT EXISTS prompt_versions (
  id          TEXT PRIMARY KEY,
  style_id    TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL DEFAULT '',
  data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_versions_style ON prompt_versions(style_id, created_at DESC);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

_ORDER = {
    "newest": "created_at DESC, id DESC",
    "oldest": "created_at ASC, id ASC",
    "rating": "rating DESC, created_at DESC, id DESC",
    "cost": "cost_usd DESC, created_at DESC, id DESC",
}

_ALLOWED_PATCH = {
    "rating", "tags", "favorite", "note", "status", "prompt", "blocks",
    "slots", "style_id", "asset_kind", "prompt_version", "label",
}


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _db_path(cfg: dict) -> Path:
    return cfg["local"] / "assets.db"


def _connect(cfg: dict) -> sqlite3.Connection:
    con = sqlite3.connect(str(_db_path(cfg)), timeout=20, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=20000")
    return con


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _legacy_rows(path: Path) -> list:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _search_text(record: dict) -> str:
    parts = [str(record.get(k) or "") for k in
             ("prompt", "note", "style_id", "style_name", "asset_kind",
              "id", "label", "filename", "model")]
    parts.extend(str(t) for t in (record.get("tags") or []))
    return " ".join(parts).lower()


def _upsert(con: sqlite3.Connection, record: dict) -> None:
    rid = str(record.get("id") or "")
    if not rid:
        raise ValueError("asset record is missing an id")
    payload = json.dumps(record, ensure_ascii=False)
    con.execute(
        "INSERT OR REPLACE INTO assets "
        "(id,created_at,style_id,status,rating,favorite,demo,cost_usd,model,file,search,data) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, str(record.get("created_at") or ""), str(record.get("style_id") or ""),
         str(record.get("status") or "raw"), _int(record.get("rating")),
         1 if record.get("favorite") else 0, 1 if record.get("demo") else 0,
         _float(record.get("cost_usd")), str(record.get("model") or ""),
         str(record.get("file") or ""), _search_text(record), payload))
    con.execute("DELETE FROM asset_tags WHERE asset_id=?", (rid,))
    tags = [str(t).strip() for t in (record.get("tags") or []) if str(t).strip()]
    if tags:
        con.executemany("INSERT OR IGNORE INTO asset_tags(asset_id,tag) VALUES(?,?)",
                        [(rid, t) for t in tags])


def ensure(cfg: dict) -> dict:
    """Create the schema once, then import legacy JSONL files a single time.

    The JSONL files are kept in place as a human-readable backup.
    """
    key = str(_db_path(cfg))
    if key in _ENSURED:
        return {"migrated_assets": 0, "migrated_versions": 0, "already": True}
    with _LOCK:
        con = _connect(cfg)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(_SCHEMA)
            done = con.execute("SELECT v FROM meta WHERE k='jsonl_migrated'").fetchone()
            n_assets = n_versions = 0
            if done is None:
                for record in _legacy_rows(cfg["local"] / "meta.jsonl"):
                    try:
                        _upsert(con, record)
                        n_assets += 1
                    except (ValueError, sqlite3.Error):
                        continue
                for record in _legacy_rows(cfg["local"] / "prompt_versions.jsonl"):
                    con.execute(
                        "INSERT OR REPLACE INTO prompt_versions(id,style_id,created_at,data) "
                        "VALUES(?,?,?,?)",
                        (str(record.get("id") or new_id()), str(record.get("style_id") or ""),
                         str(record.get("created_at") or ""),
                         json.dumps(record, ensure_ascii=False)))
                    n_versions += 1
                con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('jsonl_migrated',?)",
                            (now(),))
            con.commit()
        finally:
            con.close()
        _ENSURED.add(key)
        return {"migrated_assets": n_assets, "migrated_versions": n_versions,
                "already": False}


def load(cfg: dict) -> list:
    """Full scan - kept for tools and migrations. Prefer query_page/iter_*."""
    with _connect(cfg) as con:
        return [json.loads(r[0]) for r in
                con.execute("SELECT data FROM assets ORDER BY created_at, id")]


def save(cfg: dict, rows: list) -> list:
    """Replace the whole index (used by import/prune tools)."""
    with _connect(cfg) as con:
        con.execute("DELETE FROM assets")
        con.execute("DELETE FROM asset_tags")
        for record in rows:
            _upsert(con, record)
    return rows


def add(cfg: dict, record: dict) -> dict:
    with _connect(cfg) as con:
        _upsert(con, record)
    return record


def get(cfg: dict, asset_id: str):
    with _connect(cfg) as con:
        row = con.execute("SELECT data FROM assets WHERE id=?", (asset_id,)).fetchone()
    return json.loads(row[0]) if row else None


def update(cfg: dict, asset_id: str, patch: dict):
    with _connect(cfg) as con:
        row = con.execute("SELECT data FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not row:
            return None
        record = json.loads(row[0])
        for key, value in patch.items():
            if key in _ALLOWED_PATCH:
                record[key] = value
        record["updated_at"] = now()
        _upsert(con, record)
    return record


def _where(q="", style="", tag="", status="", favorite=False, min_rating=0):
    clauses, args = [], []
    if style:
        clauses.append("style_id=?")
        args.append(style)
    if status:
        clauses.append("status=?")
        args.append(status)
    if favorite:
        clauses.append("favorite=1")
    if min_rating:
        clauses.append("rating>=?")
        args.append(_int(min_rating))
    if tag:
        clauses.append("id IN (SELECT asset_id FROM asset_tags WHERE tag=?)")
        args.append(tag)
    needle = (q or "").strip().lower()
    if needle:
        needle = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("search LIKE ? ESCAPE '\\'")
        args.append("%" + needle + "%")
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", args


def query_page(cfg: dict, *, q="", style="", tag="", status="", favorite=False,
               min_rating=0, sort="newest", limit=60, offset=0) -> tuple[list, int]:
    """Return (rows, total). Filtering, counting and ordering all happen in SQL."""
    where, args = _where(q, style, tag, status, favorite, min_rating)
    order = _ORDER.get(sort, _ORDER["newest"])
    limit = max(1, min(int(limit or 60), 2000))
    offset = max(0, int(offset or 0))
    with _connect(cfg) as con:
        total = con.execute("SELECT COUNT(*) FROM assets" + where, args).fetchone()[0]
        rows = [json.loads(r[0]) for r in con.execute(
            "SELECT data FROM assets" + where + " ORDER BY " + order + " LIMIT ? OFFSET ?",
            args + [limit, offset])]
    return rows, total


def query(cfg: dict, *, limit=500, **kwargs) -> list:
    """Backwards-compatible wrapper returning just the rows."""
    rows, _ = query_page(cfg, limit=limit, **kwargs)
    return rows


def iter_matching(cfg: dict, batch_size: int = 500, **kwargs):
    """Stream every matching row without an artificial 500-row ceiling."""
    offset = 0
    while True:
        rows, total = query_page(cfg, limit=batch_size, offset=offset, **kwargs)
        for row in rows:
            yield row
        offset += len(rows)
        if not rows or offset >= total:
            break


def iter_all(cfg: dict, batch_size: int = 500):
    yield from iter_matching(cfg, batch_size=batch_size, sort="oldest")


def add_version(cfg: dict, *, style_id: str, asset_id: str | None, prompt: str,
                blocks: dict, note: str = "") -> dict:
    rec = {
        "id": new_id(), "style_id": style_id, "asset_id": asset_id,
        "prompt": prompt, "blocks": blocks, "note": note, "created_at": now(),
    }
    with _connect(cfg) as con:
        con.execute(
            "INSERT OR REPLACE INTO prompt_versions(id,style_id,created_at,data) "
            "VALUES(?,?,?,?)",
            (rec["id"], str(style_id or ""), rec["created_at"],
             json.dumps(rec, ensure_ascii=False)))
    return rec


def versions(cfg: dict, style_id: str | None = None) -> list:
    with _connect(cfg) as con:
        if style_id:
            cur = con.execute(
                "SELECT data FROM prompt_versions WHERE style_id=? "
                "ORDER BY created_at DESC, id DESC", (style_id,))
        else:
            cur = con.execute(
                "SELECT data FROM prompt_versions ORDER BY created_at DESC, id DESC")
        return [json.loads(r[0]) for r in cur]


def stats(cfg: dict) -> dict:
    with _connect(cfg) as con:
        total = con.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        keepers = con.execute("SELECT COUNT(*) FROM assets WHERE status='keeper'").fetchone()[0]
        demos = con.execute("SELECT COUNT(*) FROM assets WHERE demo=1").fetchone()[0]
        rejected = con.execute("SELECT COUNT(*) FROM assets WHERE status='rejected'").fetchone()[0]
        favorites = con.execute("SELECT COUNT(*) FROM assets WHERE favorite=1").fetchone()[0]
        real_cost = con.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM assets WHERE demo=0").fetchone()[0]
        by_style = {r[0]: r[1] for r in con.execute(
            "SELECT style_id, COUNT(*) FROM assets GROUP BY style_id")}
        by_model = {r[0]: r[1] for r in con.execute(
            "SELECT model, COUNT(*) FROM assets GROUP BY model")}
        by_month = {r[0]: round(r[1] or 0, 4) for r in con.execute(
            "SELECT substr(created_at,1,7), SUM(cost_usd) FROM assets "
            "WHERE created_at<>'' GROUP BY substr(created_at,1,7)")}
        tag_count = con.execute("SELECT COUNT(DISTINCT tag) FROM asset_tags").fetchone()[0]
    return {
        "total": total,
        "keepers": keepers,
        "demos": demos,
        "rejected": rejected,
        "favorites": favorites,
        "real_cost_usd": round(real_cost or 0, 4),
        "by_style": by_style,
        "by_model": by_model,
        "by_month": by_month,
        "distinct_tags": tag_count,
        "storage": "sqlite",
        "budget_usd": cfg.get("monthly_budget_usd", 100),
        "remaining_usd": round(float(cfg.get("monthly_budget_usd", 100)) - (real_cost or 0), 4),
    }
