#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
whoreshub.com 影片页取链（KVS kt_player + 原生 MP4 渐进式下载，非 HLS）。

从页面提取 flashvars progressive 配置，选择最高实际分辨率的 MP4 源。
该站 get_file URL 需 v-acctoken 临时签名（短期过期），302 重定向到 cdntrex.com CDN。
需要页面会话 Cookie（PHPSESSID / kt_acctoken）及 Whoreshub Referer；代理配置同 download_ph_1080。

实际分辨率：源标签通常准确（样本真 4K 3840x2160），但仍需用有限头部探测真实尺寸。
非 m3u8/HLS/DASH；勿使用 PNG-TS 解包或 mux-mp4。

用法:
  python3 download_wh.py "https://www.whoreshub.com/videos/NNNN/title/"
"""

from __future__ import annotations

import hashlib
import html as htmlmod
import http.cookiejar
import json
import os
import re
import struct
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

from download_ph_1080 import (
    SEC_CH_UA,
    UA,
    _make_cookie,
    env_proxies,
    maybe_decompress,
    sanitize_proxy_env,
)

# 该站首页 Cookie（kt_acctoken 由页面 Set-Cookie 动态设置）
WH_COOKIE_PAIRS = [
    # kt_acctoken 由页面返回，不硬编码
]


def _is_safe_hostname(hostname: str) -> bool:
    """验证主机名安全性：拒绝私有/回环/链路本地地址。"""
    if not hostname:
        return False
    
    hostname = hostname.lower()
    
    # 拒绝回环
    if hostname in ("localhost", "localhost.localdomain"):
        return False
    
    # 检查 IP 地址
    try:
        import ipaddress
        addr = ipaddress.ip_address(hostname)
        # 拒绝私有、回环、链路本地、多播
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast:
            return False
    except ValueError:
        # 非 IP，检查域名
        pass
    
    # 仅允许 whoreshub.com 和已知 CDN
    allowed_domains = (
        "whoreshub.com",
        "www.whoreshub.com",
        "cdntrex.com",  # origin{N}-direct.cdntrex.com
        "wh.cdntrex.com",  # 截图 CDN
    )
    
    # 完全匹配或子域匹配
    if hostname in allowed_domains:
        return True
    
    for domain in allowed_domains:
        if hostname.endswith("." + domain):
            return True
    
    return False


def is_wh_page(url: str) -> bool:
    """whoreshub.com 视频页检测（/videos/数字ID/）+ 主机名验证。"""
    if not url:
        return False
    try:
        p = urlparse(url.strip())
    except Exception:
        return False
    
    # 必须是 https
    if p.scheme not in ("https", "http"):
        return False
    
    host = (p.hostname or "").lower()
    if not host:
        return False
    
    # 严格主机名检查
    if host not in ("whoreshub.com", "www.whoreshub.com"):
        # 允许子域
        if not host.endswith(".whoreshub.com"):
            return False
    
    path = (p.path or "").lower()
    return "/videos/" in path


def site_origin_wh(url: str) -> str:
    p = urlparse(url)
    scheme = p.scheme or "https"
    netloc = p.netloc or (p.hostname or "")
    if not netloc:
        return "https://www.freepornvideos.xxx"
    return f"{scheme}://{netloc}"


def _clean_title(raw: str) -> str:
    text = htmlmod.unescape(raw or "").strip()
    text = re.sub(r"\s+", " ", text)
    for sep in (" | ", " - ", " — ", " – "):
        lower = text.lower()
        for tail in ("whoreshub", "whores hub", "whoreshub.com"):
            needle = (sep + tail).lower()
            if lower.endswith(needle):
                text = text[: -len(sep + tail)].strip()
                lower = text.lower()
    return text.strip()


def extract_video_id(url: str) -> str | None:
    """从 URL 路径提取数字 ID：/videos/93810393/…"""
    m = re.search(r"/videos/(\d+)", url)
    return m.group(1) if m else None


def extract_title(html: str) -> str | None:
    """优先 og:title / twitter:title / <title>。"""
    if not html:
        return None

    def meta_content(prop: str) -> str | None:
        m = re.search(
            rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']',
            html,
            re.IGNORECASE,
        )
        if m:
            return m.group(1)
        m = re.search(
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{re.escape(prop)}["\']',
            html,
            re.IGNORECASE,
        )
        return m.group(1) if m else None

    for key in ("og:title", "twitter:title"):
        val = meta_content(key)
        if val:
            cleaned = _clean_title(val)
            if cleaned:
                return cleaned

    m = re.search(r"<title[^>]*>([^<]+)", html, re.IGNORECASE)
    if m:
        cleaned = _clean_title(m.group(1))
        if cleaned:
            return cleaned
    return None


def extract_flashvars_from_html(html: str) -> dict[str, Any] | None:
    """从 HTML 中提取 KVS kt_player flashvars 配置（JavaScript 对象）。

    返回 flashvars dict 或 None。
    """
    # 查找 var flashvars = {...}; 使用更可靠的匹配方式
    # 由于对象内可能有分号，需要匹配括号对
    start_pat = r'var\s+flashvars\s*=\s*\{'
    m = re.search(start_pat, html, re.S | re.I)
    if not m:
        return None
    
    start = m.end() - 1  # 包含开始的 {
    bracket_count = 0
    end = start
    for i in range(start, len(html)):
        if html[i] == '{':
            bracket_count += 1
        elif html[i] == '}':
            bracket_count -= 1
            if bracket_count == 0:
                end = i + 1
                break
    
    if end <= start:
        return None
    
    blob = html[start:end]
    
    # 尝试解析为 JSON
    try:
        # 先尝试标准 JSON
        return json.loads(blob)
    except Exception:
        pass
    
    # Fallback: 转换 JavaScript 对象为 JSON
    fixed = blob
    # 1. 为裸键添加引号（video_id: → "video_id":）- 在替换单引号之前
    fixed = re.sub(r'([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:', r'\1"\2":', fixed)
    # 2. 替换值的单引号为双引号
    fixed = fixed.replace("'", '"')
    # 3. 去掉尾逗号
    fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
    # 4. 不移除注释 - URL 中的 // 会被误删
    
    try:
        return json.loads(fixed)
    except Exception:
        return None


def extract_sources_from_flashvars(flashvars: dict[str, Any]) -> list[dict[str, Any]]:
    """从 flashvars 提取 MP4 源（video_url, video_alt_url, video_alt_url2, video_alt_url3）。

    返回 [{ file, label, is_4k, is_hd }, ...]，按分辨率升序排序。
    过滤 embed 重定向 URL（*_redirect="1"）。
    """
    sources: list[dict[str, Any]] = []
    
    # 映射: (url_key, text_key, hd_key, 4k_key)
    source_keys = [
        ("video_url", "video_url_text", None, None),  # 480p
        ("video_alt_url", "video_alt_url_text", "video_alt_url_hd", None),  # 720p
        ("video_alt_url2", "video_alt_url2_text", "video_alt_url2_hd", None),  # 1080p
        ("video_alt_url3", "video_alt_url3_text", "video_alt_url3_hd", "video_alt_url3_4k"),  # 2160p
    ]
    
    for url_key, text_key, hd_key, fk_key in source_keys:
        url = flashvars.get(url_key, "")
        if not url:
            continue
        
        # 过滤 embed 重定向 URL
        redirect_key = url_key + "_redirect"
        if str(flashvars.get(redirect_key, "")).strip() == "1":
            continue
        
        label = flashvars.get(text_key, "")
        is_hd = str(flashvars.get(hd_key, "")).strip() == "1" if hd_key else False
        is_4k = str(flashvars.get(fk_key, "")).strip() == "1" if fk_key else False
        
        sources.append({
            "file": url,
            "label": label,
            "is_hd": is_hd,
            "is_4k": is_4k,
        })
    
    # 按分辨率升序排序
    sources.sort(key=lambda s: label_height(s.get("label", "")))
    
    return sources


def label_height(label: Any) -> int:
    """从标签解析分辨率数值（2160p → 2160）。"""
    m = re.search(r"(\d+)", str(label or ""))
    return int(m.group(1)) if m else 0


def probe_mp4_dimensions(data: bytes, max_scan: int = 4 * 1024 * 1024) -> tuple[int, int] | None:
    """探测 MP4 前若干字节中的真实 width x height（从 moov/trak/tkhd）。

    MP4 box 结构：4B size + 4B type + payload。
    tkhd (track header): version(1) + flags(3) + ... + matrix(36) + width(4) + height(4).
    
    max_scan 默认 4MB（支持大 moov box；用户样本需约 3.2MB）。
    仅扫描视频轨道（handler_type='vide'），忽略音频/字幕轨。
    """
    if len(data) < 32:
        return None

    limit = min(len(data), max_scan)

    def read_u32(offset: int) -> int:
        if offset + 4 > len(data):
            return 0
        return struct.unpack(">I", data[offset : offset + 4])[0]
    
    def read_u64(offset: int) -> int:
        if offset + 8 > len(data):
            return 0
        return struct.unpack(">Q", data[offset : offset + 8])[0]

    def scan_boxes(start: int, end: int, in_video_track: bool = False) -> tuple[int, int] | None:
        """递归扫描 box 树，优先视频轨道。"""
        pos = start
        is_video_track = in_video_track
        
        while pos + 8 <= end:
            size = read_u32(pos)
            if size == 1:
                # 64位大小
                if pos + 16 > len(data):
                    break
                size = read_u64(pos + 8)
                payload_start = pos + 16
            elif size == 0:
                # 延伸到文件末尾
                break
            elif size < 8:
                # 非法大小
                break
            else:
                payload_start = pos + 8
            
            typ = data[pos + 4 : pos + 8]
            box_end = pos + size
            if box_end > end or box_end > len(data):
                # box 超出可用数据，可能部分 moov
                # 尝试扫描已有部分
                box_end = min(end, len(data))

            # 检查 hdlr (handler) box 判断是否视频轨
            if typ == b"hdlr" and payload_start + 12 <= len(data):
                # hdlr: version(1) + flags(3) + pre_defined(4) + handler_type(4)
                handler_offset = payload_start + 8
                if handler_offset + 4 <= len(data):
                    handler_type = data[handler_offset : handler_offset + 4]
                    is_video_track = (handler_type == b"vide")

            # 找到 tkhd box（仅在视频轨道内）
            if typ == b"tkhd" and payload_start < len(data):
                # 检查 version
                if payload_start >= len(data):
                    pos = box_end
                    continue
                    
                version = data[payload_start]
                
                if version == 0:
                    # version 0: 32位时间
                    # version(1) + flags(3) + created(4) + modified(4) + track_id(4) + 
                    # reserved(4) + duration(4) + reserved(8) + layer(2) + alternate(2) + 
                    # volume(2) + reserved(2) + matrix(36) + width(4) + height(4)
                    offset_w = payload_start + 76
                elif version == 1:
                    # version 1: 64位时间
                    # version(1) + flags(3) + created(8) + modified(8) + track_id(4) + 
                    # reserved(4) + duration(8) + reserved(8) + layer(2) + alternate(2) + 
                    # volume(2) + reserved(2) + matrix(36) + width(4) + height(4)
                    offset_w = payload_start + 88
                else:
                    pos = box_end
                    continue
                
                if offset_w + 8 <= len(data):
                    w_fp = read_u32(offset_w)
                    h_fp = read_u32(offset_w + 4)
                    w = w_fp >> 16
                    h = h_fp >> 16
                    if w > 0 and h > 0 and w <= 8192 and h <= 8192:
                        # 仅当在视频轨道或未知轨道时返回（优先视频）
                        # 若已知非视频轨道（音频），继续查找
                        if is_video_track or in_video_track:
                            return w, h

            # 递归扫描容器 box
            if typ in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
                result = scan_boxes(payload_start, box_end, is_video_track)
                if result:
                    return result

            pos = box_end

        return None

    return scan_boxes(0, limit)


def pick_best_wh_source(
    sources: list[dict[str, Any]],
    session: "WhSession",
    page_url: str,
) -> tuple[str, int, str, int, int]:
    """选择最高实际分辨率源。返回 (url, label_height, label, actual_width, actual_height)。

    策略：
    1) 过滤出 MP4 候选（.mp4 / type=video/mp4）
    2) 按标签分辨率降序排列
    3) 对每个候选，Range 请求头部（0-4MB）探测实际尺寸
    4) 返回实际分辨率最高的源
    5) 若无法探测，标注 (0, 0) 并按标签排序（LABEL_FALLBACK，非实际尺寸）
    """
    candidates: list[tuple[int, str, str]] = []
    for s in sources:
        if not isinstance(s, dict):
            continue
        file_url = str(s.get("file") or s.get("src") or "").strip()
        if not file_url:
            continue
        label = str(s.get("label") or s.get("quality") or "")
        typ = str(s.get("type") or "").lower()
        # 仅保留 MP4
        if ".mp4" not in file_url.lower() and "video/mp4" not in typ:
            continue
        h = label_height(label)
        candidates.append((h, file_url, label or f"{h}p" if h else "unknown"))

    if not candidates:
        raise ValueError("未找到可用 MP4 源")

    # 按标签高度降序
    candidates.sort(key=lambda x: x[0], reverse=True)

    best: tuple[str, int, str, int, int] | None = None  # (url, label_h, label, w, h)
    probed_any = False
    
    for label_h, url, label in candidates:
        # 扩展为绝对 URL
        if not url.startswith("http"):
            url = urljoin(page_url, url)
        
        # 验证 URL 安全性
        parsed = urlparse(url)
        if not _is_safe_hostname(parsed.hostname or ""):
            print(f"[warn] fpv 源 label={label} 主机名不安全: {parsed.hostname}")
            continue
        
        # 探测实际尺寸（扩展到 4MB 以支持大 moov）
        try:
            head = session.get_range_bytes_wh(url, 0, 4 * 1024 * 1024 - 1, referer=page_url)
            dims = probe_mp4_dimensions(head)
            if dims:
                w, h = dims
                probed_any = True
                print(
                    f"[info] fpv 源 label={label} ({label_h}p) 实际 {w}x{h}"
                )
                if best is None or (h * w) > (best[3] * best[4]):
                    best = (url, label_h, label, w, h)
            else:
                print(f"[warn] fpv 源 label={label} 无法探测尺寸（moov 不完整或格式异常）")
                # 无法探测：标注 (0, 0) 表示 LABEL_FALLBACK
                if best is None:
                    best = (url, label_h, label, 0, 0)
        except Exception as e:
            print(f"[warn] fpv 源 label={label} Range 探测失败: {e}")
            if best is None:
                best = (url, label_h, label, 0, 0)

    if not best:
        raise ValueError("所有源均被拒绝（主机名不安全）")
    
    # 警告：若所有源都无法探测，仅按标签排序（不准确）
    if not probed_any and best[3] == 0 and best[4] == 0:
        print(f"[warn] fpv 所有源无法探测实际尺寸，按标签排序（可能不准确）")
    
    return best


class WhSession:
    """会话管理：Cookie jar + 页面/媒体请求头。"""

    def __init__(self, page_url: str):
        self.page_url = page_url
        self.origin = site_origin_wh(page_url)
        self.cj = http.cookiejar.CookieJar()
        self._lock = threading.RLock()
        proxies = env_proxies()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(proxies),
            urllib.request.HTTPCookieProcessor(self.cj),
        )
        self.opener.addheaders = [
            ("User-Agent", UA),
            ("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8"),
        ]
        self._seed_cookies()

    def _seed_cookies(self) -> None:
        host = urlparse(self.page_url).hostname or "www.whoreshub.com"
        # kt_acctoken 由页面 Set-Cookie 动态设置，不需要预设
        # Whoreshub 不需要预设 Cookie，页面响应会自动设置
        for name, val in WH_COOKIE_PAIRS:
            try:
                self.cj.set_cookie(_make_cookie(name, val, host, path="/", secure=True))
            except Exception:
                pass

    def page_headers(self) -> dict[str, str]:
        return {
            "User-Agent": UA,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Ch-Ua": SEC_CH_UA,
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Connection": "keep-alive",
        }

    def media_headers(self, *, referer: str | None = None) -> dict[str, str]:
        """媒体请求头（下载 MP4 分片时使用）。"""
        ref = referer or self.page_url
        return {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "identity",
            "Referer": ref,
            "Origin": self.origin,
            "Sec-Ch-Ua": SEC_CH_UA,
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site",
            "Connection": "keep-alive",
        }

    def get_bytes_wh(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: int = 60,
        validate_redirect: bool = True,
    ) -> tuple[str, bytes]:
        """获取字节内容，验证初始和重定向目标。"""
        # 验证初始 URL
        parsed = urlparse(url)
        if not _is_safe_hostname(parsed.hostname or ""):
            raise ValueError(f"Unsafe hostname: {parsed.hostname}")
        
        hdrs = dict(headers or self.media_headers())
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                # 验证最终 URL（重定向后）
                final_url = resp.geturl()
                if validate_redirect and final_url != url:
                    final_parsed = urlparse(final_url)
                    if not _is_safe_hostname(final_parsed.hostname or ""):
                        raise ValueError(f"Unsafe redirect to: {final_parsed.hostname}")
                
                data = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                data = maybe_decompress(data, enc)
                return final_url, data
        except urllib.error.HTTPError as e:
            try:
                _ = e.read()
            except Exception:
                pass
            raise

    def get_text(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: int = 60,
    ) -> tuple[str, str]:
        final, raw = self.get_bytes_wh(url, headers=headers, timeout=timeout)
        return final, raw.decode("utf-8", "replace")

    def get_range_bytes_wh(
        self,
        url: str,
        start: int,
        end: int,
        *,
        referer: str | None = None,
        timeout: int = 30,
    ) -> bytes:
        """Range 请求指定字节范围（用于探测或分块下载）+ 主机名验证。"""
        # 验证 URL 安全性
        parsed = urlparse(url)
        if not _is_safe_hostname(parsed.hostname or ""):
            raise ValueError(f"Unsafe hostname for range request: {parsed.hostname}")
        
        hdrs = self.media_headers(referer=referer)
        hdrs["Range"] = f"bytes={start}-{end}"
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                # 验证重定向
                final_url = resp.geturl()
                if final_url != url:
                    final_parsed = urlparse(final_url)
                    if not _is_safe_hostname(final_parsed.hostname or ""):
                        raise ValueError(f"Unsafe redirect in range request: {final_parsed.hostname}")
                
                data = resp.read()
                # Range 响应一般不压缩
                return data
        except urllib.error.HTTPError as e:
            if e.code == 416:
                # Range Not Satisfiable
                return b""
            try:
                _ = e.read()
            except Exception:
                pass
            raise


def fetch_page(
    session: WhSession, page_url: str, *, tries: int = 3
) -> tuple[str, str]:
    last: Exception | None = None
    for i in range(1, tries + 1):
        print(f"[info] freepornvideos 拉页 ({i}/{tries}): {page_url}")
        try:
            final, html = session.get_text(page_url, headers=session.page_headers())
            session.page_url = final
            session.origin = site_origin_wh(final)
            # 更新 PHPSESSID Cookie（如果页面 Set-Cookie 返回）
            return final, html
        except Exception as e:
            last = e
            print(f"[warn] freepornvideos 拉页失败: {e}")
    assert last is not None
    raise last


def resolve_wh(page_url: str) -> dict[str, Any]:
    """取链入口。成功时返回包含 session / mp4_url / title / quality 等的字典。"""
    page_url = (page_url or "").strip()
    notes: list[str] = []
    if not page_url.startswith("http"):
        return {"ok": False, "error": "URL 必须以 http/https 开头", "notes": notes}
    if not is_wh_page(page_url):
        return {
            "ok": False,
            "error": "不是 whoreshub.com 视频页（需含 /videos/数字/）",
            "notes": notes,
        }

    session = WhSession(page_url)
    try:
        final_page, html = fetch_page(session, page_url)
    except Exception as e:
        return {"ok": False, "error": f"打开视频页失败: {e}", "notes": notes}

    video_id = extract_video_id(final_page) or extract_video_id(page_url)
    title = extract_title(html)
    if not video_id:
        return {
            "ok": False,
            "error": "页面中未找到视频 ID（/videos/数字/）",
            "title": title,
            "notes": notes,
        }
    notes.append(f"video_id={video_id}")

    # 提取 flashvars
    flashvars = extract_flashvars_from_html(html)
    if not flashvars:
        return {
            "ok": False,
            "error": "页面中未找到 flashvars 配置（KVS kt_player）",
            "title": title,
            "video_id": video_id,
            "notes": notes,
        }
    
    sources = extract_sources_from_flashvars(flashvars)
    if not sources:
        return {
            "ok": False,
            "error": "flashvars 中未找到可用 MP4 源（video_url/video_alt_url*）",
            "title": title,
            "video_id": video_id,
            "notes": notes,
        }
    notes.append(f"找到 {len(sources)} 个候选源")

    try:
        mp4_url, label_h, label, w, h = pick_best_wh_source(sources, session, final_page)
    except Exception as e:
        return {
            "ok": False,
            "error": f"选择最佳源失败: {e}",
            "title": title,
            "video_id": video_id,
            "notes": notes,
        }

    actual_res = f"{w}x{h}" if w and h else f"{label_h}p"
    notes.append(f"已选源: {label} (标签 {label_h}p, 实际 {actual_res})")

    return {
        "ok": True,
        "session": session,
        "page_url": final_page,
        "video_id": video_id,
        "title": title,
        "mp4_url": mp4_url,
        "quality": h or label_h,
        "quality_label": label,
        "actual_width": w,
        "actual_height": h,
        "notes": notes,
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print("用法: python3 download_wh.py <whoreshub.com 视频页 URL>")
        return 0

    sanitize_proxy_env()
    result = resolve_wh(args[0])
    show = {k: v for k, v in result.items() if k not in {"session"}}
    print(json.dumps(show, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
