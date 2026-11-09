#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 Pornhub 视频页抓取「最高可用清晰度」的 index-v1-a1.m3u8，下载全部分片，
再用 FFmpeg 合并成一条视频（默认 ./out/video_1080p.mp4）。

依赖:
  - Python 3.10+
  - ffmpeg / ffprobe（系统安装）
  - 可选: playwright（仅 --headed / 浏览器兜底时需要）

用 uv 运行示例:
  # 安装系统 ffmpeg（Debian/Ubuntu）:
  #   sudo apt-get install -y ffmpeg
  uv run python download_ph_1080.py "https://www.pornhub.com/view_video.php?viewkey=XXXX"
  uv run python download_ph_1080.py --no-browser "视频页URL"
  uv run python download_ph_1080.py --headed "视频页URL"   # 需要: uv add playwright && uv run playwright install chromium

策略:
  1) HTTP GET 页面（带年龄 Cookie + 浏览器级请求头），从 mediaDefinitions / HTML 提取 .m3u8
  2) 在所有清晰度中选最高，并优先 index-v1-a1 媒体播放列表（master 会自动展开）
  3) CDN 分片请求使用真实浏览器媒体请求头（Referer/Origin/Sec-Fetch-*）+ Cookie jar
  4) 470/410 时在 ev-h / hv-h 等主机间故障转移
  5) FFmpeg concat 合并，优先 -c copy，失败再转码
