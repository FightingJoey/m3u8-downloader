#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fullpornxxx.net 渐进式 MP4 下载适配器

站点: www.fullpornxxx.net
CMS: Kernel Video Sharing (KVS)
播放器: kt_player (KVS 播放器)
格式: 渐进式 MP4 (H.264 + AAC)

关键特性:
- KVS kt_player + generate_mp4 加密调用
- AES-256-CBC + PBKDF2-SHA512-999 解密页面会话
- OK.ru video.get API 获取 MP4 源列表
- CDN: okcdn.ru (签名有效期~24h)
- 探测实际分辨率 (样本最高 720p, 不是 4K)
- **DO NOT send Origin to okcdn.ru** (causes 400 error code 16)

安全:
- SSRF 保护: 白名单 fullpornxxx.net + api.ok.ru + okcdn.ru
- TLS 验证始终开启
- session_key / 签名永不记录
- 每次重定向验证主机名
- 无登录/付费墙/DRM 绕过

实现限制:
- 第一方 get_stream 返回 "error" (不可用)
- 使用 OK.ru API (公开播放器路径)
- 样本最高 720p (1280×720), 无 fullhd/ultrahd
- 探测实际尺寸, 不信标签
"""

import sys
import re
import json
import logging
import struct
import base64
import ipaddress
from urllib.parse import urlparse, urljoin
from typing import Optional
import requests

# Crypto 依赖 (必须安装 pycryptodome)
try:
    from Crypto.Cipher import AES
    from Crypto.Protocol.KDF import PBKDF2
    from Crypto.Hash import SHA512
except ImportError:
    print("错误: 缺少 pycryptodome 库")
    print("请运行: pip3 install pycryptodome")
    sys.exit(1)

# ============================================================================
# 配置
# ============================================================================
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TIMEOUT = 30

logger = logging.getLogger(__name__)

# OK.ru API 常量 (来自 kt_player_new.js)
OK_API_URL_TEMPLATE = (
    "https://api.ok.ru/fb.do"
    "?application_key=CBAFJIICABABABABA"
    "&fields=video.url_tiny,video.url_low,video.url_high,video.url_medium,"
    "video.url_quadhd,video.url_mobile,video.url_ultrahd,video.url_fullhd,"
    "&method=video.get"
    "&session_key={session_key}"
    "&vids={video_ids}"
)

# 画质标签映射 (来自 kt_player)
QUALITY_LABELS = {
    "url_mobile": "144",
    "url_tiny": "240",
    "url_low": "360",
    "url_medium": "480",
    "url_high": "720",
    "url_fullhd": "1080",
    "url_quadhd": "1440",
    "url_ultrahd": "2160"
}


# ============================================================================
# SSRF 保护
# ============================================================================
def _is_safe_hostname(hostname: str) -> bool:
    """
    严格主机名验证: 白名单 fullpornxxx.net + api.ok.ru + okcdn.ru
    
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
    
    # 白名单: fullpornxxx.net, api.ok.ru, okcdn.ru
    hostname_lower = hostname.lower()
    allowed = (
        "fullpornxxx.net",
        "www.fullpornxxx.net",
        "api.ok.ru",
        "okcdn.ru"
    )
    
    # 完全匹配或子域名
    if hostname_lower in allowed:
        return True
    if hostname_lower.endswith(".fullpornxxx.net"):
        return True
    if hostname_lower.endswith(".ok.ru"):
        return True
    if hostname_lower.endswith(".okcdn.ru"):
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
class FpxSession:
    """fullpornxxx.net 会话管理器"""
    
    def __init__(self):
        self.cookies = {}  # {name: value}
        self.referer = ""  # 影片页 URL



def _merge_response_cookies(session, resp) -> None:
    """Persist Set-Cookie into the adapter session so later media fetches share it."""
    if session is None or resp is None:
        return
    jar = getattr(session, "cookies", None)
    if jar is None:
        return
    try:
        incoming = resp.cookies.get_dict() if hasattr(resp.cookies, "get_dict") else {}
    except Exception:
        incoming = {}
    if not incoming:
        return
    try:
        jar.update(incoming)
    except Exception:
        pass


