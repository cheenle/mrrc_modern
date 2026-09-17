#!/usr/bin/env python3
"""MRRC 支持包接收端（仅标准库，适合放在最小化服务器上）。

接口（nginx 用 /mrrc_modern/support/ 反代到本服务的 /）：

    POST /api/create            创建记录，返回 {"ok": true, "id": "YYYYmmdd-HHMMSS-xxxx"}
    PUT  /api/<id>/bundle       原始 zip body（Content-Length ≤ SUPPORT_MAX_MB）
    GET  /api/list              需 Basic Auth；HTML 列表（含问题描述/大小/版本）
    GET  /api/<id>/bundle       需 Basic Auth；下载

环境变量：
    SUPPORT_DIR       存储根目录（默认 /var/www/support-modern）
    SUPPORT_PASSWORD  读取列表/下载的口令（必填，不设则拒绝启动）
    SUPPORT_USER      用户名（默认 mrrc）
    SUPPORT_PORT      监听端口（默认 8098；0 = 随机端口，供测试）
    SUPPORT_PRODUCT   写进 meta.json 的产品名（默认 mrrc_modern）
    SUPPORT_MAX_MB    单包上限 MB（默认 20）

安全要点：id 白名单正则、只写 SUPPORT_DIR/<id>/、限速（每 IP 每分钟 ≤5 次 create）、
列表与下载需口令、不执行上传内容。
"""

# ── Vendored from the sibling project `mrrc` (tools/support_receiver/server.py,
#    262 lines) on 2026-09-17, spec docs/superpowers/specs/2026-09-17-support-bundle-design.md §9.
#    Local changes, deliberately minimal so the two copies stay comparable:
#      1. SUPPORT_DIR default -> /var/www/support-modern (own storage, outside the docroot);
#      2. SUPPORT_PORT default 8099 -> 8098 (MRRC Modern owns this port on the same host);
#      3. `product` is recorded in meta.json (own systemd unit, own password file).
#    Interface unchanged: POST /api/create, PUT /api/<id>/bundle, GET /api/list
#    (Basic auth), GET /api/<id>/bundle (Basic auth).

import base64
import hmac
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
DIR = os.environ.get("SUPPORT_DIR", "/var/www/support-modern")
USER = os.environ.get("SUPPORT_USER", "mrrc")
PASSWORD = os.environ.get("SUPPORT_PASSWORD", "")
PRODUCT = os.environ.get("SUPPORT_PRODUCT", "mrrc_modern")
MAX_BYTES = int(os.environ.get("SUPPORT_MAX_MB", "20")) * 1024 * 1024
RATE_PER_MINUTE = int(os.environ.get("SUPPORT_RATE_PER_MINUTE", "5"))
_RATE = {}


