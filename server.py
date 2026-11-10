#!/usr/bin/env python3
"""ai-asset-studio local server. Binds to loopback only, no third-party packages."""
from __future__ import annotations

import argparse
import json
import mimetypes
import shutil
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from studio import (characters, config, exporter, imaging, pricing, sprites,  # noqa: E402
                    store, thumbs, turnaround)

CFG: dict = {}
MAX_BODY = 2 * 1024 * 1024
PAGE_SIZE = 60
NO_KEY_MSG = ("尚未设置 API key。到「连接设置」粘贴密钥并写入 config.json 或记住到本机，"
              "也可以设置环境变量 OPENAI_API_KEY。")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json; charset=utf-8",
}


def _slot_matrix(style: dict, count: int) -> list:
    slots = style.get("slots", {})
    keys = ("subject", "asset_kind", "mood", "concept")
    out = []
    for i in range(count):
        picked = {}
        for key in keys:
            values = slots.get(key) or []
            if values:
                picked[key] = values[i % len(values)]
        out.append(picked)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "ai-asset-studio/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

    def _err(self, code: int, msg):
        self._json(code, {"ok": False, "error": str(msg)})

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if path.startswith("/api/"):
                self._api(method, path, params)
            elif path.startswith("/thumb/"):
                self._thumb(path[len("/thumb/"):])
            elif path.startswith("/media/"):
                self._media(path[len("/media/"):])
            elif path.startswith("/pack/"):
                self._pack_file(path[len("/pack/"):])
            else:
                self._static(path)
        except Exception as exc:
            traceback.print_exc()
            self._err(500, str(exc))

    def _api(self, method: str, path: str, p: dict):
        seg = [s for s in path.split("/") if s][1:]

        if method == "GET" and seg == ["config"]:
            return self._json(200, config.public(CFG))

        # Key precedence: session memory > encrypted disk > environment variable.
        # The key itself is never returned by any endpoint, only a masked hint.
        if method == "POST" and seg == ["key"]:
            b = self._body() or {}
            value = str(b.get("api_key") or "").strip()
            if len(value) < 20:
                return self._err(400, "API key 看起来不完整（少于 20 字符），未保存。")
            saved_now = bool(b.get("persist"))
            try:
                cred = config.set_session_key(value, persist=saved_now)
            except OSError as exc:
                return self._err(500, f"密钥已载入内存，但写入本机凭据失败：{exc}")
            return self._json(200, {
                "ok": True,
                "source": config.key_source(CFG),
                "persisted": cred["persisted"],
                "credentials": cred,
                "message": ("密钥已加密保存到本机，重启服务后自动生效。"
                            if saved_now else "会话密钥已载入内存，重启服务后失效。"),
            })
        if method == "POST" and seg == ["key", "persist"]:
            try:
                cred = config.persist_current_key(CFG)
            except ValueError as exc:
                return self._err(400, str(exc))
            return self._json(200, {
                "ok": True, "credentials": cred,
                "message": "当前密钥已加密保存到本机，重启服务后自动生效。",
            })
        if method == "DELETE" and seg == ["key", "persist"]:
            cred = config.clear_persisted_key()
            return self._json(200, {
                "ok": True, "source": config.key_source(CFG), "credentials": cred,
                "message": "已删除本机保存的密钥；内存中的会话密钥仍可用到重启。",
            })
        if method == "DELETE" and seg == ["key"]:
            config.clear_session_key()
            return self._json(200, {"ok": True, "source": config.key_source(CFG)})
        if method == "POST" and seg == ["key", "file"]:
            b = self._body() or {}
            value = str(b.get("api_key") or "").strip()
            try:
                res = config.write_file_key(value)
            except ValueError as exc:
                return self._err(400, str(exc))
            CFG["api_key"] = value          # make it live without a restart
            return self._json(200, dict(res, source=config.key_source(CFG),
                                        config=config.public(CFG),
                                        message=("已明文写入 config.json，重启服务后自动生效。"
                                                 if res["git_ignored"] else
                                                 "已写入，但警告：当前 .gitignore 未排除 config.json！")))
        if method == "DELETE" and seg == ["key", "file"]:
            res = config.clear_file_key()
            CFG.pop("api_key", None)
            return self._json(200, dict(res, source=config.key_source(CFG),
                                        config=config.public(CFG),
                                        message=("已删除 config.json 里的明文密钥。"
                                                 if res["removed"] else
                                                 "config.json 里本来就没有明文密钥。")))
        if method == "POST" and seg == ["key", "check"]:
            return self._json(200, imaging.check_connection(CFG))
        if method == "POST" and seg == ["settings"]:
            try:
                changed = config.apply_runtime(CFG, self._body() or {})
            except ValueError as exc:
                return self._err(400, str(exc))
            return self._json(200, {
                "ok": True, "changed": changed, "config": config.public(CFG),
            })

        if method == "GET" and seg == ["pricing"]:
            return self._json(200, pricing.price_table())
        if method == "GET" and seg == ["stats"]:
            return self._json(200, store.stats(CFG))
        if method == "GET" and seg == ["health"]:
            return self._json(200, exporter.health(CFG))
        if method == "GET" and seg == ["styles"]:
            return self._json(200, {"styles": imaging.load_styles(CFG)})
        if method == "GET" and len(seg) == 2 and seg[0] == "styles":
            style = imaging.load_style(CFG, seg[1])
            return self._json(200, style) if style else self._err(404, "style not found")
        if method == "PUT" and len(seg) == 2 and seg[0] == "styles":
            return self._json(200, imaging.save_style(CFG, seg[1], self._body()))
        if method == "POST" and len(seg) == 3 and seg[0] == "styles" and seg[2] == "version":
            b = self._body()
            rec = store.add_version(CFG, style_id=seg[1], asset_id=b.get("asset_id"),
                                    prompt=b.get("prompt", ""), blocks=b.get("blocks", {}),
                                    note=b.get("note", ""))
            return self._json(200, rec)
        if method == "GET" and seg == ["versions"]:
            return self._json(200, {"versions": store.versions(CFG, p.get("style"))})
        if method == "GET" and seg == ["assets"]:
            try:
                limit = int(p.get("limit") or PAGE_SIZE)
                offset = int(p.get("offset") or 0)
            except ValueError:
                limit, offset = PAGE_SIZE, 0
            rows, total = store.query_page(
                CFG, q=p.get("q", ""), style=p.get("style", ""), tag=p.get("tag", ""),
                status=p.get("status", ""), favorite=p.get("favorite") == "1",
                min_rating=int(p.get("min_rating") or 0),
                character_id=p.get("character_id", ""),
                sort=p.get("sort", "newest"), limit=limit, offset=offset)
            return self._json(200, {
                "assets": rows, "count": len(rows), "total": total,
                "offset": offset, "limit": limit,
                "has_more": offset + len(rows) < total,
            })
        if method == "GET" and len(seg) == 2 and seg[0] == "assets":
            row = store.get(CFG, seg[1])
            return self._json(200, row) if row else self._err(404, "asset not found")
        if method == "PATCH" and len(seg) == 2 and seg[0] == "assets":
            row = store.update(CFG, seg[1], self._body())
            return self._json(200, row) if row else self._err(404, "asset not found")
        if method == "POST" and len(seg) == 3 and seg[0] == "assets" and seg[2] == "reproduce":
            return self._reproduce(seg[1])
        if (method == "POST" and len(seg) == 3 and seg[0] == "assets"
                and seg[2] in ("keep", "reject", "restore")):
            status = {"keep": "keeper", "reject": "rejected", "restore": "raw"}[seg[2]]
            row = store.update(CFG, seg[1], {"status": status})
            return self._json(200, row) if row else self._err(404, "asset not found")
        if method == "POST" and seg == ["generate"]:
            return self._generate()
        if method == "POST" and seg == ["sync"]:
            b = self._body()
            return self._json(200, exporter.sync(
                CFG, b.get("style_id"), b.get("include_demo")))
        if method == "POST" and seg == ["export"]:
            return self._export_pack()
        if method == "GET" and seg == ["exports"]:
            folder = CFG["local"] / "export"
            files = [{"name": f.name, "size": f.stat().st_size}
                     for f in sorted(folder.glob("*.zip"), reverse=True)]
            return self._json(200, {"exports": files[:30]})

        # ---- Game asset pipeline: characters, turnarounds, sprites, packs ----
        if method == "GET" and seg == ["game", "meta"]:
            return self._json(200, {
                "profile_fields": [{"key": k, "label": v}
                                   for k, v in characters.PROFILE_FIELDS],
                "actions": [{"key": k, "label": v} for k, v in sprites.ACTIONS.items()],
                "engines": sprites.ENGINES,
                "views": [{"key": k, "label": v} for k, v, _ in turnaround.VIEWS],
                "turnaround_modes": [
                    {"key": "sheet", "label": "一次生成四视图设定图后切分（省、一致性最好）"},
                    {"key": "separate", "label": "逐视图生成（可用参考图保风格，4 倍成本）"},
                ],
                "default_cell": int(CFG.get("sprite_cell") or 128),
                "default_model": CFG.get("default_model"),
                "edit_model": CFG.get("edit_model"),
                "default_quality": CFG.get("default_quality"),
                "has_key": config.has_key(CFG),
            })

        if method == "GET" and seg == ["characters"]:
            return self._json(200, {"characters": characters.list_cards(CFG)})
        if method == "POST" and seg == ["characters", "extract"]:
            b = self._body()
            return self._json(200, characters.extract(
                CFG, str(b.get("text") or ""), str(b.get("mode") or "auto")))
        if method == "POST" and seg == ["characters"]:
            return self._json(200, characters.put_card(CFG, self._body() or {}))
        if method == "GET" and len(seg) == 2 and seg[0] == "characters":
            card = characters.get_card(CFG, seg[1])
            return self._json(200, card) if card else self._err(404, "character not found")
        if method == "DELETE" and len(seg) == 2 and seg[0] == "characters":
            ok = characters.delete_card(CFG, seg[1])
            return self._json(200, {"ok": ok}) if ok else self._err(404, "character not found")
        if method == "GET" and len(seg) == 3 and seg[0] == "characters" and seg[2] == "prompt":
            card = characters.get_card(CFG, seg[1])
            if not card:
                return self._err(404, "character not found")
            view = p.get("view", "")
            return self._json(200, {
                "anchor": card.get("anchor") or "",
                "anchor_version": card.get("anchor_version"),
                "prompt": (turnaround.build_prompt(CFG, card, view)
                           if view else turnaround.build_single_prompt(CFG, card, p.get("extra", ""))),
            })
        if (method == "POST" and len(seg) == 3 and seg[0] == "characters"
                and seg[2] == "reproduce"):
            return self._character_reproduce(seg[1])
        if (method == "POST" and len(seg) == 3 and seg[0] == "characters"
                and seg[2] == "turnaround"):
            return self._turnaround(seg[1])

        if method == "POST" and seg == ["sprites"]:
            return self._sprite_pack()
        if method == "GET" and seg == ["packs"]:
            return self._json(200, {"packs": characters.list_packs(CFG, p.get("kind", ""))})
        if method == "GET" and len(seg) == 3 and seg[0] == "packs" and seg[2] == "zip":
            return self._pack_zip(seg[1])
        if method == "GET" and len(seg) == 2 and seg[0] == "packs":
            pack = characters.get_pack(CFG, seg[1])
            return self._json(200, pack) if pack else self._err(404, "pack not found")
        if method == "DELETE" and len(seg) == 2 and seg[0] == "packs":
            return self._delete_pack(seg[1])
        return self._err(404, "unknown endpoint")

    def _character_reproduce(self, cid: str):
        b = self._body()
        dry = bool(b.get("dry_run", True))
        if not dry and not config.has_key(CFG):
            return self._err(400, NO_KEY_MSG)
        try:
            res = turnaround.reproduce(
                CFG, character_id=cid, extra=str(b.get("extra") or ""),
                ref_asset_id=str(b.get("ref_asset_id") or ""), dry_run=dry,
                model=b.get("model") or None, quality=b.get("quality") or None,
                size=b.get("size") or None)
        except ValueError as exc:
            return self._err(400, str(exc))
        return self._json(200, res)

    def _turnaround(self, cid: str):
        b = self._body()
        dry = bool(b.get("dry_run", True))
        if not dry and not config.has_key(CFG):
            return self._err(400, NO_KEY_MSG)
        try:
            res = turnaround.build(
                CFG, character_id=cid, mode=str(b.get("mode") or "sheet"),
                ref_asset_id=str(b.get("ref_asset_id") or ""), dry_run=dry,
                model=b.get("model") or None, quality=b.get("quality") or None,
                size=b.get("size") or None, name=str(b.get("name") or ""))
        except ValueError as exc:
            return self._err(400, str(exc))
        return self._json(200, {"ok": True, "pack": res})

    def _sprite_pack(self):
        b = self._body()
        asset = store.get(CFG, str(b.get("asset_id") or ""))
        if not asset:
            return self._err(404, "asset not found")
        try:
            res = sprites.build(
                CFG, asset=asset, actions=b.get("actions"),
                frames_per_action=int(b.get("frames_per_action") or 8),
                cell=int(b.get("cell") or CFG.get("sprite_cell") or 128),
                cols=int(b.get("cols") or 8), fps=int(b.get("fps") or 8),
                bg=str(b.get("bg") or "auto"),
                tolerance=int(b.get("tolerance") or 40),
                engines=b.get("engines"), name=str(b.get("name") or ""))
        except (ValueError, TypeError) as exc:
            return self._err(400, str(exc))
        return self._json(200, {"ok": True, "pack": res})

    def _pack_zip(self, pid: str):
        pack = characters.get_pack(CFG, pid)
        if not pack:
            return self._err(404, "pack not found")
        path = Path(str(pack.get("path") or ""))
        base = (CFG["local"] / "packs").resolve()
        if not path.is_file() or not path.resolve().is_relative_to(base):
            return self._err(404, "pack zip missing")
        return self._download(path.read_bytes(), path.name, "application/zip")

    def _delete_pack(self, pid: str):
        pack = characters.get_pack(CFG, pid)
        if not pack:
            return self._err(404, "pack not found")
        base = (CFG["local"] / "packs").resolve()
        for raw in (pack.get("path"), (pack.get("data") or {}).get("dir"),
                    (pack.get("data") or {}).get("zip")):
            if not raw:
                continue
            target = Path(str(raw)).resolve()
            if not target.is_relative_to(base) or target == base:
                continue
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.is_file():
                target.unlink(missing_ok=True)
        with store.connect(CFG) as con:
            con.execute("DELETE FROM packs WHERE id=?", (pid,))
        return self._json(200, {"ok": True, "deleted": pid})

    def _download(self, body: bytes, filename: str, ctype: str):
        safe = "".join(c for c in filename if c.isalnum() or c in ".-_") or "pack.zip"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{safe}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _pack_file(self, rel: str):
        """Serve files inside one generated pack (preview.html, sheet.png, views/)."""
        base = (CFG["local"] / "packs").resolve()
        target = (base / rel).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            return self._err(404, "not found")
        ctype = MIME.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0]
        return self._send(200, target.read_bytes(), ctype or "application/octet-stream")

    def _reproduce(self, asset_id: str):
        original = store.get(CFG, asset_id)
        if not original:
            return self._err(404, "asset not found")
        b = self._body()
        if not bool(b.get("dry_run", True)) and not config.has_key(CFG):
            return self._err(400, NO_KEY_MSG)
        prompt = (b.get("prompt") or "").strip() or original.get("prompt", "")
        edited = prompt != original.get("prompt", "")
        if edited:
            store.add_version(CFG, style_id=original["style_id"], asset_id=asset_id,
                              prompt=prompt,
                              blocks=b.get("blocks") or original.get("blocks") or {},
                              note="edited before reproduce")
        res = imaging.generate(
            CFG, style_id=original["style_id"], prompt=prompt,
            model=b.get("model") or original.get("model"),
            quality=b.get("quality") or original.get("quality"),
            size=b.get("size") or original.get("size"),
            dry_run=bool(b.get("dry_run", True)),
            batch_estimate=bool(b.get("batch_estimate")),
            source="reproduce", parent_id=asset_id,
            asset_kind=original.get("asset_kind", ""))
        if edited:
            version = len(store.versions(CFG, original["style_id"]))
            store.update(CFG, res["asset"]["id"], {"prompt_version": version})
        return self._json(200, res)

    def _generate(self):
        b = self._body()
        style_id = b.get("style_id") or "retro-film-diary"
        style = imaging.load_style(CFG, style_id)
        if not style:
            return self._err(404, "style not found")
        count = max(1, min(int(b.get("count") or 1), 50))
        model = b.get("model") or style.get("model", {}).get("default") or CFG["default_model"]
        quality = b.get("quality") or style.get("model", {}).get("quality") or CFG["default_quality"]
        dry = bool(b.get("dry_run", True))
        if not dry and not config.has_key(CFG):
            return self._err(400, NO_KEY_MSG)
        batch_est = bool(b.get("batch_estimate"))
        est = pricing.estimate(model, quality, count, batch=batch_est)
        cap = float((style.get("budget") or {}).get("per_run_cap_usd") or 2)

        if not dry:
            if est > cap:
                return self._err(400, f"单次预估 ${est} 超过该风格上限 ${cap}，请减少数量或降低质量档")
            if store.stats(CFG)["real_cost_usd"] + est > float(CFG.get("monthly_budget_usd", 100)):
                return self._err(400, "会超出月度预算，请先调整数量或提高预算")

        custom = b.get("prompts") or []
        extra = b.get("extra") or ""
        created = []
        if custom:
            for prompt in custom[:count]:
                res = imaging.generate(CFG, style_id=style_id, prompt=str(prompt),
                                       model=model, quality=quality, dry_run=dry,
                                       batch_estimate=batch_est)
                created.append(res["asset"])
        else:
            for slots in _slot_matrix(style, count):
                res = imaging.generate(CFG, style_id=style_id, slots=slots, extra=extra,
                                       model=model, quality=quality, dry_run=dry,
                                       batch_estimate=batch_est)
                created.append(res["asset"])
        return self._json(200, {"ok": True, "created": created, "count": len(created),
                                "estimate_usd": est, "dry_run": dry})

    def _export_pack(self):
        b = self._body()
        # Stream from SQLite - no 500-row ceiling, works with very large packs.
        rows = store.iter_matching(
            CFG, q=b.get("q", ""), style=b.get("style_id", ""),
            status=b.get("status", ""), tag=b.get("tag", ""),
            favorite=bool(b.get("favorite")), min_rating=int(b.get("min_rating") or 0),
            sort=b.get("sort", "newest"))
        if b.get("ids"):
            wanted = set(b["ids"])
            rows = (r for r in rows if r.get("id") in wanted)
        return self._json(200, exporter.export_zip(CFG, rows, b.get("name")))

    def _media(self, rel: str):
        base = CFG["local"].resolve()
        target = (base / rel).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            return self._err(404, "not found")
        ctype = MIME.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0]
        return self._send(200, target.read_bytes(), ctype or "application/octet-stream")

    def _thumb(self, rel: str):
        """Serve a small cached PNG; fall back to the original if unsupported."""
        base = CFG["local"].resolve()
        target = (base / rel).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            return self._err(404, "not found")
        cached = thumbs.ensure(CFG, rel)
        if cached and cached.is_file():
            return self._send(200, cached.read_bytes(), "image/png")
        return self._media(rel)

    def _static(self, path: str):
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        base = CFG["web_dir"].resolve()
        target = (base / name).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            return self._err(404, "not found")
        ctype = MIME.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0]
        return self._send(200, target.read_bytes(), ctype or "application/octet-stream")


def main():
    global CFG
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    CFG = config.load()

    ap = argparse.ArgumentParser(description="ai-asset-studio local server")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()
    host = args.host or str(CFG["host"])
    port = args.port or int(CFG["port"])

    ThreadingHTTPServer.daemon_threads = True
    server = ThreadingHTTPServer((host, port), Handler)
    print("ai-asset-studio ready: http://%s:%d" % (host, port))
    print("API key: %s | source: %s | persisted on disk: %s" %
          ("set" if config.has_key(CFG) else "NOT set",
           config.key_source(CFG),
           "yes" if config.public(CFG)["api_key_persisted"] else "no"))
    print("API proxy: %s" % (CFG.get("api_proxy") or "(system default)"))
    print("Asset root: %s | writable: %s" %
          (CFG["asset_root_path"], config.asset_root_writable(CFG)))
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