def get_bytes(
    url: str,
    session: Optional[FpxSession] = None,
    follow_redirects: bool = True,
    send_origin: bool = False
) -> bytes:
    """
    获取 URL 内容, 带 SSRF 保护和重定向验证
    
    **重要**: okcdn.ru 不支持 Origin 头 (返回 400 错误代码 16)
    
    Args:
        url: 目标 URL
        session: FpxSession 实例 (可选)
        follow_redirects: 是否跟随重定向
        send_origin: 是否发送 Origin 头 (okcdn.ru 必须为 False)
        
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
    
    # **DO NOT send Origin to okcdn.ru**
    if send_origin and not url.lower().find("okcdn.ru") > -1:
        parsed = urlparse(session.referer if session and session.referer else url)
        if parsed.scheme and parsed.hostname:
            headers["Origin"] = f"{parsed.scheme}://{parsed.hostname}"
    
    cookies = session.cookies if session else {}
    
    resp = requests.get(
        url,
        headers=headers,
        cookies=cookies,
        timeout=TIMEOUT,
        allow_redirects=False,
        verify=True  # TLS 验证始终开启
    )
    _merge_response_cookies(session, resp)
    
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
        return get_bytes(redirect_url, session, follow_redirects=True, send_origin=send_origin)
    
    if resp.status_code != 200:
        raise ValueError(f"HTTP {resp.status_code}: {url}")
    
    return resp.content


def get_range_bytes(
    url: str,
    start: int,
    end: int,
    session: Optional[FpxSession] = None
) -> bytes:
    """
    Range 请求, 带 SSRF 保护
    
    **重要**: okcdn.ru 不支持 Origin 头
    
    Args:
        url: 目标 URL
        start: 起始字节 (包含)
        end: 结束字节 (包含)
        session: FpxSession 实例 (可选)
        
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
    
    # **DO NOT send Origin to okcdn.ru**
    
    cookies = session.cookies if session else {}
    
    resp = requests.get(
        url,
        headers=headers,
        cookies=cookies,
        timeout=TIMEOUT,
        allow_redirects=False,
        verify=True
    )
    _merge_response_cookies(session, resp)
    
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
    
    # 接受 206 Partial Content 或 200 OK
    if resp.status_code not in (200, 206):
        raise ValueError(f"Range 请求失败 HTTP {resp.status_code}: {url}")
    
    return resp.content


# ============================================================================
# MP4 探测 (复用 fpv/wh/hdp/1porn 实现)
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
# Crypto (AES-256-CBC + PBKDF2-SHA512)
# ============================================================================
def decrypt_session_key(cipher_blob: str, passphrase: str) -> str:
    """
    解密 OK.ru session_key
    
    来自 kt_player_new.js Encryption.decrypt:
    - 外层: Base64(UTF-8 JSON {ciphertext, iv, salt, iterations})
    - 内层: AES-256-CBC, PBKDF2-SHA512-999
    
    Args:
        cipher_blob: Base64 编码的加密 JSON
        passphrase: 密码短语 (来自页面 HTML)
        
    Returns:
        str: 解密后的 session_key
        
    Raises:
        ValueError: 解密失败
    """
    try:
        # 解码外层 Base64
        json_str = base64.b64decode(cipher_blob).decode("utf-8")
        crypto_data = json.loads(json_str)
        
        # 提取字段
        ciphertext_b64 = crypto_data["ciphertext"]
        iv_hex = crypto_data["iv"]
        salt_hex = crypto_data["salt"]
        iterations = crypto_data.get("iterations", 999)
        
        # 解码
        ciphertext = base64.b64decode(ciphertext_b64)
        iv = bytes.fromhex(iv_hex)
        salt = bytes.fromhex(salt_hex)
        
        # PBKDF2-SHA512 派生密钥 (32 字节 = AES-256)
        key = PBKDF2(
            passphrase.encode("utf-8"),
            salt,
            dkLen=32,
            count=iterations,
            hmac_hash_module=SHA512
        )
        
        # AES-256-CBC 解密
        cipher = AES.new(key, AES.MODE_CBC, iv)
        plaintext = cipher.decrypt(ciphertext)
        
        # 移除 PKCS7 padding
        padding_len = plaintext[-1]
        if padding_len > 16 or padding_len < 1:
            raise ValueError("无效的 padding")
        
        plaintext = plaintext[:-padding_len]
        
        return plaintext.decode("utf-8")
    
    except Exception as e:
        raise ValueError(f"解密失败: {e}")


