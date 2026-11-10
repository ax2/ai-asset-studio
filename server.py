#!/usr/bin/env python3
"""ai-asset-studio local server. Binds to loopback only, no third-party packages."""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from studio import config, exporter, imaging, pricing, store  # noqa: E402

CFG: dict = {}
MAX_BODY = 2 * 1024 * 1024
NO_KEY_MSG = "尚未设置 API key。到「连接设置」粘贴会话密钥，或设置环境变量 OPENAI_API_KEY。"

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
            elif path.startswith("/media/"):
                self._media(path[len("/media/"):])
            else:
                self._static(path)
        except Exception as exc:
            traceback.print_exc()
            self._err(500, str(exc))

    def _api(self, method: str, path: str, p: dict):
        seg = [s for s in path.split("/") if s][1:]

        if method == "GET" and seg == ["config"]:
            return self._json(200, config.public(CFG))

        # Session key: process memory only. Never written to disk, never logged,
        # never returned to the browser, gone when the server restarts.
        if method == "POST" and seg == ["key"]:
            value = str((self._body() or {}).get("api_key") or "").strip()
            if len(value) < 20:
                return self._err(400, "API key 看起来不完整（少于 20 字符），未保存。")
            config.set_session_key(value)
            return self._json(200, {
                "ok": True,
                "source": config.key_source(CFG),
                "message": "会话密钥已载入内存：不写文件、不进 git，重启服务后自动失效。",
            })
        if method == "DELETE" and seg == ["key"]:
            config.clear_session_key()
            return self._json(200, {"ok": True, "source": config.key_source(CFG)})
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
            rows = store.query(
                CFG, q=p.get("q", ""), style=p.get("style", ""), tag=p.get("tag", ""),
                status=p.get("status", ""), favorite=p.get("favorite") == "1",
                min_rating=int(p.get("min_rating") or 0),
                sort=p.get("sort", "newest"), limit=int(p.get("limit") or 500))
            return self._json(200, {"assets": rows, "count": len(rows)})
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
        return self._err(404, "unknown endpoint")

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
        rows = store.query(
            CFG, q=b.get("q", ""), style=b.get("style_id", ""),
            status=b.get("status", ""), tag=b.get("tag", ""),
            favorite=bool(b.get("favorite")), min_rating=int(b.get("min_rating") or 0),
            sort=b.get("sort", "newest"), limit=int(b.get("limit") or 500))
        if b.get("ids"):
            wanted = set(b["ids"])
            rows = [r for r in rows if r.get("id") in wanted]
        return self._json(200, exporter.export_zip(CFG, rows, b.get("name")))

    def _media(self, rel: str):
        base = CFG["local"].resolve()
        target = (base / rel).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            return self._err(404, "not found")
        ctype = MIME.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0]
        return self._send(200, target.read_bytes(), ctype or "application/octet-stream")

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
    print("API key: %s | source: %s (dry-run works without it)" %
          ("set" if config.has_key(CFG) else "NOT set", config.key_source(CFG)))
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