"""

from __future__ import annotations

import concurrent.futures
import gzip
import html
import zlib
import http.cookiejar
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

DEFAULT_URL = "https://www.pornhub.com/view_video.php?viewkey=6a95e0f73bfc8"
OUT_DIR = Path("./out")
WORK_DIR = OUT_DIR / "segments"
PLAYLIST_PATH = OUT_DIR / "index_1080p_v1_a1.m3u8"
CONCAT_LIST = OUT_DIR / "concat.txt"
OUTPUT_MP4 = OUT_DIR / "video_1080p.mp4"
WAIT_SECONDS = 45
# 略降默认并发；主修复仍是请求头 / Cookie，不是并发
SEG_WORKERS = 4
# 与当前桌面 Chrome 对齐（页面与 CDN 共用）
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
)
SEC_CH_UA = '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"'
AGE_COOKIE_PAIRS = [
    ("accessAgeDisclaimerPH", "1"),
    ("accessAgeDisclaimerUK", "1"),
    ("age_verified", "1"),
    ("cookiesBanner", "closed"),
    ("platform", "pc"),
    ("bs", "1"),
    ("ss", "1"),
]
CDN_HOST_FALLBACKS = [
    "ev-h.phncdn.com",
    "hv-h.phncdn.com",
    "cv-h.phncdn.com",
    "ei-h.phncdn.com",
    "ev.phncdn.com",
    "cdn1d-static-shared.phncdn.com",
]
DISMISS_SELECTORS = [
    "button.js-closeAgeModal",
    "button.buttonOver18",
    "button:has-text('I am 18')",
    "button:has-text('Enter')",
    "button:has-text('Ok')",
    "button:has-text('Accept')",
    "button:has-text('同意')",
]
PLAY_SELECTORS = [".mgp_play", "button[aria-label='Play']", "video"]


def parse_args(argv: list[str]) -> tuple[str, bool, bool, Path]:
    headed = os.environ.get("HEADED", "").strip().lower() in ("1", "true", "yes")
    use_browser = True
    url = DEFAULT_URL
    out = OUTPUT_MP4
    positional: list[str] = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("--headed", "-H"):
            headed = True
        elif a == "--headless":
            headed = False
        elif a in ("--no-browser", "--http-only"):
            use_browser = False
        elif a == "--out" and i + 1 < len(argv):
            i += 1
            out = Path(argv[i])
        elif a.startswith("-"):
            print(f"[warn] 忽略未知参数: {a}")
        else:
            positional.append(a)
        i += 1
    if positional:
        url = positional[0]
    return url, headed, use_browser, out


def require_ffmpeg() -> None:
    if not shutil.which("ffmpeg"):
        print(
            "[fail] 未找到 ffmpeg。\n"
            "  Debian/Ubuntu: sudo apt-get install -y ffmpeg\n"
            "  macOS: brew install ffmpeg\n"
            "  Windows: 安装后把 ffmpeg 加到 PATH"
        )
        raise SystemExit(2)
    print(f"[info] ffmpeg: {shutil.which('ffmpeg')}")
    if shutil.which("ffprobe"):
        print(f"[info] ffprobe: {shutil.which('ffprobe')}")


def viewkey_from_url(url: str) -> str | None:
    q = parse_qs(urlparse(url).query)
    if q.get("viewkey"):
        return q["viewkey"][0]
    m = re.search(r"/embed/([a-zA-Z0-9]+)", url)
    return m.group(1) if m else None



def _clean_ph_title(raw: str) -> str:
    """Strip Pornhub site suffixes and tidy whitespace."""
    t = html.unescape(raw or "").strip()
    if not t:
        return ""
    # Common suffixes: " - Pornhub.com", " | Pornhub", " - Pornhub"
    t = re.sub(
        r"\s*[\-|–—|]\s*Pornhub(?:\.com)?\s*$",
        "",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(r"\s+", " ", t).strip()
    return t


def extract_video_title(html_text: str) -> str | None:
    """Extract real video title from Pornhub page HTML.

    Preference order:
      1) og:title
      2) twitter:title
      3) flashvars / JSON video_title
      4) <title> with site suffix cleaned
    """
    if not html_text:
        return None

    def meta_content(prop: str) -> str | None:
        # property/name before content
        m = re.search(
            rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']',
            html_text,
            re.IGNORECASE,
        )
        if m:
            return m.group(1)
        # content before property/name
        m = re.search(
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{re.escape(prop)}["\']',
            html_text,
            re.IGNORECASE,
        )
        return m.group(1) if m else None

    for key in ("og:title", "twitter:title"):
        val = meta_content(key)
        if val:
            cleaned = _clean_ph_title(val)
            if cleaned:
                return cleaned

    # flashvars / media JSON video_title (may be unicode-escaped)
    m = re.search(r'"video_title"\s*:\s*"((?:\\.|[^"\\])*)"', html_text)
    if m:
        import json as _json

        try:
            raw = _json.loads('"' + m.group(1) + '"')
        except Exception:
            raw = m.group(1).replace("\\/", "/")
        cleaned = _clean_ph_title(str(raw))
        if cleaned:
            return cleaned

    m = re.search(r"<title[^>]*>([^<]+)</title>", html_text, re.IGNORECASE)
    if m:
        cleaned = _clean_ph_title(m.group(1))
        if cleaned:
            return cleaned
    return None


def site_origin(url: str) -> str:
    p = urlparse(url)
    host = (p.hostname or "www.pornhub.com").lower()
    # CDN / 媒体请求统一用主站 Origin，更接近真实播放器
    if "pornhub" in host:
        if host.startswith("cn."):
            return "https://cn.pornhub.com"
        return "https://www.pornhub.com"
    scheme = p.scheme or "https"
    return f"{scheme}://{host}"


def site_referer(url: str) -> str:
    """CDN 防盗链常用整页 URL；若不是视频页则退回站点根。"""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if "pornhub" in host and ("view_video" in (p.path or "") or "/embed/" in (p.path or "")):
        # 去掉 fragment；保留 query（含 viewkey）
        return urlunparse((p.scheme or "https", p.netloc, p.path, "", p.query, ""))
    if "pornhub" in host:
        return site_origin(url) + "/"
    return "https://www.pornhub.com/"


def _make_cookie(
    name: str,
    value: str,
    domain: str,
    *,
    path: str = "/",
    secure: bool = True,
) -> http.cookiejar.Cookie:
    return http.cookiejar.Cookie(
        0,
        name,
        value,
        None,
        False,
        domain,
        True,
        domain.startswith("."),
        path,
        False,
        secure,
        None,
        False,
        None,
        None,
        {},
    )


_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


def sanitize_proxy_env() -> list[str]:
    """Drop empty/whitespace *_proxy env keys so they cannot wipe uppercase ones.

    CPython getproxies_environment() prefers lowercase; an empty http_proxy=''
    removes any HTTP_PROXY that was already collected. Compose ${http_proxy:-}
    used to inject those empty strings into Docker.

    Returns names that were deleted (for optional logging).
    """
    removed: list[str] = []
    for key in list(os.environ.keys()):
        if not key.lower().endswith("_proxy"):
            continue
        if not (os.environ.get(key) or "").strip():
            del os.environ[key]
            removed.append(key)
    return removed


def env_proxies() -> dict[str, str]:
    """Build an explicit scheme->URL dict for ProxyHandler (never trust getproxies alone).

    Checks both cases; prefers a non-empty value. Maps ALL_PROXY onto http/https
    when those schemes are missing so urllib always has a tunnel target.
    """
    sanitize_proxy_env()

    def _first(*names: str) -> str:
        for n in names:
            v = (os.environ.get(n) or "").strip()
            if v:
                return v
        return ""

    http = _first("http_proxy", "HTTP_PROXY")
    https = _first("https_proxy", "HTTPS_PROXY")
    all_p = _first("all_proxy", "ALL_PROXY")
    no_p = _first("no_proxy", "NO_PROXY")

    proxies: dict[str, str] = {}
    if http:
        proxies["http"] = http
    if https:
        proxies["https"] = https
    if all_p:
        proxies["all"] = all_p
        proxies.setdefault("http", all_p)
        proxies.setdefault("https", all_p)
    if no_p:
        proxies["no"] = no_p
    return proxies


class BrowserSession:
    """共享 CookieJar + 页面/媒体两套请求头，供线程池复用。"""

    def __init__(self, page_url: str):
        self.page_url = page_url
        self.origin = site_origin(page_url)
        self.referer = site_referer(page_url)
        self.cj = http.cookiejar.CookieJar()
        self._lock = threading.RLock()
        # Explicit ProxyHandler dict — empty lowercase env must not disable proxies.
        proxies = env_proxies()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(proxies),
            urllib.request.HTTPCookieProcessor(self.cj),
        )
        # addheaders 作为兜底；实际每次 Request 会带完整 headers
        self.opener.addheaders = [
            ("User-Agent", UA),
            ("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8"),
        ]
        self._seed_age_cookies()

    def _seed_age_cookies(self) -> None:
        hosts = {
            ".pornhub.com",
            ".pornhub.org",
            ".pornhub.net",
            "www.pornhub.com",
            "cn.pornhub.com",
        }
        page_host = urlparse(self.page_url).hostname
        if page_host:
            hosts.add(page_host)
            parts = page_host.split(".")
            if len(parts) >= 2:
                hosts.add("." + ".".join(parts[-2:]))
        for domain in hosts:
            for name, val in AGE_COOKIE_PAIRS:
                try:
                    self.cj.set_cookie(_make_cookie(name, val, domain))
                except Exception:
                    pass

    def update_referer(self, url: str) -> None:
        if url and "pornhub" in (urlparse(url).hostname or ""):
            self.referer = site_referer(url)
            self.origin = site_origin(url)
            self.page_url = url

    def page_headers(self) -> dict[str, str]:
        return {
            "User-Agent": UA,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8,"
                "application/signed-exchange;v=b3;q=0.7"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",  # 不含 br：stdlib 无法解 Brotli
            "Cache-Control": "max-age=0",
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
        """逼近浏览器对 HLS playlist / .ts 分片的媒体请求。"""
        ref = referer or self.referer or "https://www.pornhub.com/"
        origin = site_origin(ref) if "pornhub" in (urlparse(ref).hostname or "") else self.origin
        return {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            # 分片为二进制；声明 identity，避免部分环境对 br 处理异常
            "Accept-Encoding": "identity",
            "Referer": ref if ref.endswith("/") or "view_video" in ref or "/embed/" in ref else ref + "/",
            "Origin": origin,
            "Sec-Ch-Ua": SEC_CH_UA,
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site",
            "Connection": "keep-alive",
        }

    def cookie_header_for(self, url: str) -> str | None:
        """手动拼 Cookie，便于在 CDN 域也带上主站年龄 Cookie（若 jar 未自动匹配）。"""
        with self._lock:
            req = urllib.request.Request(url)
            self.cj.add_cookie_header(req)
            h = req.get_header("Cookie")
            if h:
                return h
            # CDN 域通常吃不到 .pornhub.com cookie；补一份年龄 Cookie 作兜底
            host = (urlparse(url).hostname or "").lower()
            if "phncdn.com" in host or "pornhub" in host:
                return "; ".join(f"{n}={v}" for n, v in AGE_COOKIE_PAIRS)
        return None


def rewrite_cdn_hosts(url: str) -> list[str]:
    p = urlparse(url)
    preferred: list[str] = []
    for h in [p.hostname, *CDN_HOST_FALLBACKS]:
        if h and h not in preferred:
            preferred.append(h)
    return [
        urlunparse((p.scheme or "https", h, p.path, p.params, p.query, p.fragment))
        for h in preferred
    ]


def _is_failover_status(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        # 470: PH CDN 客户端/防盗链拒绝；410: 签名过期或资源失效；403/404 也试备用域
        return exc.code in (470, 410, 403, 404, 502, 503)
    return False



def maybe_decompress(data: bytes, content_encoding: str | None = None) -> bytes:
    """urllib 在部分环境下不会自动解 Content-Encoding，按魔数兜底。"""
    if not data:
        return data
    enc = (content_encoding or "").lower()
    if data[:2] == bytes([0x1F, 0x8B]) or "gzip" in enc:
        try:
            return gzip.decompress(data)
        except Exception:
            pass
    if data[:2] in (bytes([0x78, 0x9C]), bytes([0x78, 0x01]), bytes([0x78, 0xDA])) or "deflate" in enc:
        try:
            return zlib.decompress(data)
        except Exception:
            try:
                return zlib.decompress(data, -zlib.MAX_WBITS)
            except Exception:
                pass
    return data


def http_get_bytes(
    session: BrowserSession,
    url: str,
    *,
    referer: str | None = None,
    try_hosts: bool = True,
    for_media: bool = True,
) -> tuple[str, bytes]:
    if for_media:
        headers = session.media_headers(referer=referer)
    else:
        headers = session.page_headers()
        if referer:
            headers["Referer"] = referer
            headers["Sec-Fetch-Site"] = "same-origin"
            headers["Sec-Fetch-Mode"] = "navigate"

    candidates = rewrite_cdn_hosts(url) if try_hosts and for_media else [url]
    last: Exception | None = None
    for cand in candidates:
        hdrs = dict(headers)
        cookie = session.cookie_header_for(cand)
        if cookie:
            hdrs["Cookie"] = cookie
        try:
            req = urllib.request.Request(cand, headers=hdrs)
            with session.opener.open(req, timeout=60) as resp:
                # Set-Cookie 已由 HTTPCookieProcessor 进 jar
                data = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                data = maybe_decompress(data, enc)
                return resp.geturl(), data
        except Exception as e:
            last = e
            host = urlparse(cand).hostname
            print(f"[warn] GET {host}: {e}")
            if for_media and not _is_failover_status(e) and not isinstance(
                e, (urllib.error.URLError, TimeoutError, OSError)
            ):
                # 非故障转移类错误仍继续试其它 host（网络抖动）
                continue
    assert last is not None
    raise last


def http_get_text(
    session: BrowserSession,
    url: str,
    referer: str | None = None,
    *,
    try_hosts: bool = True,
    for_media: bool = True,
) -> tuple[str, str]:
    final, raw = http_get_bytes(
        session,
        url,
        referer=referer,
        try_hosts=try_hosts,
        for_media=for_media,
    )
    return final, raw.decode("utf-8", "replace")


def extract_m3u8_urls(html: str) -> list[str]:
    """从 HTML / JSON 转义文本中提取 .m3u8 URL（含 JSON \\/ 转义）。"""
    raw = re.findall(r'https:\\?/\\?/[^"\'\s<>]+?\.m3u8[^"\'\s<>]*', html)
    urls = [u.replace("\\/", "/").replace("&amp;", "&") for u in raw]
    # 优先再从 mediaDefinitions JSON 取，避免漏掉 validfrom 变体
    for u in extract_media_definition_urls(html):
        urls.append(u)
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def quality_height_from_url(url: str) -> int:
    """从 CDN 路径识别清晰度高度（2160/1440/1080/720/480/240 等）。"""
    path = urlparse(url).path or ""
    # Pornhub 常见：/1080P_4000K_.../ 或 /720P_/
    m = re.search(r"(?:^|/)(\d{3,4})[pP](?:_|/|$)", path)
    if m:
        return int(m.group(1))
    low = path.lower()
    for h in (2160, 1440, 1080, 720, 480, 360, 240):
        if f"{h}p" in low:
            return h
    return 0


def quality_height_from_field(quality) -> int:
    """解析 mediaDefinitions[*].quality（可为 int/str/list）。"""
    if quality is None:
        return 0
    if isinstance(quality, bool):
        return 0
    if isinstance(quality, (int, float)):
        return int(quality)
    if isinstance(quality, (list, tuple)):
        best = 0
        for item in quality:
            best = max(best, quality_height_from_field(item))
        return best
    s = str(quality).strip().lower()
    if not s or s in ("false", "true", "auto"):
        return 0
    m = re.search(r"(\d{3,4})", s)
    return int(m.group(1)) if m else 0


def extract_media_definition_entries(html: str) -> list[tuple[str, int]]:
    """解析 flashvars.mediaDefinitions[*] -> (videoUrl, quality_height)。"""
    import json

    out: list[tuple[str, int]] = []
    key = '"mediaDefinitions"'
    pos = 0
    while True:
        idx = html.find(key, pos)
        if idx < 0:
            break
        bracket = html.find("[", idx)
        if bracket < 0:
            break
        depth = 0
        end = None
        for i in range(bracket, len(html)):
            ch = html[i]
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is None:
            break
        blob = html[bracket:end]
        try:
            data = json.loads(blob)
        except Exception:
            pos = end
            continue
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                vu = item.get("videoUrl")
                if not isinstance(vu, str) or ".m3u8" not in vu:
                    continue
                u = vu.replace("\\/", "/").replace("&amp;", "&")
                h = quality_height_from_field(item.get("quality"))
                if h <= 0:
                    h = quality_height_from_url(u)
                out.append((u, h))
        pos = end
    return out


def extract_media_definition_urls(html: str) -> list[str]:
    """解析 flashvars.mediaDefinitions[*].videoUrl。"""
    return [u for u, _h in extract_media_definition_entries(html)]


def url_token_style(url: str) -> str:
    q = urlparse(url).query
    if "validfrom=" in q and "hash=" in q:
        return "validfrom"
    if re.search(r"(?:^|[?&])h=", q) and re.search(r"(?:^|[?&])e=", q):
        return "h_e"
    return "other"


def playlist_body_style(body: str) -> str:
    """区分用户要的 validfrom+.ts 与错误的 h/e fMP4 或旧 h/e TS。"""
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    segs = [ln for ln in lines if not ln.startswith("#")]
    if not segs:
        return "empty"
    if "#EXT-X-MAP:" in body or any(".m4s" in s for s in segs):
        return "fmp4"
    sample = "\n".join(segs[:8])
    if "validfrom=" in sample and "hash=" in sample and ".ts" in sample:
        return "validfrom_ts"
    if re.search(r"(?:^|[?&])h=", sample) and ".ts" in sample:
        return "h_e_ts"
    if ".ts" in sample:
        return "ts_other"
    return "other"


def is_preferred_media_playlist(body: str) -> bool:
    return playlist_body_style(body) == "validfrom_ts"


def is_1080_index_v1(url: str) -> bool:
    path = urlparse(url).path
    low = path.lower()
    return ("1080p" in low or "1080P" in path) and "index-v1-a1.m3u8" in low


def is_index_v1_a1(url: str) -> bool:
    return "index-v1-a1.m3u8" in (urlparse(url).path or "").lower()


def is_1080_master_or_media(url: str) -> bool:
    """兼容旧名：路径含任意已知清晰度即视为 HLS 候选（不仅 1080）。"""
    return quality_height_from_url(url) > 0 or ".m3u8" in url.lower()


def is_hls_candidate_url(url: str) -> bool:
    return ".m3u8" in (url or "").lower()


def child_playlist_urls(master_url: str, body: str) -> list[str]:
    base = master_url.rsplit("/", 1)[0] + "/"
    out: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ".m3u8" not in line:
            continue
        out.append(urljoin(base, line))
    return out


def parse_media_segments(playlist_url: str, body: str) -> tuple[str | None, list[str]]:
    """Return (init_map_url_or_None, segment_urls)."""
    base = playlist_url.rsplit("/", 1)[0] + "/"
    init: str | None = None
    m = re.search(r'#EXT-X-MAP:URI="([^"]+)"', body)
    if m:
        init = urljoin(base, m.group(1))
    segs: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ".m3u8" in line:
            continue
        segs.append(urljoin(base, line))
    return init, segs


def score_hls_candidate(url: str, height_hint: int = 0) -> tuple[int, int, int, int]:
    """Higher is better: height, then validfrom/index/ev-h/bitrate。"""
    path = urlparse(url).path
    host = (urlparse(url).hostname or "").lower()
    height = height_hint or quality_height_from_url(url)
    score = 0
    if is_index_v1_a1(url):
        score += 50
    if path.lower().endswith("master.m3u8"):
        score += 10  # usable via expand
    style = url_token_style(url)
    if style == "validfrom":
        score += 300
    elif style == "h_e":
        score -= 80
    if host.startswith("ev-h."):
        score += 40
    elif host.startswith("hv-h."):
        score -= 40
    m = re.search(r"(\d+)K_", path)
    kbps = int(m.group(1)) if m else 0
    return height, score, kbps, len(url)


def score_1080_candidate(url: str) -> tuple[int, int, int]:
    """兼容旧接口：忽略高度主序，仅返回次级分数三元组。"""
    _h, score, kbps, n = score_hls_candidate(url)
    return score, kbps, n


def pick_best_hls_source(
    urls: list[str],
    height_by_url: dict[str, int] | None = None,
) -> str | None:
    """在全部 m3u8 候选中选最高清晰度；同高度再按 validfrom / index-v1-a1 等偏好。"""
    height_by_url = height_by_url or {}
    cands = [u for u in urls if is_hls_candidate_url(u)]
    if not cands:
        return None

    def key(u: str) -> tuple[int, int, int, int]:
        return score_hls_candidate(u, height_by_url.get(u, 0))

    cands.sort(key=key, reverse=True)
    best_h = key(cands[0])[0]
    top = [u for u in cands if key(u)[0] == best_h] if best_h > 0 else list(cands)

    for u in top:
        if url_token_style(u) == "validfrom" and is_index_v1_a1(u):
            return u
    for u in top:
        if url_token_style(u) == "validfrom":
            return u
    for u in top:
        if is_index_v1_a1(u):
            return u
    return top[0]


def pick_1080_source(
    urls: list[str],
    height_by_url: dict[str, int] | None = None,
) -> str | None:
    """兼容旧名：实际选择最高可用清晰度。"""
    return pick_best_hls_source(urls, height_by_url)


def _with_cdn_host(url: str, host: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme or "https", host, p.path, p.params, p.query, p.fragment))


def resolve_1080_media_playlist(
    session: BrowserSession,
    source_url: str,
    referer: str,
) -> tuple[str, str]:
    """Return (final_media_playlist_url, body). Expand master if needed.

    优先得到分片为 seg-*-v1-a1.ts?validfrom=...&hash=... 的播放列表；
    若源是 hv-h 的 h/e fMP4，会尝试 ev-h 等同路径以拿到 MPEG-TS。
    """
    attempts: list[str] = [source_url]
    # h/e 或 hv-h：额外尝试 ev-h（常为 mpeg2_ts + 相对 seg-*.ts）
    if url_token_style(source_url) == "h_e" or "hv-h." in (urlparse(source_url).hostname or ""):
        for h in CDN_HOST_FALLBACKS:
            alt = _with_cdn_host(source_url, h)
            if alt not in attempts:
                attempts.append(alt)

    best: tuple[str, str, int, int] | None = None  # url, body, rank, height

    def rank_body(body: str) -> int:
        style = playlist_body_style(body)
        return {
            "validfrom_ts": 100,
            "h_e_ts": 40,
            "ts_other": 30,
            "fmp4": 0,
            "other": 10,
            "empty": -1,
        }.get(style, 0)

    for src in attempts:
        try:
            final, body = http_get_text(
                session, src, referer=referer, for_media=True
            )
        except Exception as e:
            print(f"[warn] resolve GET fail {urlparse(src).hostname}: {e}")
            continue

        candidates_bodies: list[tuple[str, str]] = []
        if "#EXTINF" in body and (
            "index-v1-a1" in final.lower() or playlist_body_style(body) != "empty"
        ):
            # 已是媒体列表
            if "#EXT-X-STREAM-INF" not in body:
                candidates_bodies.append((final, body))
        if "#EXT-X-STREAM-INF" in body or any(
            ".m3u8" in ln and not ln.strip().startswith("#") for ln in body.splitlines()
        ):
            children = child_playlist_urls(final, body)
            # 先试更高清晰度子列表
            children.sort(key=lambda u: score_hls_candidate(u), reverse=True)
            for child in children:
                try:
                    cfinal, cbody = http_get_text(
                        session, child, referer=referer, for_media=True
                    )
                except Exception as e:
                    print(f"[warn] child playlist fail: {e}")
                    continue
                if "#EXTINF" in cbody:
                    candidates_bodies.append((cfinal, cbody))

        for mu, mb in candidates_bodies:
            r = rank_body(mb)
            h = quality_height_from_url(mu)
            style = playlist_body_style(mb)
            print(
                f"[info] 候选媒体列表 style={style} rank={r} height={h or '?'} "
                f"host={urlparse(mu).hostname}"
            )
            if best is None or (r, h) > (best[2], best[3]):
                best = (mu, mb, r, h)
            # 已是优选 validfrom_ts 且当前批里高度最高时即可返回；
            # 同一次 src 内继续扫完以尽量拿到更高清晰度。
            # 若 rank 已满分且高度不低，留到本轮 candidates 结束再定。

        # 本轮 src 结束后，若已有满分 validfrom_ts，直接用当前 best
        if best is not None and best[2] >= 100:
            return best[0], best[1]

    if best and best[2] >= 0:
        if best[2] < 100:
            print(
                f"[warn] 未拿到 validfrom+.ts 播放列表，暂用 style={playlist_body_style(best[1])} "
                f"（可能是 CDN 下发了 h/e 变体）"
            )
        return best[0], best[1]
    raise RuntimeError(f"无法从 {source_url[:120]} 解析出媒体播放列表")


def collect_m3u8_with_heights(html: str) -> tuple[list[str], dict[str, int]]:
    """提取全部 m3u8，并合并 mediaDefinitions.quality / 路径中的清晰度。"""
    height_by: dict[str, int] = {}
    for u, h in extract_media_definition_entries(html):
        height_by[u] = max(height_by.get(u, 0), h or 0)
    urls = extract_m3u8_urls(html)
    for u in urls:
        h = height_by.get(u, 0) or quality_height_from_url(u)
        height_by[u] = h
    return urls, height_by


def collect_m3u8_from_html(html: str) -> list[str]:
    urls, _ = collect_m3u8_with_heights(html)
    return urls


def http_find_1080(
    session: BrowserSession, url: str, *, max_page_tries: int = 5
) -> tuple[str | None, str, list[str], str | None]:
    """从视频页取最高清晰度 HLS 源。页面会在 validfrom / h=e 间轮换，故多次拉取并优先 validfrom。

    Returns: (source_m3u8, referer, notes, title)
    """
    notes: list[str] = []
    best_he: str | None = None
    last_referer = session.referer or url
    page_title: str | None = None

    for attempt in range(1, max_page_tries + 1):
        print(f"[info] HTTP GET ({attempt}/{max_page_tries}): {url}")
        print(f"[info] UA: {UA}")
        print(f"[info] Referer/Origin 基线: {session.referer} | {session.origin}")
        try:
            final, html = http_get_text(
                session, url, try_hosts=False, for_media=False
            )
        except Exception as e:
            notes.append(f"HTTP GET 失败: {e}")
            print(f"[warn] {notes[-1]}")
            continue

        session.update_referer(
            final if "view_video" in final or "/embed/" in final else url
        )
        last_referer = session.referer
        with session._lock:
            names = sorted({c.name for c in session.cj})
        print(
            f"[info] Cookie jar ({len(names)}): {', '.join(names[:12])}"
            + ("..." if len(names) > 12 else "")
        )
        print(f"[info] 落地: {final} (html {len(html)} bytes)")
        if page_title is None:
            page_title = extract_video_title(html)
            if page_title:
                print(f"[info] 标题: {page_title}")
        vk = viewkey_from_url(url)
        path = urlparse(final).path or "/"
        if vk and path.rstrip("/") in ("", "/") and "view_video" not in final and "/embed/" not in final:
            notes.append(f"页面被重定向离开视频（可能不可用）; viewkey={vk}")
            print(f"[warn] {notes[-1]}")

        urls, height_by = collect_m3u8_with_heights(html)
        styles = {}
        heights = {}
        for u in urls:
            styles[url_token_style(u)] = styles.get(url_token_style(u), 0) + 1
            h = height_by.get(u, 0) or quality_height_from_url(u)
            if h:
                heights[h] = heights.get(h, 0) + 1
        print(f"[info] HTML 中发现 {len(urls)} 个 .m3u8 ({styles}); 清晰度={heights or '{}'}")
        picked = pick_1080_source(urls, height_by)
        if picked:
            st = url_token_style(picked)
            qh = height_by.get(picked, 0) or quality_height_from_url(picked)
            print(f"[info] 选中源 style={st} quality={qh or '?'}P: {picked[:140]}")
            if st == "validfrom":
                return picked, session.referer, notes, page_title
            if st == "h_e" and best_he is None:
                best_he = picked
            # 没有 validfrom 则继续换页重试
            notes.append(f"第 {attempt} 次页面只有 {st} 签名，继续重试以获取 validfrom")
            continue

        if vk:
            host = urlparse(final).hostname or "www.pornhub.com"
            if host.startswith("cn."):
                host = "www.pornhub.com"
            embed = f"https://{host}/embed/{vk}"
            print(f"[info] embed 兜底: {embed}")
            try:
                efinal, ehtml = http_get_text(
                    session,
                    embed,
                    referer=f"https://{host}/",
                    try_hosts=False,
                    for_media=False,
                )
            except Exception as e:
                notes.append(f"embed 失败: {e}")
                continue
            low = ehtml.lower()
            if "this video is unavailable" in low or "removedvideo" in low:
                notes.append("embed: 视频不可用（删除/下架/地区）")
                print(f"[fail] {notes[-1]}")
                return None, session.referer, notes, page_title
            if page_title is None:
                page_title = extract_video_title(ehtml)
                if page_title:
                    print(f"[info] 标题(embed): {page_title}")
            urls, height_by = collect_m3u8_with_heights(ehtml)
            picked = pick_1080_source(urls, height_by)
            if picked:
                session.update_referer(efinal)
                qh = height_by.get(picked, 0) or quality_height_from_url(picked)
                print(f"[info] embed 选中 style={url_token_style(picked)} quality={qh or '?'}P")
                if url_token_style(picked) == "validfrom":
                    return picked, session.referer, notes, page_title
                if best_he is None:
                    best_he = picked

    if best_he:
        notes.append("多次重试仍无 validfrom，回退 h/e 源（resolve 时会尽量切到 ev-h TS）")
        print(f"[warn] {notes[-1]}")
        return best_he, last_referer, notes, page_title
    return None, last_referer, notes, page_title


def browser_find_1080(url: str, headed: bool) -> str | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[warn] 未安装 playwright，跳过浏览器兜底")
        return None

    print(f"[info] Playwright 兜底 (headed={headed})")
    found: list[str] = []

    def on_response(response) -> None:
        try:
            rurl = response.url
        except Exception:
            return
        if ".m3u8" not in rurl:
            return
        if is_hls_candidate_url(rurl):
            found.append(rurl)
            print(f"[info] 网络命中: {rurl[:140]}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=UA,
            locale="zh-CN",
            extra_http_headers={
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Sec-Ch-Ua": SEC_CH_UA,
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
            },
            ignore_https_errors=True,
        )
        host = urlparse(url).hostname or "www.pornhub.com"
        root = "." + ".".join(host.split(".")[-2:])
        cookies = []
        for domain in {root, host, ".pornhub.com"}:
            for name, val in AGE_COOKIE_PAIRS:
                cookies.append(
                    {"name": name, "value": val, "domain": domain, "path": "/"}
                )
        try:
            context.add_cookies(cookies)
        except Exception as e:
            print(f"[warn] cookies: {e}")

        page = context.new_page()
        page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"[warn] goto: {e}")
        print(f"[info] browser: {page.url} | {page.title()!r}")

        for sel in DISMISS_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=400):
                    loc.click(force=True, timeout=2000)
                    break
            except Exception:
                pass

        try:
            html = page.content()
            for u in collect_m3u8_from_html(html):
                if is_hls_candidate_url(u):
                    found.append(u)
        except Exception as e:
            print(f"[warn] DOM: {e}")

        try:
            md = page.evaluate(
                """() => {
                    const out = [];
                    for (const k of Object.keys(window)) {
                        if (!k.startsWith('flashvars')) continue;
                        try {
                            const defs = window[k] && window[k].mediaDefinitions;
                            if (!Array.isArray(defs)) continue;
                            for (const d of defs) {
                                if (d && d.videoUrl && String(d.videoUrl).includes('.m3u8'))
                                    out.push(String(d.videoUrl));
                            }
                        } catch (e) {}
                    }
                    return out;
                }"""
            )
            for u in md or []:
                if is_hls_candidate_url(u):
                    found.append(u)
        except Exception as e:
            print(f"[warn] flashvars: {e}")

        if not found:
            for sel in PLAY_SELECTORS:
                try:
                    loc = page.locator(sel).first
                    if loc.count() and loc.is_visible(timeout=400):
                        loc.click(force=True, timeout=2000)
                        break
                except Exception:
                    pass
            try:
                page.evaluate(
                    "() => { const v=document.querySelector('video'); "
                    "if(v){v.muted=true; return v.play().catch(()=>{});} }"
                )
            except Exception:
                pass
            deadline = time.time() + WAIT_SECONDS
            while time.time() < deadline and not found:
                page.wait_for_timeout(500)

        browser.close()

    return pick_1080_source(list(dict.fromkeys(found)))


def download_one_segment(
    session: BrowserSession,
    url: str,
    dest: Path,
    referer: str,
) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    final, data = http_get_bytes(
        session, url, referer=referer, for_media=True
    )
    if not data:
        raise RuntimeError(f"空分片: {final}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return dest


def download_segments(
    session: BrowserSession,
    init_url: str | None,
    seg_urls: list[str],
    referer: str,
) -> list[Path]:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    if init_url:
        init_path = WORK_DIR / "init.mp4"
        print(f"[info] 下载 init map -> {init_path.name}")
        download_one_segment(session, init_url, init_path, referer)
        paths.append(init_path)

    total = len(seg_urls)
    print(f"[info] 下载 {total} 个分片（workers={SEG_WORKERS}）...")
    print(f"[info] CDN 请求头: Referer={referer!r} Origin={site_origin(referer)!r}")

    def job(i_url: tuple[int, str]) -> tuple[int, Path]:
        i, u = i_url
        ext = Path(urlparse(u).path).suffix or ".ts"
        if ext.lower() not in (".ts", ".m4s", ".mp4", ".aac"):
            ext = ".ts"
        dest = WORK_DIR / f"seg_{i:05d}{ext}"
        download_one_segment(session, u, dest, referer)
        return i, dest

    results: dict[int, Path] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=SEG_WORKERS) as ex:
        futs = [ex.submit(job, (i, u)) for i, u in enumerate(seg_urls, 1)]
        done = 0
        for fut in concurrent.futures.as_completed(futs):
            i, path = fut.result()
            results[i] = path
            done += 1
            if done == total or done % 5 == 0 or done <= 3:
                print(
                    f"[info] 分片进度 {done}/{total}: {path.name} "
                    f"({path.stat().st_size} bytes)"
                )

    for i in range(1, total + 1):
        paths.append(results[i])
    return paths


def write_concat_list(seg_paths: list[Path], list_path: Path) -> None:
    lines = []
    for p in seg_paths:
        ap = p.resolve().as_posix().replace("'", r"'\''")
        lines.append(f"file '{ap}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def merge_with_ffmpeg(seg_paths: list[Path], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    write_concat_list(seg_paths, CONCAT_LIST)

    exts = {p.suffix.lower() for p in seg_paths}
    use_binary = ".m4s" in exts or (len(seg_paths) > 1 and seg_paths[0].name == "init.mp4")

    if use_binary:
        merged_ts = OUT_DIR / "merged.bin"
        print(f"[info] 二进制拼接 {len(seg_paths)} 个分片 -> {merged_ts}")
        with merged_ts.open("wb") as out:
            for p in seg_paths:
                out.write(p.read_bytes())
        cmd_copy = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(merged_ts),
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart",
            str(output),
        ]
        cmd_re = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(merged_ts),
            "-c:v", "libx264", "-c:a", "aac",
            "-movflags", "+faststart",
            str(output),
        ]
    else:
        cmd_copy = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(CONCAT_LIST),
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart",
            str(output),
        ]
        cmd_re = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(CONCAT_LIST),
            "-c:v", "libx264", "-c:a", "aac",
            "-movflags", "+faststart",
            str(output),
        ]

    print(f"[info] FFmpeg 合并 (stream copy) -> {output}")
    r = subprocess.run(cmd_copy, capture_output=True, text=True)
    if r.returncode != 0 or not output.exists() or output.stat().st_size == 0:
        print(f"[warn] copy 失败，尝试转码: {(r.stderr or r.stdout)[-400:]}")
        r2 = subprocess.run(cmd_re, capture_output=True, text=True)
        if r2.returncode != 0 or not output.exists() or output.stat().st_size == 0:
            print(f"[fail] FFmpeg 转码也失败:\n{r2.stderr or r2.stdout}")
            raise SystemExit(1)
        print("[ok] 转码合并成功")
    else:
        print("[ok] stream copy 合并成功")


def ffprobe_duration(path: Path) -> str | None:
    if not shutil.which("ffprobe"):
        return None
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        sec = float(r.stdout.strip())
        m, s = divmod(sec, 60)
        h, m = divmod(int(m), 60)
        if h:
            return f"{h:d}:{m:02d}:{s:06.3f} ({sec:.3f}s)"
        return f"{int(m):d}:{s:06.3f} ({sec:.3f}s)"
    except Exception as e:
        print(f"[warn] ffprobe: {e}")
        return None


def main() -> int:
    url, headed, use_browser, output = parse_args(sys.argv)
    require_ffmpeg()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    session = BrowserSession(url)
    print(f"[info] 目标: {url}")
    print(f"[info] 输出: {output.resolve()}")

    source, referer, notes, _title = http_find_1080(session, url)
    if not source and use_browser:
        if any("不可用" in n for n in notes):
            print("[info] 跳过浏览器（视频不可用）")
        else:
            source = browser_find_1080(url, headed)
            referer = referer or session.referer or url

    if not source:
        for n in notes:
            print(f"[note] {n}")
        print(
            "[fail] 未找到可用 m3u8。常见原因：视频下架、地区限制、页面无 HLS 源。"
            "可试 --headed，或开启「流量经本机转发」。"
        )
        return 1

    print(f"[info] 选中源: {source}")
    media_url, body = resolve_1080_media_playlist(session, source, referer)
    qh = quality_height_from_url(media_url)
    if not is_index_v1_a1(media_url):
        print(f"[warn] 媒体列表路径不含 index-v1-a1: {media_url}")
    print(f"[info] 媒体列表 quality={qh or '?'}P: {media_url}")
    PLAYLIST_PATH.write_text(body, encoding="utf-8")
    print(f"[ok] 已保存播放列表 -> {PLAYLIST_PATH.resolve()}")

    init_url, seg_urls = parse_media_segments(media_url, body)
    if not seg_urls:
        print("[fail] 播放列表中没有分片")
        return 1
    print(f"[info] 分片数: {len(seg_urls)}" + (f" + init" if init_url else ""))

    seg_paths = download_segments(session, init_url, seg_urls, referer)
    merge_with_ffmpeg(seg_paths, output)

    size = output.stat().st_size
    dur = ffprobe_duration(output)
    print(f"[success] 视频已生成: {output.resolve()}")
    print(f"[success] 大小: {size} bytes ({size / (1024 * 1024):.2f} MiB)")
    if dur:
        print(f"[success] 时长: {dur}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