# ============================================================================
# 页面解析
# ============================================================================
def _extract_generate_mp4_call(html: str) -> Optional[tuple[str, str, str, str]]:
    """
    从 HTML 提取 generate_mp4() 调用
    
    Args:
        html: 页面 HTML
        
    Returns:
        (cipher_blob, passphrase, ok_video_ids, page_video_id) 或 None
    """
    # 匹配: generate_mp4('cipher', 'pass', 'ok_ids', 'page_id')
    pattern = re.compile(
        r"generate_mp4\(\s*'([^']+)',\s*'([^']+)',\s*'([^']+)',\s*'([^']+)'\s*\)",
        re.IGNORECASE
    )
    
    match = pattern.search(html)
    if match:
        return match.groups()
    
    return None


def _extract_title_from_html(html: str) -> str:
    """
    从 HTML 提取视频标题
    
    优先级:
    1. <title> 标签 (移除站点后缀)
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
        title = re.sub(r'\s*-\s*(fullpornxxx\.net|www\.fullpornxxx\.net)\s*$', '', title, flags=re.IGNORECASE)
        if title:
            return title
    
    # 尝试 og:title
    og_match = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if og_match:
        return og_match.group(1).strip()
    
    return "video"


def _extract_video_id_from_url(url: str) -> Optional[str]:
    """
    从 URL 提取 video_id
    
    Args:
        url: 影片页 URL (https://www.fullpornxxx.net/video/35975/...)
        
    Returns:
        str: video_id 或 None
    """
    match = re.search(r'/video/(\d+)/', url, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


# ============================================================================
# OK.ru API
# ============================================================================
def get_ok_video_sources(session_key: str, ok_video_ids: str) -> dict:
    """
    调用 OK.ru video.get API
    
    Args:
        session_key: 解密后的 session_key
        ok_video_ids: OK.ru 视频 ID (逗号分隔)
        
    Returns:
        dict: API 响应 JSON
        
    Raises:
        ValueError: API 调用失败
    """
    url = OK_API_URL_TEMPLATE.format(
        session_key=session_key,
        video_ids=ok_video_ids
    )
    
    # 验证 URL 安全性
    if not _validate_url_safety(url):
        raise ValueError(f"不安全的 API URL: {url}")
    
    headers = {"User-Agent": USER_AGENT}
    
    resp = requests.get(
        url,
        headers=headers,
        timeout=TIMEOUT,
        verify=True
    )
    
    if resp.status_code != 200:
        raise ValueError(f"OK.ru API 失败 HTTP {resp.status_code}")
    
    try:
        return resp.json()
    except json.JSONDecodeError as e:
        raise ValueError(f"OK.ru API 返回无效 JSON: {e}")


def parse_ok_sources(api_response: dict) -> list[dict]:
    """
    解析 OK.ru API 响应, 提取视频源
    
    Args:
        api_response: OK.ru video.get JSON 响应
        
    Returns:
        list of {url, label, label_height}
        按 label_height 降序排列
    """
    sources = []
    
    videos = api_response.get("videos", [])
    if not videos:
        return sources
    
    video = videos[0]  # 通常只有一个视频
    
    for key, label in QUALITY_LABELS.items():
        url = video.get(key)
        if url:
            sources.append({
                "url": url,
                "label": label,
                "label_height": int(label)
            })
    
    # 按标签高度降序
    sources.sort(key=lambda x: x["label_height"], reverse=True)
    
    return sources


# ============================================================================
# 源选择
# ============================================================================
def pick_best_source(
    sources: list[dict],
    session: Optional[FpxSession] = None,
    probe_limit: int = 4 * 1024 * 1024
) -> tuple[str, int, str, int, int]:
    """
    选择实际分辨率最高的源
    
    对每个源进行 Range 请求探测实际尺寸。
    
    Args:
        sources: 源列表 [{url, label, label_height}]
        session: FpxSession 实例
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
def is_fpx_page(page_url: str) -> bool:
    """
    判断是否为 fullpornxxx.net 影片页
    
    Args:
        page_url: 页面 URL
        
    Returns:
        bool: 是否为 fullpornxxx 页面
    """
    try:
        parsed = urlparse(page_url)
        hostname = parsed.hostname
        if not hostname:
            return False
        
        hostname_lower = hostname.lower()
        
        # 检查主机名
        if "fullpornxxx.net" not in hostname_lower:
            return False
        
        # 检查路径 (/video/数字/)
        path = parsed.path.lower()
        if re.search(r'/video/\d+/', path):
            return True
        
        return False
    
    except Exception:
        return False


def resolve_fpx_job(page_url: str) -> dict:
    """
    解析 fullpornxxx.net 影片页, 返回最高画质 MP4 URL 和元数据
    
    Args:
        page_url: 影片页 URL
        
    Returns:
        dict: {
            "job_id": str,
            "page_url": str,
            "title": str,
            "url": str (最高画质 MP4 URL),
            "quality_label": str (例如 "720"),
            "quality_note": str (实际分辨率说明),
            "session": FpxSession,
            "site": "fpx"
        }
        
    Raises:
        ValueError: 解析失败
    """
    logger.info(f"解析 fullpornxxx.net: {page_url}")
    
    # 验证 URL 安全
    if not _validate_url_safety(page_url):
        raise ValueError(f"不安全的 URL: {page_url}")
    
    # 创建会话
    session = FpxSession()
    session.referer = page_url
    
    # 获取页面
    logger.info("获取影片页...")
    html = get_bytes(page_url, session, follow_redirects=True).decode("utf-8", errors="ignore")
    
    # 提取标题
    title = _extract_title_from_html(html)
    logger.info(f"标题: {title}")
    
    # 提取 video_id
    video_id = _extract_video_id_from_url(page_url)
    if not video_id:
        raise ValueError("无法从 URL 提取 video_id")
    logger.info(f"Video ID: {video_id}")
    
    # 提取 generate_mp4 调用
    logger.info("解析 generate_mp4 调用...")
    gm4_data = _extract_generate_mp4_call(html)
    if not gm4_data:
        raise ValueError("未找到 generate_mp4() 调用")
    
    cipher_blob, passphrase, ok_video_ids, page_video_id = gm4_data
    logger.info(f"OK.ru Video IDs: {ok_video_ids}")
    
    # 解密 session_key
    logger.info("解密 session_key...")
    try:
        session_key = decrypt_session_key(cipher_blob, passphrase)
        logger.info("Session key 解密成功")
    except ValueError as e:
        raise ValueError(f"Session key 解密失败: {e}")
    
    # 调用 OK.ru API
    logger.info("调用 OK.ru video.get API...")
    try:
        api_response = get_ok_video_sources(session_key, ok_video_ids)
    except ValueError as e:
        raise ValueError(f"OK.ru API 调用失败: {e}")
    
    # 解析源列表
    logger.info("解析 OK.ru 响应...")
    sources = parse_ok_sources(api_response)
    
    if not sources:
        raise ValueError("OK.ru API 未返回任何源")
    
    logger.info(f"找到 {len(sources)} 个源:")
    for src in sources:
        logger.info(f"  - {src['label']} ({src['label_height']}p): {src['url'][:80]}...")
    
    # 选择最高画质
    logger.info("探测实际分辨率...")
    best_url, label_height, label, actual_w, actual_h = pick_best_source(sources, session)
    
    # 构造画质说明
    if actual_w > 0 and actual_h > 0:
        quality_note = f"实际分辨率 {actual_w}×{actual_h} (标签: {label})"
    else:
        quality_note = f"标签 {label} (未探测实际尺寸, 可能不准确)"
    
    logger.info(f"选择源: {quality_note}")
    
    # 生成 job_id
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
        "site": "fpx"
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
        print("用法: python download_fpx.py <page_url>")
        print("示例: python download_fpx.py 'https://www.fullpornxxx.net/video/35975/gimme-all-2_v2/'")
        sys.exit(1)
    
    page_url = sys.argv[1]
    
    try:
        result = resolve_fpx_job(page_url)
        print("\n=== 解析结果 ===")
        print(f"标题: {result['title']}")
        print(f"画质: {result['quality_note']}")
        print(f"URL: {result['url'][:100]}...")
    except Exception as e:
        print(f"错误: {e}")
        sys.exit(1)
