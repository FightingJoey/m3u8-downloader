#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地桥接服务：在 m3u8-downloader 页面里输入 Pornhub 视频页 URL，
后端调用 download_ph_1080.py 取 1080P index-v1-a1.m3u8，
把分片 URL 改写为同源 /proxy，前端仍用原有 JS 分片下载 + mux-mp4 合成。

用法:
  python3 ph_server.py
  # 浏览器打开 http://127.0.0.1:8765/
  # 输入 https://cn.pornhub.com/view_video.php?viewkey=XXXX
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import traceback
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
CACHE.mkdir(exist_ok=True)

# 复用已验证的 Pornhub 取链逻辑
sys.path.insert(0, str(ROOT))
from download_ph_1080 import (  # noqa: E402
    BrowserSession,
    extract_video_title,  # re-exported helper
    http_find_1080,
    http_get_bytes,
    is_1080_index_v1,
    resolve_1080_media_playlist,
    viewkey_from_url,
)

HOST = "127.0.0.1"
PORT = 8765

# job_id -> {session, referer, media_url, created}
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL_SEC = 3600


def _purge_old_jobs() -> None:
    now = time.time()
    with _JOBS_LOCK:
        dead = [k for k, v in _JOBS.items() if now - v.get("created", 0) > _JOB_TTL_SEC]
        for k in dead:
            _JOBS.pop(k, None)
            for p in CACHE.glob(f"{k}*"):
                try:
                    p.unlink()
                except OSError:
                    pass


def _is_ph_page(url: str) -> bool:
    u = url.lower()
    return "pornhub." in u and ("view_video.php" in u or "/embed/" in u or "viewkey=" in u)


def _abs_uri(base: str, maybe_rel: str) -> str:
    maybe_rel = maybe_rel.strip().strip('"').strip("'")
    if maybe_rel.startswith("http://") or maybe_rel.startswith("https://"):
        return maybe_rel
    return urljoin(base if base.endswith("/") else base.rsplit("/", 1)[0] + "/", maybe_rel)


def _proxy_path(abs_url: str, job_id: str) -> str:
    # 绝对路径；前端 applyURL 会拼上 origin。勿对 u 使用 safe 保留字符，避免 & 拆参。
    return f"/proxy?job={quote(job_id, safe='')}&u={quote(abs_url, safe='')}"


def _query_param_once(query: str, key: str) -> str:
    """从 query string 取参数并只 unquote 一次（parse_qs 后再 unquote 会破坏 CDN 签名里的 %3D）。"""
    prefix = key + "="
    for part in query.split("&"):
        if part.startswith(prefix):
            return unquote(part[len(prefix) :])
    return ""


def _media_content_type(url: str) -> str:
    path = urlparse(url).path.lower()
    if path.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if path.endswith(".ts"):
        return "video/mp2t"
    if path.endswith(".m4s") or path.endswith(".mp4") or path.endswith(".cmfv"):
        return "video/mp4"
    if path.endswith(".aac") or path.endswith(".m4a"):
        return "audio/mp4"
    if path.endswith(".key"):
        return "application/octet-stream"
    # Pornhub 路径常含 .mp4/seg-*.ts，不能用 "mp4" in url
    return "application/octet-stream"


def rewrite_playlist_for_proxy(media_url: str, body: str, job_id: str) -> str:
    """把 m3u8 里的分片 / KEY / MAP 改成走本地 /proxy，前端即可同源下载。"""
    base = media_url.rsplit("/", 1)[0] + "/"
    out_lines: list[str] = []

    def rewrite_attr_uris(line: str) -> str:
        def repl(m: re.Match[str]) -> str:
            abs_u = _abs_uri(base, m.group(1))
            return f'URI="{_proxy_path(abs_u, job_id)}"'

        return re.sub(r'URI="([^"]+)"', repl, line)

    for raw in body.splitlines():
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped:
            out_lines.append(line)
            continue
        if stripped.startswith("#"):
            if "URI=" in stripped and (
                stripped.startswith("#EXT-X-KEY")
                or stripped.startswith("#EXT-X-MAP")
                or stripped.startswith("#EXT-X-MEDIA")
                or stripped.startswith("#EXT-X-SESSION-KEY")
            ):
                out_lines.append(rewrite_attr_uris(line))
            else:
                out_lines.append(line)
            continue
        # 媒体分片或子播放列表 URI
        abs_u = _abs_uri(base, stripped)
        out_lines.append(_proxy_path(abs_u, job_id))
    return "\n".join(out_lines) + ("\n" if body.endswith("\n") else "")


