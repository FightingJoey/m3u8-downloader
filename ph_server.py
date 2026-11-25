#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地桥接服务：在 m3u8-downloader 页面里输入视频页 URL，
后端按站点取最高可用清晰度 m3u8/MP4，把分片 URL 改写为同源 /proxy，
前端仍用原有 JS 分片下载 + mux-mp4 合成（或直接下载 MP4）。

支持:
  - Pornhub 视频页（download_ph_1080.py）
  - pornhd3x.tv / wwwN.pornhd3x.tv 影片页（download_phd3x.py；代理会剥 PNG 包装的 TS）
  - freepornvideos.xxx 视频页（download_fpv.py；原生 MP4 渐进式下载）
  - whoreshub.com 视频页（download_wh.py；KVS kt_player + 原生 MP4 渐进式下载）

用法:
  python3 ph_server.py
  # 浏览器打开 http://127.0.0.1:8765/
  # 输入 Pornhub / pornhd3x.tv / freepornvideos.xxx / whoreshub.com 视频页链接
"""

from __future__ import annotations

import json
import os
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
    env_proxies,
    extract_video_title,  # re-exported helper
    http_find_1080,
    http_get_bytes,
    is_1080_index_v1,
    quality_height_from_url,
    resolve_1080_media_playlist,
    sanitize_proxy_env,
    viewkey_from_url,
)
from download_phd3x import (  # noqa: E402
    is_phd3x_page,
    resolve_phd3x,
    strip_png_wrapped_ts,
)
from download_fpv import (  # noqa: E402
    is_fpv_page,
    resolve_fpv,
)
from download_wh import (  # noqa: E402
    is_wh_page,
    resolve_wh,
)
from download_hdp import (  # noqa: E402
    is_hdp_page,
    resolve_hdp,
)

HOST = os.environ.get("PH_HOST", "0.0.0.0")
PORT = int(os.environ.get("PH_PORT", "8765"))

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
        msg = "；".join(notes) if notes else "未找到可用 m3u8（可能下架 / 地区限制 / 无 HLS 源）"
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
                "quality": quality_height_from_url(media_url) or None,
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
    quality = quality_height_from_url(media_url) or quality_height_from_url(source)
    return {
        "ok": True,
        "job_id": job_id,
        "playlist_url": f"/cache/{playlist_name}",
        "media_url": media_url,
        "source": source,
        "segment_count": seg_count,
        "quality": quality or None,
        "is_1080_index_v1": is_1080_index_v1(media_url),
        "viewkey": vk,
        "title": title,
        "notes": notes,
    }



def resolve_phd3x_job(page_url: str) -> dict:
    """把 download_phd3x.resolve_phd3x 的结果写成与 Pornhub 相同的 API 形态。"""
    _purge_old_jobs()
    got = resolve_phd3x(page_url)
    if not got.get("ok"):
        return {
            "ok": False,
            "error": got.get("error") or "pornhd3x 解析失败",
            "notes": got.get("notes") or [],
            "title": got.get("title"),
            "episode_id": got.get("episode_id"),
            "quality": got.get("quality"),
        }

    job_id = uuid.uuid4().hex[:12]
    media_url = got["media_url"]
    body = got["body"]
    title = got.get("title")
    rewritten = rewrite_playlist_for_proxy(media_url, body, job_id)
    playlist_name = f"{job_id}_media.m3u8"
    raw_name = f"{job_id}_raw.m3u8"
    (CACHE / raw_name).write_text(body, encoding="utf-8")
    (CACHE / playlist_name).write_text(rewritten, encoding="utf-8")
    (CACHE / f"{job_id}_meta.json").write_text(
        json.dumps(
            {
                "site": "phd3x",
                "page_url": got.get("page_url") or page_url,
                "source": got.get("source"),
                "media_url": media_url,
                "episode_id": got.get("episode_id"),
                "title": title,
                "quality": got.get("quality"),
                "quality_label": got.get("quality_label"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "session": got["session"],
            "referer": None,
            "no_referer": True,
            "site": "phd3x",
            "media_url": media_url,
            "created": time.time(),
            "title": title,
        }

    return {
        "ok": True,
        "job_id": job_id,
        "playlist_url": f"/cache/{playlist_name}",
        "media_url": media_url,
        "source": got.get("source"),
        "segment_count": got.get("segment_count") or 0,
        "quality": got.get("quality") or None,
        "quality_label": got.get("quality_label"),
        "episode_id": got.get("episode_id"),
        "title": title,
        "notes": got.get("notes") or [],
        "site": "phd3x",
    }


def resolve_fpv_job(page_url: str) -> dict:
    """freepornvideos：直接 MP4 下载，无需 m3u8 改写（返回 mp4_url 供前端直接下载）。"""
    _purge_old_jobs()
    got = resolve_fpv(page_url)
    if not got.get("ok"):
        return {
            "ok": False,
            "error": got.get("error") or "freepornvideos 解析失败",
            "notes": got.get("notes") or [],
            "title": got.get("title"),
            "video_id": got.get("video_id"),
            "quality": got.get("quality"),
        }

    job_id = uuid.uuid4().hex[:12]
    mp4_url = got["mp4_url"]
    title = got.get("title")
    quality = got.get("quality")
    actual_w = got.get("actual_width") or 0
    actual_h = got.get("actual_height") or 0

    (CACHE / f"{job_id}_meta.json").write_text(
        json.dumps(
            {
                "site": "fpv",
                "page_url": got.get("page_url") or page_url,
                "mp4_url": mp4_url,
                "video_id": got.get("video_id"),
                "title": title,
                "quality": quality,
                "quality_label": got.get("quality_label"),
                "actual_width": actual_w,
                "actual_height": actual_h,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "session": got["session"],
            "referer": got.get("page_url") or page_url,
            "site": "fpv",
            "mp4_url": mp4_url,
            "created": time.time(),
            "title": title,
        }

    # 返回格式：前端检测到 is_mp4=True 时走直接下载流程
    return {
        "ok": True,
        "is_mp4": True,
        "job_id": job_id,
        "mp4_url": _proxy_path(mp4_url, job_id),
        "quality": quality or None,
        "quality_label": got.get("quality_label"),
        "actual_resolution": f"{actual_w}x{actual_h}" if actual_w and actual_h else None,
        "video_id": got.get("video_id"),
        "title": title,
        "notes": got.get("notes") or [],
        "site": "fpv",
    }


def resolve_hdp_job(page_url: str) -> dict:
    """hdporn.gg：直接 MP4 下载（Video.js + KVS get_file），无需 m3u8 改写。"""
    _purge_old_jobs()
    got = resolve_hdp(page_url)
    if not got.get("ok"):
        return {
            "ok": False,
            "error": got.get("error") or "hdporn.gg 解析失败",
            "notes": got.get("notes") or [],
            "title": got.get("title"),
            "video_id": got.get("video_id"),
            "quality": got.get("quality"),
        }

    job_id = uuid.uuid4().hex[:12]
    mp4_url = got["mp4_url"]
    title = got.get("title")
    quality = got.get("quality")
    actual_w = got.get("actual_width") or 0
    actual_h = got.get("actual_height") or 0

    (CACHE / f"{job_id}_meta.json").write_text(
        json.dumps(
            {
                "site": "hdp",
                "page_url": got.get("page_url") or page_url,
                "mp4_url": mp4_url,
                "video_id": got.get("video_id"),
                "title": title,
                "quality": quality,
                "quality_label": got.get("quality_label"),
                "actual_width": actual_w,
                "actual_height": actual_h,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "session": got["session"],
            "referer": got.get("page_url") or page_url,
            "site": "hdp",
            "mp4_url": mp4_url,
            "created": time.time(),
            "title": title,
        }

    # 返回格式：前端检测到 is_mp4=True 时走直接下载流程
    return {
        "ok": True,
        "is_mp4": True,
        "job_id": job_id,
        "mp4_url": _proxy_path(mp4_url, job_id),
        "quality": quality or None,
        "quality_label": got.get("quality_label"),
        "actual_resolution": f"{actual_w}x{actual_h}" if actual_w and actual_h else None,
        "video_id": got.get("video_id"),
        "title": title,
        "notes": got.get("notes") or [],
        "site": "hdp",
    }


def resolve_wh_job(page_url: str) -> dict:
    """whoreshub：直接 MP4 下载（KVS kt_player + flashvars），无需 m3u8 改写。"""
    _purge_old_jobs()
    got = resolve_wh(page_url)
    if not got.get("ok"):
        return {
            "ok": False,
            "error": got.get("error") or "whoreshub 解析失败",
            "notes": got.get("notes") or [],
            "title": got.get("title"),
            "video_id": got.get("video_id"),
            "quality": got.get("quality"),
        }

    job_id = uuid.uuid4().hex[:12]
    mp4_url = got["mp4_url"]
    title = got.get("title")
    quality = got.get("quality")
    actual_w = got.get("actual_width") or 0
    actual_h = got.get("actual_height") or 0

    (CACHE / f"{job_id}_meta.json").write_text(
        json.dumps(
            {
                "site": "wh",
                "page_url": got.get("page_url") or page_url,
                "mp4_url": mp4_url,
                "video_id": got.get("video_id"),
                "title": title,
                "quality": quality,
                "quality_label": got.get("quality_label"),
                "actual_width": actual_w,
                "actual_height": actual_h,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "session": got["session"],
            "referer": got.get("page_url") or page_url,
            "site": "wh",
            "mp4_url": mp4_url,
            "created": time.time(),
            "title": title,
        }

    # 返回格式：前端检测到 is_mp4=True 时走直接下载流程
    return {
        "ok": True,
        "is_mp4": True,
        "job_id": job_id,
        "mp4_url": _proxy_path(mp4_url, job_id),
        "quality": quality or None,
        "quality_label": got.get("quality_label"),
        "actual_resolution": f"{actual_w}x{actual_h}" if actual_w and actual_h else None,
        "video_id": got.get("video_id"),
        "title": title,
    }


def resolve_page(page_url: str) -> dict:
    page_url = (page_url or "").strip()
    if not page_url.startswith("http"):
        return {"ok": False, "error": "URL 必须以 http/https 开头"}
    if is_hdp_page(page_url):
        return resolve_hdp_job(page_url)
    if is_wh_page(page_url):
        return resolve_wh_job(page_url)
    if is_fpv_page(page_url):
        return resolve_fpv_job(page_url)
    if is_phd3x_page(page_url):
        return resolve_phd3x_job(page_url)
    if _is_ph_page(page_url):
        return resolve_ph(page_url)
    return {
        "ok": False,
        "error": "请输入 Pornhub 视频页（view_video / viewkey / embed）、pornhd3x.tv 影片页（/movies/…）、freepornvideos.xxx 视频页（/videos/…）、whoreshub.com 视频页（/videos/…）或 hdporn.gg 视频页（/videos/…）",
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
                result = resolve_page(unquote(url))
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
            result = resolve_page(url)
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
            return self._send_json(400, {"ok": False, "error": "非法目标 URL（必须 http/https）"})

        # 验证目标 URL 主机名（防 SSRF）
        try:
            from download_fpv import _is_safe_hostname
            parsed_target = urlparse(target)
            target_host = parsed_target.hostname or ""
            # fpvcdn.com / phncdn.com / pornhd3x / cdntrex CDN 等已知合法
            allowed_cdn = ("fpvcdn.com", "phncdn.com", "pornhd3x.tv", "cdntrex.com")
            is_allowed = any(
                target_host == cdn or target_host.endswith("." + cdn)
                for cdn in allowed_cdn
            ) or _is_safe_hostname(target_host)
            if not is_allowed:
                sys.stderr.write(f"[proxy] SSRF blocked: {target_host}\n")
                return self._send_json(403, {"ok": False, "error": f"目标主机名不允许: {target_host}"})
        except Exception as e:
            sys.stderr.write(f"[proxy] hostname validation error: {e}\n")
            return self._send_json(500, {"ok": False, "error": f"主机名验证失败: {e}"})

        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if not job:
            return self._send_json(
                410, {"ok": False, "error": "任务已过期（约 1 小时），请重新解析视频页链接"}
            )

        session = job["session"]
        referer: str = job.get("referer") or ""
        site = job.get("site") or "pornhub"

        # freepornvideos: 支持 Range 请求（流式下载 / 断点续传）+ Cookie 保留
        if site == "fpv":
            range_header = self.headers.get("Range")
            try:
                if range_header and range_header.startswith("bytes="):
                    # Range 请求：解析 bytes=start-end
                    parts = range_header[6:].split("-", 1)
                    start = int(parts[0]) if parts[0] else 0
                    end = int(parts[1]) if len(parts) > 1 and parts[1] else None
                    # fpv session 支持 Range
                    if end is None:
                        # 请求到文件末尾：先 HEAD 获取总大小
                        # 简化：直接用 get_bytes 拉整个（后续可优化为真 Range）
                        _final, data = session.get_bytes(
                            target, headers=session.media_headers(referer=referer)
                        )
                        if start > 0:
                            data = data[start:]
                        # 返回 206 Partial Content
                        self.send_response(206)
                        self.send_header("Content-Type", "video/mp4")
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header("Content-Range", f"bytes {start}-{start+len(data)-1}/*")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Range, Content-Type")
                        self.send_header("Accept-Ranges", "bytes")
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        self.wfile.write(data)
                    else:
                        # 指定 Range
                        data = session.get_range_bytes(target, start, end, referer=referer)
                        self.send_response(206)
                        self.send_header("Content-Type", "video/mp4")
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header("Content-Range", f"bytes {start}-{end}/*")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Range, Content-Type")
                        self.send_header("Accept-Ranges", "bytes")
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        self.wfile.write(data)
                else:
                    # 完整请求
                    _final, data = session.get_bytes(
                        target, headers=session.media_headers(referer=referer)
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Type")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
            except Exception as e:
                sys.stderr.write(f"[proxy] fpv fail {target[:120]}: {e}\n")
                return self._send_json(502, {"ok": False, "error": str(e)})
            return

        # pornhub / phd3x: 现有逻辑
        try:
            if site == "phd3x" or job.get("no_referer"):
                # 播放列表按站点要求不带 Referer；分片同样走该会话
                _final, data = session.get_bytes(
                    target, headers=session.media_headers(omit_referer=True)
                )
            else:
                _final, data = http_get_bytes(
                    session, target, referer=referer, for_media=True
                )
        except Exception as e:
            sys.stderr.write(f"[proxy] fail {target[:120]}: {e}\n")
            return self._send_json(502, {"ok": False, "error": str(e)})

        data, stripped = strip_png_wrapped_ts(data)
        ctype = _media_content_type(target)
        if stripped or (data and data[0:1] == b"\x47" and len(data) >= 188):
            # 剥掉 PNG 包装后必须是 mux-mp4 能吃的 MPEG-TS
            if stripped or ctype in ("application/octet-stream", "image/png"):
                ctype = "video/mp2t"
        elif data[:4] in (b"\x00\x00\x00\x18", b"\x00\x00\x00\x1c") or data[4:8] in (b"ftyp", b"styp", b"moof"):
            if ctype == "application/octet-stream":
                ctype = "video/mp4"
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


def _safe_proxy_url(v: str) -> str:
    """Strip userinfo from a proxy URL for logs."""
    try:
        p = urlparse(v)
        if p.hostname:
            host = p.hostname
            port = f":{p.port}" if p.port else ""
            scheme = f"{p.scheme}://" if p.scheme else ""
            return f"{scheme}{host}{port}"
        return v.split("@")[-1]
    except Exception:
        return v.split("@")[-1]


def _proxy_env_summary() -> str:
    """One-line proxy status after sanitize (host:port only, no credentials)."""
    proxies = env_proxies()
    if not proxies:
        return "(未设置；直连，国内常因墙出现 SSL UNEXPECTED_EOF)"
    order = ("https", "http", "all", "no")
    seen: list[str] = []
    for scheme in order:
        v = proxies.get(scheme)
        if not v:
            continue
        label = "NO_PROXY" if scheme == "no" else f"{scheme.upper()}_PROXY"
        seen.append(f"{label}={_safe_proxy_url(v)}")
    return ", ".join(seen)


def main() -> int:
    # Empty lowercase http_proxy='' (Compose ${http_proxy:-}) wipes uppercase in getproxies().
    before = __import__("urllib.request").request.getproxies()
    removed = sanitize_proxy_env()
    after = env_proxies()
    if removed and after and not before:
        print(
            "[warn] 已清除空的小写代理变量 "
            f"({', '.join(removed)})；此前 getproxies() 为空，HTTPS 会直连失败"
        )

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[ok] Pornhub / pornhd3x / freepornvideos + m3u8-downloader 桥接服务")
    print(f"[ok] 打开: http://{HOST}:{PORT}/")
    print(f"[ok] 输入 Pornhub / pornhd3x.tv / freepornvideos.xxx 视频页 URL，点「转码为MP4下载」")
    print(f"[ok] 取链: download_ph_1080.py / download_phd3x.py / download_fpv.py；分片+合成: index.html / mux-mp4.js")
    print(f"[ok] 代理环境: {_proxy_env_summary()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[info] 已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
