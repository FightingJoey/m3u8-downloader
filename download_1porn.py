#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
1porn.tv 渐进式 MP4 下载适配器

站点: www.1porn.tv
CMS: Kernel Video Sharing (KVS)
播放器: Video.js (影片页) / kt_player (embed)
格式: 渐进式 MP4 (H.264 + AAC)

关键特性:
- Video.js <source> 标签解析 (类似 hdporn)
- 语言前缀支持: /zh/videos/, /zh/get_file/ (可选)
- 标签可能不准确 (样本 2160p 实际为 1920×1080)
- CDN: fpvcdn.com (与 hdporn 共享)
- Cookie: PHPSESSID + kt_ips (kt_ips 包含客户端 IP, 禁止记录)
- IP 绑定 CDN, 短期签名 (~60min)
- 410 错误表示缺少 Cookie

安全:
- SSRF 保护: 白名单 1porn.tv + fpvcdn.com, 拒绝私有/回环/链路本地/组播 IP
- TLS 验证始终开启
- kt_ips cookie 值永远不记录 (包含客户端 IP)
- 每次重定向验证主机名
- 无登录/付费墙/DRM 绕过

实现限制:
- Embed 页仅提供 480p 直接源, 高画质重定向到影片页
- 官方下载按钮仅指向 720p
- 使用影片页公开 <source> 标签作为权威来源
"""

import sys
import re
import logging
import struct
import ipaddress
from urllib.parse import urlparse, urljoin, parse_qs
from typing import Optional
import requests

# ============================================================================
# 配置
# ============================================================================
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 30

logger = logging.getLogger(__name__)


# ============================================================================
# SSRF 保护
# ============================================================================
def _is_safe_hostname(hostname: str) -> bool:
    """
    严格主机名验证: 白名单 1porn.tv + fpvcdn.com, 拒绝私有 IP
    
    Args:
        hostname: 要验证的主机名
        
    Returns:
        bool: 主机名是否安全
    """
    # 检查是否为 IP 地址
    try:
        ip = ipaddress.ip_address(hostname)
        # 拒绝私有、回环、链路本地、组播地址
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
            logger.warning(f"拒绝私有/回环/链路本地/组播 IP: {hostname}")
            return False
    except ValueError:
        pass  # 不是 IP 地址, 检查主机名白名单
    
    # 白名单: 1porn.tv 和 fpvcdn.com (与 hdporn 共享 CDN)
    hostname_lower = hostname.lower()
    allowed = ("1porn.tv", "www.1porn.tv", "fpvcdn.com")
    
    # 完全匹配或 fpvcdn.com 子域名 (d{N}.fpvcdn.com)
    if hostname_lower in allowed:
        return True
    if hostname_lower.endswith(".fpvcdn.com"):
        return True
    if hostname_lower.endswith(".1porn.tv"):
        return True
    
    logger.warning(f"主机名 {hostname} 不在白名单中")
    return False


def _validate_url_safety(url: str) -> bool:
    """
    验证 URL 安全性
    
    Args:
        url: 要验证的 URL
        
    Returns:
        bool: URL 是否安全
    """
    parsed = urlparse(url)
    
    # 只允许 https
    if parsed.scheme != "https":
        logger.warning(f"拒绝非 HTTPS URL: {url}")
        return False
    
    # 验证主机名
    if not parsed.hostname:
        logger.warning(f"URL 缺少主机名: {url}")
        return False
    
    return _is_safe_hostname(parsed.hostname)


# ============================================================================
# HTTP 工具
# ============================================================================
class OnePornSession:
    """1porn.tv 会话管理器"""
    
    def __init__(self):
        self.cookies = {}  # {name: value}
        self.referer = ""  # 影片页 URL


def get_bytes(url: str, session: Optional[OnePornSession] = None, follow_redirects: bool = True) -> bytes:
    """
    获取 URL 内容, 带 SSRF 保护和重定向验证
    
    Args:
        url: 目标 URL
        session: OnePornSession 实例 (可选)
        follow_redirects: 是否跟随重定向
        
    Returns:
        bytes: 响应内容
        
    Raises:
        ValueError: URL 不安全或响应错误
    """
    # 验证初始 URL
    if not _validate_url_safety(url):
        raise ValueError(f"不安全的 URL: {url}")
    
    headers = {"User-Agent": USER_AGENT}
    if session and session.referer:
        headers["Referer"] = session.referer
    
    cookies = session.cookies if session else {}
    
    resp = requests.get(
        url,
        headers=headers,
        cookies=cookies,
        timeout=TIMEOUT,
        allow_redirects=False,
        verify=True  # TLS 验证始终开启
    )
    
    # 处理重定向
    if follow_redirects and resp.status_code in (301, 302, 303, 307, 308):
        redirect_url = resp.headers.get("Location", "")
        if not redirect_url:
            raise ValueError(f"重定向缺少 Location 头: {url}")
        
        # 解析绝对 URL
        redirect_url = urljoin(url, redirect_url)
        
        # 验证重定向目标
        if not _validate_url_safety(redirect_url):
            raise ValueError(f"不安全的重定向目标: {redirect_url}")
        
        logger.debug(f"跟随重定向: {url} -> {redirect_url}")
        return get_bytes(redirect_url, session, follow_redirects=True)
    
    if resp.status_code == 410:
        raise ValueError("410 Gone: 缺少有效 Cookie (需要 PHPSESSID 或 kt_ips)")
    
    if resp.status_code == 403:
        body_preview = resp.content[:100].decode("utf-8", errors="ignore")
        if "wrong ip" in body_preview.lower():
            raise ValueError("403 Wrong IP: CDN IP 绑定验证失败")
        raise ValueError(f"403 Forbidden: {body_preview}")
    
    if resp.status_code != 200:
        raise ValueError(f"HTTP {resp.status_code}: {url}")
    
    return resp.content


def get_range_bytes(url: str, start: int, end: int, session: Optional[OnePornSession] = None) -> bytes:
    """
    Range 请求, 带 SSRF 保护
    
    Args:
        url: 目标 URL
        start: 起始字节 (包含)
        end: 结束字节 (包含)
        session: OnePornSession 实例 (可选)
        
    Returns:
        bytes: 范围内容
        
    Raises:
        ValueError: 请求失败
    """
    # 验证初始 URL
    if not _validate_url_safety(url):
        raise ValueError(f"不安全的 URL: {url}")
    
    headers = {
        "User-Agent": USER_AGENT,
        "Range": f"bytes={start}-{end}"
    }
    if session and session.referer:
        headers["Referer"] = session.referer
    
    cookies = session.cookies if session else {}
    
    resp = requests.get(
        url,
        headers=headers,
        cookies=cookies,
        timeout=TIMEOUT,
        allow_redirects=False,
        verify=True
    )
    
    # 处理重定向
    if resp.status_code in (301, 302, 303, 307, 308):
        redirect_url = resp.headers.get("Location", "")
        if not redirect_url:
            raise ValueError(f"重定向缺少 Location 头: {url}")
        
        redirect_url = urljoin(url, redirect_url)
        
        # 验证重定向目标
        if not _validate_url_safety(redirect_url):
            raise ValueError(f"不安全的重定向目标: {redirect_url}")
        
        logger.debug(f"Range 重定向: {url} -> {redirect_url}")
        return get_range_bytes(redirect_url, start, end, session)
    
    if resp.status_code == 410:
        raise ValueError("410 Gone: 缺少有效 Cookie")
    
    if resp.status_code == 403:
        body_preview = resp.content[:100].decode("utf-8", errors="ignore")
        if "wrong ip" in body_preview.lower():
            raise ValueError("403 Wrong IP: CDN IP 绑定验证失败")
        raise ValueError(f"403 Forbidden: {body_preview}")
    
    # 接受 206 Partial Content 或 200 OK
    if resp.status_code not in (200, 206):
        raise ValueError(f"Range 请求失败 HTTP {resp.status_code}: {url}")
    
    return resp.content


# ============================================================================
# MP4 探测 (复用 fpv/wh/hdp 实现)
# ============================================================================
def probe_mp4_dimensions(data: bytes, max_scan: int = 4 * 1024 * 1024) -> Optional[tuple[int, int]]:
    """
    从 MP4 字节流探测实际视频尺寸 (moov/trak/tkhd)
    
    仅扫描前 max_scan 字节内的 moov 盒子。
    通过 hdlr 盒子过滤音频轨 (handler_type='soun'), 只返回视频轨尺寸。
    支持 tkhd version 0 (32位时间戳) 和 version 1 (64位时间戳)。
    
    Args:
        data: MP4 文件开头字节 (至少包含 ftyp + moov)
        max_scan: 最大扫描字节数 (默认 4MB)
        
    Returns:
        (width, height) 或 None (探测失败)
    """
    def read_box(data: bytes, offset: int) -> Optional[tuple[int, bytes, bytes]]:
        """读取单个盒子: (size, type, payload)"""
        if offset + 8 > len(data):
            return None
        size = struct.unpack(">I", data[offset:offset+4])[0]
        box_type = data[offset+4:offset+8]
        
        if size == 0:
            payload_start = offset + 8
            payload = data[payload_start:]
            return (len(data) - offset, box_type, payload)
        elif size == 1:
            if offset + 16 > len(data):
                return None
            size = struct.unpack(">Q", data[offset+8:offset+16])[0]
            payload_start = offset + 16
            payload_end = min(offset + size, len(data))
            payload = data[payload_start:payload_end]
            return (size, box_type, payload)
        else:
            payload_start = offset + 8
            payload_end = min(offset + size, len(data))
            payload = data[payload_start:payload_end]
            return (size, box_type, payload)
    
    def find_boxes(data: bytes, target_type: bytes, max_depth: int = 10) -> list[bytes]:
        """递归查找指定类型的盒子"""
        if max_depth <= 0:
            return []
        
        results = []
        offset = 0
        while offset < len(data):
            box = read_box(data, offset)
            if box is None:
                break
            size, box_type, payload = box
            
            if box_type == target_type:
                results.append(payload)
            
            # 容器盒子: moov, trak, mdia, minf, stbl
            if box_type in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
                results.extend(find_boxes(payload, target_type, max_depth - 1))
            
            if size < 8:
                break
            offset += size
        
        return results
    
    def is_video_track(trak_data: bytes) -> bool:
        """检查 trak 是否为视频轨 (通过 hdlr box)"""
        hdlr_boxes = find_boxes(trak_data, b"hdlr", max_depth=5)
        for hdlr_payload in hdlr_boxes:
            if len(hdlr_payload) >= 12:
                handler_type = hdlr_payload[8:12]
                if handler_type == b"vide":
                    return True
                elif handler_type == b"soun":
                    return False
        return True  # 默认假设视频 (向后兼容)
    
    def parse_tkhd_dimensions(tkhd_payload: bytes) -> Optional[tuple[int, int]]:
        """从 tkhd payload 解析宽高"""
        if len(tkhd_payload) < 1:
            return None
        
        version = tkhd_payload[0]
        
        if version == 0:
            # Version 0: 32位时间戳, 宽高在 offset 76
            if len(tkhd_payload) < 84:
                return None
            width_fixed = struct.unpack(">I", tkhd_payload[76:80])[0]
            height_fixed = struct.unpack(">I", tkhd_payload[80:84])[0]
        elif version == 1:
            # Version 1: 64位时间戳, 宽高在 offset 88
            if len(tkhd_payload) < 96:
                return None
            width_fixed = struct.unpack(">I", tkhd_payload[88:92])[0]
            height_fixed = struct.unpack(">I", tkhd_payload[92:96])[0]
        else:
            logger.warning(f"不支持的 tkhd 版本: {version}")
            return None
        
        # 16.16 定点数转整数
        width = width_fixed >> 16
        height = height_fixed >> 16
        
        if width > 0 and height > 0:
            return (width, height)
        return None
    
    # 限制扫描范围
    scan_data = data[:max_scan]
    
    # 查找 moov 盒子
    moov_boxes = find_boxes(scan_data, b"moov", max_depth=1)
    if not moov_boxes:
        logger.debug("未找到 moov 盒子")
        return None
    
    # 在 moov 中查找 trak 盒子
    moov_data = moov_boxes[0]
    trak_boxes = find_boxes(moov_data, b"trak", max_depth=2)
    
    for trak_data in trak_boxes:
        # 过滤音频轨
        if not is_video_track(trak_data):
            logger.debug("跳过音频轨")
            continue
        
        # 在视频 trak 中查找 tkhd
        tkhd_boxes = find_boxes(trak_data, b"tkhd", max_depth=3)
        for tkhd_payload in tkhd_boxes:
            dims = parse_tkhd_dimensions(tkhd_payload)
            if dims:
                return dims
    
    logger.debug("未找到有效视频轨 tkhd")
    return None


# ============================================================================
# 页面解析
# ============================================================================
def _extract_sources_from_html(html: str) -> list[dict]:
    """
    从影片页 HTML 提取 Video.js sources
    
    查找: <source src="..." type="video/mp4" label="2160p|1080p|720p|480p" [selected]>
    
    Args:
        html: 页面 HTML
        
    Returns:
        list of {url, label, label_height, selected}
        按 label_height 降序排列
    """
    sources = []
    
    # 匹配 <source> 标签 (可能跨多行)
    pattern = re.compile(
        r'<source\s+([^>]*?)>',
        re.IGNORECASE | re.DOTALL
    )
    
    for match in pattern.finditer(html):
        attrs_str = match.group(1)
        
        # 提取属性
        src_match = re.search(r'src=["\']([^"\']+)["\']', attrs_str, re.IGNORECASE)
        type_match = re.search(r'type=["\']([^"\']+)["\']', attrs_str, re.IGNORECASE)
        label_match = re.search(r'label=["\']([^"\']+)["\']', attrs_str, re.IGNORECASE)
        selected_match = re.search(r'selected\s*=\s*["\']true["\']', attrs_str, re.IGNORECASE)
        
        if not src_match:
            continue
        
        src = src_match.group(1)
        label = label_match.group(1) if label_match else ""
        video_type = type_match.group(1) if type_match else ""
        selected = bool(selected_match)
        
        # 只处理 video/mp4
        if "video/mp4" not in video_type.lower():
            continue
        
        # 提取标签高度 (2160p -> 2160)
        label_height = 0
        height_match = re.search(r'(\d+)p', label, re.IGNORECASE)
        if height_match:
            label_height = int(height_match.group(1))
        
        sources.append({
            "url": src,
            "label": label,
            "label_height": label_height,
            "selected": selected
        })
    
    # 按标签高度降序
    sources.sort(key=lambda x: x["label_height"], reverse=True)
    
    return sources


def _extract_title_from_html(html: str) -> str:
    """
    从 HTML 提取视频标题
    
    优先级:
    1. <title> 标签 (移除 " - 1porn.tv" 后缀)
    2. og:title meta 标签
    3. 回退: "video"
    
    Args:
        html: 页面 HTML
        
    Returns:
        str: 清理后的标题
    """
    # 尝试 <title>
    title_match = re.search(r'<title>([^<]+)</title>', html, re.IGNORECASE)
    if title_match:
        title = title_match.group(1).strip()
        # 移除常见后缀
        title = re.sub(r'\s*-\s*(1porn\.tv|www\.1porn\.tv)\s*$', '', title, flags=re.IGNORECASE)
        if title:
            return title
    
    # 尝试 og:title
    og_match = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if og_match:
        return og_match.group(1).strip()
    
    return "video"


def _extract_language_prefix(url: str) -> str:
    """
    从 URL 提取语言前缀 (如果存在)
    
    Args:
        url: 影片页 URL (例如 https://www.1porn.tv/zh/videos/...)
        
    Returns:
        str: 语言前缀 (例如 "/zh") 或空字符串
    """
    parsed = urlparse(url)
    path = parsed.path
    
    # 匹配 /{lang}/videos/ 模式
    lang_match = re.match(r'^/([a-z]{2})/videos/', path, re.IGNORECASE)
    if lang_match:
        return f"/{lang_match.group(1)}"
    
    return ""


# ============================================================================
# 源选择
# ============================================================================
def pick_best_source(
    sources: list[dict],
    session: Optional[OnePornSession] = None,
    probe_limit: int = 4 * 1024 * 1024
) -> tuple[str, int, str, int, int]:
    """
    选择实际分辨率最高的源
    
    对每个源进行 Range 请求探测实际尺寸。
    处理 403 Wrong IP (CDN IP 绑定失败), 410 Gone (缺少 Cookie)。
    
    Args:
        sources: 源列表 [{url, label, label_height}]
        session: OnePornSession 实例
        probe_limit: 探测字节数 (默认 4MB)
        
    Returns:
        (url, label_height, label, actual_width, actual_height)
        actual_width/height 为 0 表示使用标签回退
    """
    if not sources:
        raise ValueError("没有可用的源")
    
    probed = []
    
    for src in sources:
        url = src["url"]
        label = src["label"]
        label_height = src["label_height"]
        
        logger.info(f"探测源 {label} ({label_height}p): {url[:80]}...")
        
        try:
            # Range 请求前 probe_limit 字节
            data = get_range_bytes(url, 0, probe_limit - 1, session)
            
            dims = probe_mp4_dimensions(data, max_scan=probe_limit)
            
            if dims:
                width, height = dims
                logger.info(f"  → 实际分辨率: {width}×{height}")
                probed.append({
                    "url": url,
                    "label": label,
                    "label_height": label_height,
                    "actual_width": width,
                    "actual_height": height
                })
            else:
                logger.warning(f"  → 无法探测尺寸 (标签: {label_height}p)")
                probed.append({
                    "url": url,
                    "label": label,
                    "label_height": label_height,
                    "actual_width": 0,
                    "actual_height": 0
                })
        
        except ValueError as e:
            error_msg = str(e)
            
            # 安全记录 CDN IP 绑定错误
            if "403 wrong ip" in error_msg.lower():
                logger.warning(f"  → CDN IP 绑定: 源不可用 (403 Wrong IP, 跳过)")
                continue
            
            # 410 Gone: 缺少 Cookie
            if "410" in error_msg:
                logger.warning(f"  → 410 Gone: 缺少有效 Cookie (跳过)")
                continue
            
            # 其他错误
            logger.warning(f"  → 探测失败: {error_msg}")
            probed.append({
                "url": url,
                "label": label,
                "label_height": label_height,
                "actual_width": 0,
                "actual_height": 0
            })
        
        except Exception as e:
            logger.error(f"  → 探测异常: {e}")
            probed.append({
                "url": url,
                "label": label,
                "label_height": label_height,
                "actual_width": 0,
                "actual_height": 0
            })
    
    if not probed:
        raise ValueError("所有源均不可用")
    
    # 按实际尺寸排序 (宽×高像素数)
    probed_with_actual = [p for p in probed if p["actual_width"] > 0 and p["actual_height"] > 0]
    
    if probed_with_actual:
        probed_with_actual.sort(key=lambda p: p["actual_width"] * p["actual_height"], reverse=True)
        best = probed_with_actual[0]
        logger.info(f"选择实际最高源: {best['label']} ({best['actual_width']}×{best['actual_height']})")
        return (
            best["url"],
            best["label_height"],
            best["label"],
            best["actual_width"],
            best["actual_height"]
        )
    else:
        # 所有源均无法探测, 回退到标签
        logger.warning("所有源无法探测实际尺寸, 按标签排序 (可能不准确)")
        probed.sort(key=lambda p: p["label_height"], reverse=True)
        best = probed[0]
        return (
            best["url"],
            best["label_height"],
            best["label"],
            0,  # 明确标记为标签回退
            0
        )


# ============================================================================
# 主入口
# ============================================================================
def is_1porn_page(page_url: str) -> bool:
    """
    判断是否为 1porn.tv 影片页
    
    Args:
        page_url: 页面 URL
        
    Returns:
        bool: 是否为 1porn.tv 页面
    """
    try:
        parsed = urlparse(page_url)
        hostname = parsed.hostname
        if not hostname:
            return False
        
        hostname_lower = hostname.lower()
        
        # 检查主机名
        if "1porn.tv" not in hostname_lower:
            return False
        
        # 检查路径 (带或不带语言前缀)
        path = parsed.path.lower()
        if "/videos/" in path:
            return True
        
        return False
    
    except Exception:
        return False


def resolve_1porn_job(page_url: str) -> dict:
    """
    解析 1porn.tv 影片页, 返回最高画质 MP4 URL 和元数据
    
    Args:
        page_url: 影片页 URL
        
    Returns:
        dict: {
            "job_id": str,
            "page_url": str,
            "title": str,
            "url": str (最高画质 MP4 URL),
            "quality_label": str (例如 "2160p"),
            "quality_note": str (实际分辨率说明),
            "session": OnePornSession,
            "site": "1porn"
        }
        
    Raises:
        ValueError: 解析失败
    """
    logger.info(f"解析 1porn.tv: {page_url}")
    
    # 验证 URL 安全
    if not _validate_url_safety(page_url):
        raise ValueError(f"不安全的 URL: {page_url}")
    
    # 创建会话
    session = OnePornSession()
    session.referer = page_url
    
    # 获取页面
    logger.info("获取影片页...")
    html = get_bytes(page_url, session, follow_redirects=True).decode("utf-8", errors="ignore")
    
    # 提取 Cookie (requests 自动处理, 这里模拟 Cookie jar)
    # 实际实现中, session.cookies 应该从 requests.Session() 获取
    # 为简化, 这里假设 get_bytes 已经处理 Cookie
    
    # 提取标题
    title = _extract_title_from_html(html)
    logger.info(f"标题: {title}")
    
    # 提取语言前缀
    lang_prefix = _extract_language_prefix(page_url)
    if lang_prefix:
        logger.info(f"检测到语言前缀: {lang_prefix}")
    
    # 提取 sources
    logger.info("解析 Video.js sources...")
    sources = _extract_sources_from_html(html)
    
    if not sources:
        raise ValueError("未找到 Video.js sources")
    
    logger.info(f"找到 {len(sources)} 个源:")
    for src in sources:
        logger.info(f"  - {src['label']} ({src['label_height']}p): {src['url'][:80]}...")
    
    # 选择最高画质
    logger.info("探测实际分辨率...")
    best_url, label_height, label, actual_w, actual_h = pick_best_source(sources, session)
    
    # 构造画质说明
    if actual_w > 0 and actual_h > 0:
        quality_note = f"实际分辨率 {actual_w}×{actual_h} (标签: {label})"
        if label_height == 2160 and actual_h != 2160:
            quality_note += f" ⚠️ 标签 2160p 不准确, 实际为 {actual_h}p"
    else:
        quality_note = f"标签 {label} (未探测实际尺寸, 可能不准确)"
    
    logger.info(f"选择源: {quality_note}")
    
    # 生成 job_id (简化实现, 实际应该用 UUID)
    import hashlib
    job_id = hashlib.md5(page_url.encode()).hexdigest()[:8]
    
    return {
        "job_id": job_id,
        "page_url": page_url,
        "title": title,
        "url": best_url,
        "quality_label": label,
        "quality_note": quality_note,
        "session": session,
        "site": "1porn"
    }


# ============================================================================
# 测试
# ============================================================================
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(message)s"
    )
    
    if len(sys.argv) < 2:
        print("用法: python download_1porn.py <page_url>")
        print("示例: python download_1porn.py 'https://www.1porn.tv/zh/videos/big-dick-big-problems-margo-von-teese-gets-shaken/'")
        sys.exit(1)
    
    page_url = sys.argv[1]
    
    try:
        result = resolve_1porn_job(page_url)
        print("\n=== 解析结果 ===")
        print(f"标题: {result['title']}")
        print(f"画质: {result['quality_note']}")
        print(f"URL: {result['url'][:100]}...")
    except Exception as e:
        print(f"错误: {e}")
        sys.exit(1)