def resolve_ph(page_url: str) -> dict:
    _purge_old_jobs()
    page_url = page_url.strip()
    if not page_url.startswith("http"):
        return {"ok": False, "error": "URL 必须以 http/https 开头"}
    if not _is_ph_page(page_url):
        return {"ok": False, "error": "不是 Pornhub 视频页链接（需含 view_video / viewkey / embed）"}

    session = BrowserSession(page_url)
    source, referer, notes, title = http_find_1080(session, page_url)
    if not source:
        msg = "；".join(notes) if notes else "未找到 1080P m3u8（可能无 1080 / 下架 / 地区限制）"
        return {"ok": False, "error": msg, "notes": notes, "title": title}

    try:
        media_url, body = resolve_1080_media_playlist(session, source, referer)
    except Exception as e:
        return {
            "ok": False,
            "error": f"解析媒体播放列表失败: {e}",
            "notes": notes,
            "title": title,
        }

    job_id = uuid.uuid4().hex[:12]
    rewritten = rewrite_playlist_for_proxy(media_url, body, job_id)
    playlist_name = f"{job_id}_index-v1-a1.m3u8"
    raw_name = f"{job_id}_raw.m3u8"
    vk = viewkey_from_url(page_url)
    (CACHE / raw_name).write_text(body, encoding="utf-8")
    (CACHE / playlist_name).write_text(rewritten, encoding="utf-8")
    (CACHE / f"{job_id}_meta.json").write_text(
        json.dumps(
            {
                "page_url": page_url,
                "source": source,
                "media_url": media_url,
                "referer": referer,
                "viewkey": vk,
                "title": title,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "session": session,
            "referer": referer or session.referer or page_url,
            "media_url": media_url,
            "created": time.time(),
            "title": title,
        }

    seg_count = sum(
        1
        for ln in body.splitlines()
        if ln.strip() and not ln.strip().startswith("#") and ".m3u8" not in ln
    )
    return {
        "ok": True,
        "job_id": job_id,
        "playlist_url": f"/cache/{playlist_name}",
        "media_url": media_url,
        "source": source,
        "segment_count": seg_count,
        "is_1080_index_v1": is_1080_index_v1(media_url),
        "viewkey": vk,
        "title": title,
        "notes": notes,
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[http] " + (fmt % args) + "\n")

    def _send_json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, code: int, data: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Range, Accept")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/resolve":
            qs = parse_qs(parsed.query)
            url = (qs.get("url") or [""])[0]
            if not url:
                return self._send_json(400, {"ok": False, "error": "缺少 url 参数"})
            try:
                result = resolve_ph(unquote(url))
                return self._send_json(200 if result.get("ok") else 502, result)
            except Exception as e:
                traceback.print_exc()
                return self._send_json(500, {"ok": False, "error": str(e)})

        if path == "/api/health":
            return self._send_json(200, {"ok": True, "jobs": len(_JOBS)})

        if path == "/proxy":
            return self._handle_proxy(parsed.query)

        # /cache/* 播放列表：显式返回，带 CORS，避免被当成下载附件
        if path.startswith("/cache/") and path.endswith(".m3u8"):
            fp = ROOT / path.lstrip("/")
            if not fp.is_file():
                return self.send_error(404, "playlist not found")
            data = fp.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return

        # 其它静态资源
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/resolve":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return self._send_json(400, {"ok": False, "error": "JSON 无效"})
        url = (body.get("url") or "").strip()
        if not url:
            return self._send_json(400, {"ok": False, "error": "缺少 url"})
        try:
            result = resolve_ph(url)
            return self._send_json(200 if result.get("ok") else 502, result)
        except Exception as e:
            traceback.print_exc()
            return self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_proxy(self, query: str) -> None:
        job_id = _query_param_once(query, "job")
        target = _query_param_once(query, "u")
        if not job_id or not target:
            return self._send_json(400, {"ok": False, "error": "缺少 job 或 u"})
        if not target.startswith("http"):
            return self._send_json(400, {"ok": False, "error": "非法目标 URL"})

        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if not job:
            return self._send_json(
                410, {"ok": False, "error": "任务已过期，请重新解析 Pornhub 链接"}
            )

        session: BrowserSession = job["session"]
        referer: str = job["referer"]
        try:
            _final, data = http_get_bytes(
                session, target, referer=referer, for_media=True
            )
        except Exception as e:
            sys.stderr.write(f"[proxy] fail {target[:120]}: {e}\n")
            return self._send_json(502, {"ok": False, "error": str(e)})

        ctype = _media_content_type(target)
        # 同源 XHR；补齐 CORS，便于 file:// 或其它端口调试
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Type")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Accept-Ranges", "none")
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[ok] Pornhub + m3u8-downloader 桥接服务")
    print(f"[ok] 打开: http://{HOST}:{PORT}/")
    print(f"[ok] 输入 Pornhub 视频页 URL，点「转码为MP4下载」")
    print(f"[ok] 取链逻辑: download_ph_1080.py；分片+合成: 原 index.html / mux-mp4.js")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[info] 已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