class Handler(BaseHTTPRequestHandler):
    server_version = "MRRC-Support/1.0"

    # ---- 基础工具 ----
    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            user, _, pwd = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except Exception:
            return False
        return hmac.compare_digest(user, USER) and hmac.compare_digest(pwd, PASSWORD)

    def _require_auth(self):
        if self._authed():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="MRRC Support"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _rate_ok(self):
        now = time.time()
        bucket = [t for t in _RATE.get(self.client_address[0], []) if now - t < 60]
        bucket.append(now)
        _RATE[self.client_address[0]] = bucket
        return len(bucket) <= RATE_PER_MINUTE

    # ---- POST /api/create ----
    def do_POST(self):
        path = self.path.rstrip("/")
        if path.endswith("/delete"):                      # 列表页的删除按钮走这里
            if not self._require_auth():
                return
            match = re.match(r"^/api/([^/]+)/delete$", path)
            if not match or not ID_RE.match(match.group(1)):
                return self._json(400, {"ok": False, "reason": "bad_id"})
            folder = os.path.join(DIR, match.group(1))
            if not os.path.isdir(folder):
                return self._json(404, {"ok": False, "reason": "unknown_id"})
            for name in os.listdir(folder):
                try:
                    os.remove(os.path.join(folder, name))
                except OSError:
                    pass
            try:
                os.rmdir(folder)
            except OSError:
                pass
            self.send_response(303)                       # 删完回列表页
            self.send_header("Location", "/mrrc/support/api/list")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path != "/api/create":
            return self._json(404, {"ok": False, "reason": "not_found"})
        if not self._rate_ok():
            return self._json(429, {"ok": False, "reason": "rate_limited"})
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
        except ValueError:
            length = 0
        try:
            meta = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(meta, dict):
                meta = {}
        except Exception:
            meta = {}
        rid = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
        folder = os.path.join(DIR, rid)
        os.makedirs(folder, exist_ok=True)
        record = {"id": rid, "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
                  "remote": self.client_address[0],
                  "problem": str(meta.get("problem", ""))[:2000],
                  "contact": str(meta.get("contact", ""))[:200],
                  "version": str(meta.get("version", ""))[:40],
                  "product": PRODUCT}
        with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=2)
        return self._json(200, {"ok": True, "id": rid})

    # ---- PUT /api/<id>/bundle ----
    def do_DELETE(self):
        """删除某条支持包（需口令）。路径与 PUT 同形，避免额外路由。"""
        if not self._require_auth():
            return
        match = re.match(r"^/api/([^/]+)/bundle$", self.path or "")
        if not match or not ID_RE.match(match.group(1)):
            return self._json(400, {"ok": False, "reason": "bad_id"})
        folder = os.path.join(DIR, match.group(1))
        if not os.path.isdir(folder):
            return self._json(404, {"ok": False, "reason": "unknown_id"})
        for name in os.listdir(folder):
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass
        try:
            os.rmdir(folder)
        except OSError:
            pass
        return self._json(200, {"ok": True, "id": match.group(1)})

    def do_PUT(self):
        match = re.match(r"^/api/([^/]+)/bundle$", self.path or "")
        if not match or not ID_RE.match(match.group(1)):
            # 含 ../ 之类的非法路径也落到这里（400），绝不拼接用户输入到磁盘路径
            return self._json(400, {"ok": False, "reason": "bad_id"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BYTES:
            # 有界丢弃请求体：直接回 413 会让客户端"写入中断（broken pipe）"。
            # 只读到上限附近就够，避免被超大 Content-Length 拖住（读完即关闭连接）。
            # 完整读走（封顶 8×上限）：否则客户端剩下的字节会撞上已关闭的连接（RST）。
            # 超过封顶（真正的恶意超大 body）才直接断开。
            remaining = min(length, 8 * MAX_BYTES)
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            self.close_connection = True
            return self._json(413, {"ok": False, "reason": "too_large"})
        folder = os.path.join(DIR, match.group(1))
        if not os.path.isdir(folder):
            return self._json(404, {"ok": False, "reason": "unknown_id"})
        data = self.rfile.read(length)
        with open(os.path.join(folder, "bundle.zip"), "wb") as fh:
            fh.write(data)
        return self._json(200, {"ok": True, "size": length})

    # ---- GET /api/list, /api/<id>/bundle ----
    def do_GET(self):
        if not self._require_auth():
            return
        if self.path.rstrip("/") in ("/api/list", "/api", ""):
            return self._render_list()
        match = re.match(r"^/api/([^/]+)/bundle$", self.path or "")
        if match and ID_RE.match(match.group(1)):
            path = os.path.join(DIR, match.group(1), "bundle.zip")
            if os.path.isfile(path):
                data = open(path, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="support-{match.group(1)}.zip"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _render_list(self):
        rows = []
        try:
            entries = sorted(os.listdir(DIR), reverse=True)[:200]
        except OSError:
            entries = []
        for rid in entries:
            folder = os.path.join(DIR, rid)
            if not ID_RE.match(rid) or not os.path.isdir(folder):
                continue
            try:
                meta = json.load(open(os.path.join(folder, "meta.json"), encoding="utf-8"))
            except Exception:
                meta = {}
            bundle = os.path.join(folder, "bundle.zip")
            size = os.path.getsize(bundle) if os.path.isfile(bundle) else 0
            rows.append(
                f"<li><b>{rid}</b> · {meta.get('product', '?')} "
                f"{meta.get('version', '?')} · {size / 1024:.0f} KB · "
                f"{meta.get('remote', '?')}<br>"
                f"<span class='p'>{meta.get('problem', '(无描述)')}</span><br>"
                f"<a href='/api/{rid}/bundle'>下载包</a> "
                f"<form method='post' action='/api/{rid}/delete' style='display:inline'"
                f" onsubmit=\"return confirm('删除 {rid}？')\">"
                f"<button type='submit' style='background:#a33;color:#fff;border:0;"
                f"border-radius:3px;padding:2px 8px;cursor:pointer'>删除</button></form></li>")
        body = ("<!doctype html><html lang='zh'><meta charset='utf-8'>"
                "<title>MRRC 支持包</title><style>body{font:14px/1.6 system-ui,sans-serif;"
                "background:#111;color:#ddd;max-width:900px;margin:24px auto}li{margin:14px 0}"
                ".p{color:#8ac}</style><h2>MRRC 支持包（%d）</h2><ul>%s</ul></html>"
                % (len(rows), "".join(rows) or "<li>暂无</li>")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.client_address[0], fmt % args))
        sys.stderr.flush()


def main():
    if not PASSWORD:
        print("SUPPORT_PASSWORD 未设置，拒绝启动", file=sys.stderr)
        return 2
    os.makedirs(DIR, exist_ok=True)
    port = int(os.environ.get("SUPPORT_PORT", "8098"))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"listening on {httpd.server_address[0]}:{httpd.server_address[1]}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
