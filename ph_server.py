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
from download_1porn import (  # noqa: E402
    is_1porn_page,
    resolve_1porn_job,
)
from download_fpx import (  # noqa: E402
    is_fpx_page,
    resolve_fpx_job,
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


# 各站点适配器白名单的并集。/proxy 必须用这一份，不能只用 download_fpv 的名单，
# 否则 whoreshub.com / hdporn.gg / 1porn.tv / okcdn.ru 的 get_file 或 CDN 会被 SSRF 拦成 403。
_PROXY_ALLOWED_SUFFIXES = (
    "freepornvideos.xxx",
    "fpvcdn.com",
    "whoreshub.com",
    "cdntrex.com",
    "hdporn.gg",
    "ahcdn.com",
    "1porn.tv",
    "fullpornxxx.net",
    "ok.ru",
    "okcdn.ru",
    "phncdn.com",
    "pornhd3x.tv",
)
_MP4_SITES = frozenset({"fpv", "wh", "hdp", "1porn", "fpx"})
def _redact_url(url: str) -> str:
    """Strip short-lived tokens from a URL or a log line that contains one."""
    # Regex (not urlparse) so access-log lines are not rewritten as URLs.
    out = re.sub(
        r"(v-acctoken(?:=|%3D|%3d))[^&\s%]+",
        r"\1REDACTED",
        url,
        flags=re.I,
    )
    out = re.sub(r"(kt_ips=)[^;\s&%]+", r"\1REDACTED", out, flags=re.I)
    out = re.sub(
        r"((?:acc)?token(?:=|%3D|%3d))[^&\s%]+",
        r"\1REDACTED",
        out,
        flags=re.I,
    )
    return out


def _proxy_host_allowed(hostname: str) -> bool:
    """Tight SSRF allowlist for /proxy targets (page get_file hosts + known CDNs)."""
    host = (hostname or "").lower().strip()
    if not host or host in {"localhost", "localhost.localdomain"}:
        return False
    try:
        import ipaddress
        addr = ipaddress.ip_address(host)
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast:
            return False
        return False  # raw IPs are never in the domain allowlist
    except ValueError:
        pass
    for domain in _PROXY_ALLOWED_SUFFIXES:
        if host == domain or host.endswith("." + domain):
            return True
    return False


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


def resolve_fpx_job_wrapper(page_url: str) -> dict:
    """fullpornxxx.net：直接 MP4 下载（OK.ru API + AES 解密）"""
    _purge_old_jobs()
    try:
        got = resolve_fpx_job(page_url)
    except Exception as e:
        return {"ok": False, "error": f"fullpornxxx.net 解析失败: {e}"}
    if not got.get("job_id"):
        return {
            "ok": False,
            "error": "fullpornxxx.net 解析失败",
        }
    
    job_id = got["job_id"]
    mp4_url = got["url"]
    title = got.get("title")
    quality_label = got.get("quality_label")
    quality_note = got.get("quality_note", "")
    
    (CACHE / f"{job_id}_meta.json").write_text(
        json.dumps(
            {
                "site": "fpx",
                "page_url": got.get("page_url") or page_url,
                "mp4_url": mp4_url,
                "title": title,
                "quality_label": quality_label,
                "quality_note": quality_note,
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
            "site": "fpx",
            "mp4_url": mp4_url,
            "created": time.time(),
            "title": title,
        }
    
    return {
        "ok": True,
        "is_mp4": True,
        "job_id": job_id,
        "mp4_url": _proxy_path(mp4_url, job_id),
        "title": title,
        "quality": quality_note,
        "quality_label": quality_label,
        "site": "fpx",
    }


def resolve_1porn_job_wrapper(page_url: str) -> dict:
    """1porn.tv：直接 MP4 下载（Video.js + KVS get_file），支持语言前缀。"""
    _purge_old_jobs()
    try:
        got = resolve_1porn_job(page_url)
    except Exception as e:
        return {"ok": False, "error": f"1porn.tv 解析失败: {e}"}
    if not got.get("job_id"):
        return {
            "ok": False,
            "error": "1porn.tv 解析失败",
        }
    
    job_id = got["job_id"]
    mp4_url = got["url"]
    title = got.get("title")
    quality_label = got.get("quality_label")
    quality_note = got.get("quality_note", "")
    
    (CACHE / f"{job_id}_meta.json").write_text(
        json.dumps(
            {
                "site": "1porn",
                "page_url": got.get("page_url") or page_url,
                "mp4_url": mp4_url,
                "title": title,
                "quality_label": quality_label,
                "quality_note": quality_note,
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
            "site": "1porn",
            "mp4_url": mp4_url,
            "created": time.time(),
            "title": title,
        }
    
    return {
        "ok": True,
        "is_mp4": True,
        "job_id": job_id,
        "mp4_url": _proxy_path(mp4_url, job_id),
        "title": title,
        "quality": quality_note,
        "quality_label": quality_label,
        "site": "1porn",
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
        "notes": got.get("notes") or [],
        "site": "wh",
    }


def resolve_page(page_url: str) -> dict:
    page_url = (page_url or "").strip()
    if not page_url.startswith("http"):
        return {"ok": False, "error": "URL 必须以 http/https 开头"}
    if is_fpx_page(page_url):
        return resolve_fpx_job_wrapper(page_url)
    if is_1porn_page(page_url):
        return resolve_1porn_job_wrapper(page_url)
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
        "error": "请输入 Pornhub、pornhd3x.tv、freepornvideos.xxx、whoreshub.com、hdporn.gg、1porn.tv 或 fullpornxxx.net 的视频页链接",
    }



def _mp4_request_headers(session, site: str, target: str, referer: str, range_header: str | None) -> dict[str, str]:
    """Build media headers. Never send Origin to okcdn.ru (fullpornxxx / OK.ru)."""
    headers: dict[str, str] = {}
    if site == "fpx":
        # okcdn.ru returns HTTP 400 (error 16) if Origin is present.
        from download_fpx import USER_AGENT as FPX_UA
        headers["User-Agent"] = FPX_UA
        if referer:
            headers["Referer"] = referer
        headers["Accept"] = "*/*"
    elif site == "1porn":
        from download_1porn import USER_AGENT as PORN_UA
        headers["User-Agent"] = PORN_UA
        if referer:
            headers["Referer"] = referer
        headers["Accept"] = "*/*"
    elif hasattr(session, "media_headers"):
        headers = dict(session.media_headers(referer=referer or None))
    else:
        headers = {"Accept": "*/*"}
        if referer:
            headers["Referer"] = referer
    if range_header and range_header.lower().startswith("bytes="):
        headers["Range"] = range_header
    return headers


def _iter_urllib_mp4(session, target: str, headers: dict[str, str], timeout: int = 180):
    """Open an MP4 via the job's urllib opener, follow redirects, validate hosts."""
    import urllib.request
    import urllib.error

    parsed = urlparse(target)
    if parsed.scheme not in ("http", "https") or not _proxy_host_allowed(parsed.hostname or ""):
        raise ValueError(f"目标主机名不允许: {parsed.hostname}")
    req = urllib.request.Request(target, headers=headers)
    opener = getattr(session, "opener", None)
    if opener is None:
        opener = urllib.request.build_opener()
    resp = opener.open(req, timeout=timeout)
    final = resp.geturl()
    final_host = urlparse(final).hostname or ""
    if not _proxy_host_allowed(final_host):
        try:
            resp.close()
        except Exception:
            pass
        raise ValueError(f"Unsafe redirect to: {final_host}")
    return resp


def _iter_requests_mp4(session, target: str, headers: dict[str, str], timeout: int = 180):
    """Open an MP4 via requests, following redirects with the same SSRF checks. stream=True."""
    import requests
    from urllib.parse import urljoin as _urljoin

    url = target
    last = None
    for _ in range(8):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not _proxy_host_allowed(parsed.hostname or ""):
            if last is not None:
                last.close()
            raise ValueError(f"目标主机名不允许: {parsed.hostname}")
        http = getattr(session, "http", None)
        cookies = None
        if http is None:
            cookies = getattr(session, "cookies", None) or {}
            last = requests.get(
                url,
                headers=headers,
                cookies=cookies,
                timeout=timeout,
                allow_redirects=False,
                verify=True,
                stream=True,
            )
        else:
            last = http.get(
                url,
                headers=headers,
                timeout=timeout,
                allow_redirects=False,
                verify=True,
                stream=True,
            )
        if last.status_code in (301, 302, 303, 307, 308):
            loc = last.headers.get("Location") or ""
            last.close()
            if not loc:
                raise ValueError("重定向缺少 Location")
            url = _urljoin(url, loc)
            # Range / Referer stay; never add Origin on subsequent hops (okcdn.ru).
            continue
        return last
    raise ValueError("重定向次数过多")


def _stream_mp4_proxy(handler: "Handler", job: dict, target: str, range_header: str | None) -> None:
    """Stream a progressive MP4 through /proxy with Range support and session cookies."""
    session = job["session"]
    referer: str = job.get("referer") or ""
    site = job.get("site") or ""
    headers = _mp4_request_headers(session, site, target, referer, range_header)
    upstream = None
    try:
        if site in {"1porn", "fpx"}:
            upstream = _iter_requests_mp4(session, target, headers)
            status = upstream.status_code
            up_headers = upstream.headers
            def _chunks():
                for chunk in upstream.iter_content(64 * 1024):
                    if chunk:
                        yield chunk
            closer = upstream.close
        else:
            upstream = _iter_urllib_mp4(session, target, headers)
            status = getattr(upstream, "status", None) or upstream.getcode()
            up_headers = upstream.headers
            def _chunks():
                while True:
                    chunk = upstream.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
            closer = upstream.close

        if status not in (200, 206):
            # Drain a short error body for the message, do not log tokens.
            try:
                if site in {"1porn", "fpx"}:
                    preview = (upstream.content or b"")[:120]
                else:
                    preview = upstream.read(120)
            except Exception:
                preview = b""
            closer()
            raise ValueError(f"上游 HTTP {status}")

        ctype = up_headers.get("Content-Type") or "video/mp4"
        if "html" in ctype.lower() or "json" in ctype.lower():
            closer()
            raise ValueError(f"上游返回非视频类型: {ctype}")
        clen = up_headers.get("Content-Length")
        crange = up_headers.get("Content-Range")
        accept_ranges = up_headers.get("Accept-Ranges") or "bytes"

        handler.send_response(status)
        handler.send_header("Content-Type", ctype if ctype else "video/mp4")
        if clen:
            handler.send_header("Content-Length", clen)
        if crange:
            handler.send_header("Content-Range", crange)
        handler.send_header("Accept-Ranges", accept_ranges)
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header(
            "Access-Control-Expose-Headers",
            "Content-Length, Content-Range, Content-Type, Accept-Ranges",
        )
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        for chunk in _chunks():
            handler.wfile.write(chunk)
    finally:
        if upstream is not None:
            try:
                upstream.close()
            except Exception:
                pass


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        try:
            msg = fmt % args
        except Exception:
            msg = fmt
        sys.stderr.write("[http] " + _redact_url(msg) + "\n")

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
            parsed_target = urlparse(target)
            target_host = parsed_target.hostname or ""
            if parsed_target.scheme not in ("http", "https") or not _proxy_host_allowed(target_host):
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

        # 渐进式 MP4 站点：透传 Range，流式转发，保留解析会话 Cookie / Referer
        if site in _MP4_SITES:
            range_header = self.headers.get("Range")
            try:
                _stream_mp4_proxy(self, job, target, range_header)
            except Exception as e:
                sys.stderr.write(f"[proxy] {site} fail {_redact_url(target)[:180]}: {e}\n")
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
            sys.stderr.write(f"[proxy] fail {_redact_url(target)[:180]}: {e}\n")
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
