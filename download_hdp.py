#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hdporn.gg 视频页取链（Video.js + Progressive MP4，KVS get_file 分发）。

基于公开调查证据（2026-11-25，样本 93690758）：
- 播放器：Video.js + silvermine quality selector
- 格式：Progressive MP4（非 HLS/DASH），无 PNG 包装
- 源配置：HTML <video> <source> 标签（src/type/label/selected）
- URL 模式：/get_file/{storage}/{42hex}/{bucket}/{id}/{filename}/
- CDN: 302 重定向到 d{N}.fpvcdn.com（带 key/end/ip 签名，约60分钟过期）
- Cookie: 需 PHPSESSID + kt_ips（含客户端IP，禁止log）
- Referer: 应发送影片页 URL
- 分辨率：样本 720p=1280x720 真实，480p 在证据网络 403 Wrong IP
- moov: 在文件头，约 2.1 MiB，支持 Range 206

页面说明提及4K但样本HTML只有720p/480p源，不虚构更高档URL。
embed页只有480p真实+720p重定向，需解析完整影片页获取最高质量。
官方Download按钮是同一720p文件（+download=true），不是登录绕过。

登录墙：站点有 /login-required/，但样本的Download按钮公开可用，不绕过。
"""

from __future__ import annotations

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

# 该站首页 Cookie（kt_ips 由页面 Set-Cookie 动态设置，含客户端IP）
HDP_COOKIE_PAIRS = [
    # kt_ips 由页面返回，不硬编码
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
    
    # 仅允许 hdporn.gg 和已知 CDN
    allowed_domains = (
        "hdporn.gg",
        "www.hdporn.gg",
        "fpvcdn.com",  # d{N}.fpvcdn.com
        "ahcdn.com",   # ip{N}.ahcdn.com（样本480p失败但仍是已知CDN）
    )
    
    # 完全匹配或子域匹配
    if hostname in allowed_domains:
        return True
    
    for domain in allowed_domains:
        if hostname.endswith("." + domain):
            return True
    
    return False


def is_hdp_page(url: str) -> bool:
    """hdporn.gg 视频页检测（/videos/{slug}/）+ 主机名验证。"""
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
    if host not in ("hdporn.gg", "www.hdporn.gg"):
        # 允许子域
        if not host.endswith(".hdporn.gg"):
            return False
    
    path = (p.path or "").lower()
    return "/videos/" in path


def site_origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _clean_title(raw: str) -> str:
    text = htmlmod.unescape(raw or "").strip()
    text = re.sub(r"\s+", " ", text)
    for sep in (" | ", " - ", " — ", " – "):
        lower = text.lower()
        for tail in ("hdporn", "hd porn", "hdporn.gg"):
            needle = (sep + tail).lower()
            if lower.endswith(needle):
                text = text[: -len(sep + tail)].strip()
                lower = text.lower()
    return text.strip()


def extract_video_id(url: str) -> str | None:
    """从 URL 路径或 embed 路径提取数字 ID：/videos/{slug}/ 或 /embed/{id}"""
    # 尝试从 embed 提取
    m = re.search(r"/embed/(\d+)", url)
    if m:
        return m.group(1)
    # 尝试从 get_file 提取
    m = re.search(r"/get_file/[^/]+/[^/]+/\d+/(\d+)/", url)
    if m:
        return m.group(1)
    return None


def extract_title(html: str) -> str | None:
    """优先 og:title / twitter:title，回退到 <title>。"""
    for pat in (
        r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
        r'<meta\s+name=["\']twitter:title["\']\s+content=["\']([^"\']+)["\']',
        r"<title[^>]*>([^<]+)</title>",
    ):
        m = re.search(pat, html, re.I)
        if m:
            return _clean_title(m.group(1))
    return None


def extract_sources_from_html(html: str) -> list[dict[str, Any]]:
    """从 HTML 中提取 Video.js <source> 标签（src/type/label/selected）。

    返回 [{ file, label, selected, ... }, ...]
    """
    sources: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    # 查找 <source> 标签：<source src="..." type="video/mp4" label="720p" selected="true">
    source_pattern = r'<source\s+([^>]+)>'
    
    for m in re.finditer(source_pattern, html, re.I):
        attrs = m.group(1)
        
        # 提取 src
        src_match = re.search(r'src=["\']([^"\']+)["\']', attrs, re.I)
        if not src_match:
            continue
        src = src_match.group(1).strip()
        
        if not src or src in seen_urls:
            continue
        
        # 提取 type
        type_match = re.search(r'type=["\']([^"\']+)["\']', attrs, re.I)
        media_type = type_match.group(1) if type_match else ""
        
        # 只处理 video/mp4
        if "video/mp4" not in media_type.lower():
            continue
        
        # 提取 label
        label_match = re.search(r'label=["\']([^"\']+)["\']', attrs, re.I)
        label = label_match.group(1) if label_match else ""
        
        # 提取 selected
        selected_match = re.search(r'selected=["\']?(true|True|TRUE)["\']?', attrs, re.I)
        is_selected = bool(selected_match)
        
        sources.append({
            "file": src,
            "label": label,
            "selected": is_selected,
            "type": media_type,
        })
        seen_urls.add(src)
    
    return sources


def label_height(label: Any) -> int:
    """从标签解析分辨率数值（720p → 720）。"""
    m = re.search(r"(\d+)", str(label or ""))
    return int(m.group(1)) if m else 0


def probe_mp4_dimensions(data: bytes, max_scan: int = 4 * 1024 * 1024) -> tuple[int, int] | None:
    """探测 MP4 前若干字节中的真实 width x height（从 moov/trak/tkhd）。
    
    样本 moov 约 2.1 MiB，使用 4MB 窗口。
    仅扫描视频轨道（handler_type='vide'），忽略音频/字幕轨。
    """
    if len(data) < 8:
        return None
    
    limit = min(len(data), max_scan)
    
    def read_box(offset: int) -> tuple[int, bytes, int] | None:
        if offset + 8 > limit:
            return None
        size_bytes = data[offset : offset + 4]
        box_type = data[offset + 4 : offset + 8]
        size = struct.unpack(">I", size_bytes)[0]
        if size == 0:
            size = limit - offset
        elif size == 1:
            if offset + 16 > limit:
                return None
            size = struct.unpack(">Q", data[offset + 8 : offset + 16])[0]
            return size, box_type, 16
        return size, box_type, 8
    
    def find_box(start: int, end: int, target: bytes) -> int | None:
        pos = start
        while pos < end:
            box_info = read_box(pos)
            if not box_info:
                break
            size, box_type, header = box_info
            if box_type == target:
                return pos
            pos += size
            if size < 8 or pos > end:
                break
        return None
    
    # 查找 moov
    moov_pos = find_box(0, limit, b"moov")
    if moov_pos is None:
        return None
    
    moov_info = read_box(moov_pos)
    if not moov_info:
        return None
    moov_size, _, moov_header = moov_info
    moov_start = moov_pos + moov_header
    moov_end = moov_pos + moov_size
    
    # 遍历 moov 中的 trak
    pos = moov_start
    while pos < moov_end:
        box_info = read_box(pos)
        if not box_info:
            break
        size, box_type, header = box_info
        
        if box_type == b"trak":
            trak_start = pos + header
            trak_end = pos + size
            
            # 检查是否为视频轨道（hdlr box）
            hdlr_pos = find_box(trak_start, trak_end, b"hdlr")
            if hdlr_pos is not None:
                hdlr_info = read_box(hdlr_pos)
                if hdlr_info and hdlr_pos + hdlr_info[2] + 8 <= limit:
                    handler_type = data[hdlr_pos + hdlr_info[2] + 8 : hdlr_pos + hdlr_info[2] + 12]
                    if handler_type != b"vide":
                        pos += size
                        continue
            
            # 在 trak 中查找 tkhd
            tkhd_pos = find_box(trak_start, trak_end, b"tkhd")
            if tkhd_pos is None:
                pos += size
                continue
            
            tkhd_info = read_box(tkhd_pos)
            if not tkhd_info:
                pos += size
                continue
            
            tkhd_header = tkhd_info[2]
            tkhd_data_start = tkhd_pos + tkhd_header
            
            if tkhd_data_start + 1 > limit:
                pos += size
                continue
            
            version = data[tkhd_data_start]
            
            # version 0: width/height 在 offset 76/80（32位时间戳）
            # version 1: width/height 在 offset 88/92（64位时间戳）
            if version == 0:
                width_offset = tkhd_data_start + 76
                height_offset = tkhd_data_start + 80
            else:  # version 1
                width_offset = tkhd_data_start + 88
                height_offset = tkhd_data_start + 92
            
            if height_offset + 4 > limit:
                pos += size
                continue
            
            # width/height 存储为 16.16 固定点数（32位，前16位是整数部分）
            width_fixed = struct.unpack(">I", data[width_offset : width_offset + 4])[0]
            height_fixed = struct.unpack(">I", data[height_offset : height_offset + 4])[0]
            
            width = width_fixed >> 16
            height = height_fixed >> 16
            
            if width > 0 and height > 0:
                return (width, height)
        
        pos += size
        if size < 8:
            break
    
    return None


class HdpSession:
    """会话管理：Cookie jar + 页面/媒体请求头。"""

    def __init__(self, page_url: str):
        self.page_url = page_url
        self.origin = site_origin(page_url)
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
        host = urlparse(self.page_url).hostname or "www.hdporn.gg"
        # kt_ips 由页面 Set-Cookie 动态设置（含客户端IP），不预设
        for name, val in HDP_COOKIE_PAIRS:
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
        """媒体请求头（下载 MP4 时使用）。"""
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

    def get_bytes(
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

        req = urllib.request.Request(url, headers=headers or {})
        with self._lock:
            try:
                resp = self.opener.open(req, timeout=timeout)
                final_url = resp.geturl()
                
                # 验证重定向目标
                if validate_redirect and final_url != url:
                    final_parsed = urlparse(final_url)
                    if not _is_safe_hostname(final_parsed.hostname or ""):
                        raise ValueError(f"Unsafe redirect target: {final_parsed.hostname}")
                
                data = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "")
                return final_url, maybe_decompress(data, enc)
            except urllib.error.HTTPError as e:
                if e.code == 403:
                    # CDN 可能返回 403 Wrong IP，记录但不暴露签名
                    body = e.read()
                    if b"Wrong IP" in body:
                        raise RuntimeError(f"CDN IP binding: source unavailable (403 Wrong IP)")
                    raise RuntimeError(f"HTTP 403 Forbidden: {url}")
                raise

    def get_range_bytes(
        self,
        url: str,
        start: int,
        end: int,
        *,
        referer: str | None = None,
        timeout: int = 60,
        validate_redirect: bool = True,
    ) -> tuple[str, bytes]:
        """Range 请求，验证初始和重定向目标。"""
        # 验证初始 URL
        parsed = urlparse(url)
        if not _is_safe_hostname(parsed.hostname or ""):
            raise ValueError(f"Unsafe hostname: {parsed.hostname}")

        headers = self.media_headers(referer=referer)
        headers["Range"] = f"bytes={start}-{end-1}"
        
        req = urllib.request.Request(url, headers=headers)
        with self._lock:
            try:
                resp = self.opener.open(req, timeout=timeout)
                final_url = resp.geturl()
                
                # 验证重定向目标
                if validate_redirect and final_url != url:
                    final_parsed = urlparse(final_url)
                    if not _is_safe_hostname(final_parsed.hostname or ""):
                        raise ValueError(f"Unsafe redirect target: {final_parsed.hostname}")
                
                data = resp.read()
                return final_url, data
            except urllib.error.HTTPError as e:
                if e.code == 403:
                    # CDN 可能返回 403 Wrong IP
                    body = e.read()
                    if b"Wrong IP" in body:
                        raise RuntimeError(f"CDN IP binding: source unavailable (403 Wrong IP)")
                    raise RuntimeError(f"HTTP 403 Forbidden: {url}")
                elif e.code == 416:
                    raise RuntimeError(f"Range not satisfiable: {url}")
                raise


def fetch_page(session: HdpSession, url: str, max_retries: int = 3) -> tuple[str, str]:
    """拉取页面，重试逻辑。"""
    for attempt in range(max_retries):
        try:
            final_url, data = session.get_bytes(url, headers=session.page_headers(), timeout=60)
            html = data.decode("utf-8", errors="ignore")
            return final_url, html
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            print(f"[warn] hdporn 拉页失败: {e}", file=sys.stderr)
    raise RuntimeError("Should not reach here")


LABEL_FALLBACK = "(标签)"


def pick_best_source(
    sources: list[dict],
    session: HdpSession,
    page_url: str,
    max_scan: int = 4 * 1024 * 1024,
) -> tuple[str, int, str, int, int]:
    """选择最高实际分辨率的源。
    
    返回: (url, label_height, label, actual_width, actual_height)
    actual_width/height 为 0 表示使用标签回退。
    """
    if not sources:
        raise ValueError("No sources to pick from")
    
    best_url = ""
    best_label = ""
    best_label_h = 0
    best_w = 0
    best_h = 0
    
    # 收集所有探测结果
    probed: list[tuple[str, str, int, int, int]] = []
    
    for src in sources:
        url = src.get("file", "")
        label = src.get("label", "")
        
        if not url:
            continue
        
        label_h = label_height(label)
        
        # 尝试探测实际分辨率
        try:
            _, chunk = session.get_range_bytes(
                url,
                0,
                max_scan,
                referer=page_url,
                timeout=30,
            )
            
            dims = probe_mp4_dimensions(chunk, max_scan)
            if dims:
                w, h = dims
                probed.append((url, label, label_h, w, h))
                print(f"[info] hdporn 探测: {label} → {w}x{h}", file=sys.stderr)
            else:
                # 探测失败，使用标签
                probed.append((url, label, label_h, 0, 0))
                print(f"[warn] hdporn 探测失败，使用标签: {label}", file=sys.stderr)
        
        except RuntimeError as e:
            # CDN IP binding 或其他错误
            if "403 Wrong IP" in str(e):
                print(f"[warn] hdporn {label} CDN IP binding (403 Wrong IP)，跳过", file=sys.stderr)
                continue
            else:
                # 其他错误，使用标签回退
                probed.append((url, label, label_h, 0, 0))
                print(f"[warn] hdporn 探测错误: {label} - {e}", file=sys.stderr)
        
        except Exception as e:
            # 其他异常，使用标签回退
            probed.append((url, label, label_h, 0, 0))
            print(f"[warn] hdporn 探测异常: {label} - {e}", file=sys.stderr)
    
    if not probed:
        raise ValueError("All sources failed or were skipped (IP binding)")
    
    # 选择最高分辨率
    # 优先使用实际探测的分辨率（w*h），回退到标签
    for url, label, label_h, w, h in probed:
        if w > 0 and h > 0:
            # 有实际分辨率
            if (w * h) > (best_w * best_h):
                best_url = url
                best_label = label
                best_label_h = label_h
                best_w = w
                best_h = h
        else:
            # 标签回退
            if best_w == 0 and best_h == 0:  # 只在没有实际分辨率时比较标签
                if label_h > best_label_h:
                    best_url = url
                    best_label = label
                    best_label_h = label_h
                    best_w = 0
                    best_h = 0
    
    if not best_url:
        raise ValueError("No valid source found")
    
    # 警告：如果所有源都未能探测
    if best_w == 0 and best_h == 0:
        print(f"[warn] hdporn 所有源无法探测实际尺寸，按标签排序（可能不准确）", file=sys.stderr)
    
    return best_url, best_label_h, best_label, best_w, best_h


def resolve_hdp(page_url: str) -> dict[str, Any]:
    """取链入口。成功时返回包含 session / mp4_url / title / quality 等的字典。"""
    page_url = (page_url or "").strip()
    notes: list[str] = []
    
    if not page_url.startswith("http"):
        return {"ok": False, "error": "URL 必须以 http/https 开头", "notes": notes}
    
    if not is_hdp_page(page_url):
        return {
            "ok": False,
            "error": "不是 hdporn.gg 视频页（需含 /videos/{slug}/）",
            "notes": notes,
        }

    session = HdpSession(page_url)
    try:
        final_page, html = fetch_page(session, page_url)
    except Exception as e:
        return {"ok": False, "error": f"打开视频页失败: {e}", "notes": notes}

    video_id = extract_video_id(final_page) or extract_video_id(page_url)
    title = extract_title(html)
    
    if video_id:
        notes.append(f"video_id={video_id}")

    sources = extract_sources_from_html(html)
    if not sources:
        return {
            "ok": False,
            "error": "页面中未找到 Video.js <source>（无 MP4 源）",
            "title": title,
            "video_id": video_id,
            "notes": notes,
        }
    notes.append(f"找到 {len(sources)} 个候选源")

    try:
        mp4_url, label_h, label, w, h = pick_best_source(sources, session, final_page)
    except Exception as e:
        return {
            "ok": False,
            "error": f"选择最佳源失败: {e}",
            "title": title,
            "video_id": video_id,
            "notes": notes,
        }

    actual_res = f"{w}x{h}" if w and h else f"{label_h}p"
    quality_label = f"{label} (实际 {actual_res})" if w and h else f"{label} {LABEL_FALLBACK}"
    notes.append(f"已选源: {label} (标签 {label_h}p, 实际 {actual_res})")

    return {
        "ok": True,
        "session": session,
        "page_url": final_page,
        "video_id": video_id,
        "title": title,
        "mp4_url": mp4_url,
        "quality": label_h,
        "quality_label": quality_label,
        "actual_width": w,
        "actual_height": h,
        "notes": notes,
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print("用法: python3 download_hdp.py <hdporn.gg 视频页 URL>")
        return 0

    sanitize_proxy_env()
    result = resolve_hdp(args[0])
    show = {k: v for k, v in result.items() if k not in {"session"}}
    print(json.dumps(show, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
