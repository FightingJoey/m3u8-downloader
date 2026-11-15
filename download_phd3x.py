#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pornhd3x.tv（含 wwwN.pornhd3x.tv）影片页取链。

从页面提取 episode id，按播放器逻辑设置 token Cookie 并请求
/ajax/get_sources/{id}/{md5}，拿到最高清晰度 m3u8。该站部分分片是
「PNG 头 + 填充 + MPEG-TS」包装，剥壳逻辑见 strip_png_wrapped_ts。

仅标准库；代理走 download_ph_1080 已有的环境变量处理。
"""

from __future__ import annotations

import hashlib
import html as htmlmod
import http.cookiejar
import json
import random
import re
import sys
import threading
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin, urlparse

from download_ph_1080 import (  # noqa: E402
    SEC_CH_UA,
    UA,
    _make_cookie,
    env_proxies,
    maybe_decompress,
)

# fix.js 中 KqSa 字符串 index 60（播放器签名/Cookie 名素材）
SECRET = (
    "n1sqcua67bcq9826avrbi6m49vd7shxkn985mhodk06twz87wwxtp3dqiicks2df"
    "yud213k6ygiomq01s94e4tr9v0k887bkyud213k6ygiomq01s94e4tr9v0k887bk"
    "qocxzw39esdyfhvtkpzq9n4e7at4kc6k8sxom08bl4dukp16h09oplu7zov4m5f8"
)
SIG_SALT = "98126avrbi6m49vd7shxkn985"
PNG_SIG = b"\x89PNG\r\n\x1a\n"
TS_PACKET = 188


def is_phd3x_page(url: str) -> bool:
    """Host 为 pornhd3x.tv / wwwN.pornhd3x.tv，或该域下的 /movies/ 页。"""
    if not url:
        return False
    try:
        p = urlparse(url.strip())
    except Exception:
        return "pornhd3x.tv" in url.lower()
    host = (p.hostname or "").lower()
    if host == "pornhd3x.tv" or host.endswith(".pornhd3x.tv"):
        return True
    # 容错：主机写成别的镜像但路径是影片页，且 URL 里仍带站点名
    path = (p.path or "").lower()
    if "/movies/" in path and "pornhd3x" in (url.lower() + host):
        return True
    return False


def site_origin(url: str) -> str:
    p = urlparse(url)
    scheme = p.scheme or "https"
    netloc = p.netloc or (p.hostname or "")
    if not netloc:
        return "https://www.pornhd3x.tv"
    return f"{scheme}://{netloc}"


def cookie_name_for(episode_id: str) -> str:
    return SECRET[13:37] + episode_id + SECRET[40:64]


def sources_signature(episode_id: str, token: str) -> str:
    raw = f"{episode_id}{token}{SIG_SALT}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def sources_url(origin: str, episode_id: str, token: str) -> str:
    origin = origin.rstrip("/")
    sig = sources_signature(episode_id, token)
    return f"{origin}/ajax/get_sources/{episode_id}/{sig}?count=1&mobile=false"


def random_token(n: int = 6) -> str:
    """接近 Math.random().toString(36).substring(2, 8)：6 位 0-9a-z。"""
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    return "".join(random.choice(alphabet) for _ in range(n))


def _find_png_iend_end(data: bytes) -> int | None:
    """Walk PNG chunks; return offset immediately after IEND chunk."""
    if not data.startswith(PNG_SIG):
        return None
    pos = 8
    n = len(data)
    while pos + 12 <= n:
        length = int.from_bytes(data[pos : pos + 4], "big")
        typ = data[pos + 4 : pos + 8]
        end = pos + 12 + length
        if length < 0 or end > n:
            return None
        if typ == b"IEND":
            return end
        pos = end
    return None


def is_mpegts_at(data: bytes, offset: int, *, min_packets: int = 2) -> bool:
    """True if data[offset:] is 188-byte TS packets starting with 0x47."""
    if offset < 0 or offset >= len(data):
        return False
    remaining = len(data) - offset
    if remaining < TS_PACKET or data[offset] != 0x47:
        return False
    n = remaining // TS_PACKET
    if n < 1:
        return False
    for i in range(n):
        if data[offset + i * TS_PACKET] != 0x47:
            return False
    # Prefer a clean multiple; allow a short tail only if we already saw enough packets
    tail = remaining % TS_PACKET
    if tail and n < min_packets:
        return False
    return n >= min_packets or tail == 0


def _find_ts_offset(data: bytes, start: int) -> int | None:
    pos = start
    n = len(data)
    while pos < n:
        i = data.find(0x47, pos)
        if i < 0:
            return None
        if is_mpegts_at(data, i):
            return i
        pos = i + 1
    return None


def strip_png_wrapped_ts(data: bytes) -> tuple[bytes, bool]:
    """若正文是 PNG 包装的 MPEG-TS，剥掉 PNG/填充，返回裸 TS。

    返回 (payload, stripped)。已经是裸 TS / fMP4 / 其它数据则原样返回，
    stripped=False。
    """
    if not data:
        return data, False
    if data.startswith(PNG_SIG):
        after = _find_png_iend_end(data)
        start = after if after is not None else 8
        # 常见：IEND 后先有 0xFF 填充，再跟 188 对齐的 0x47 包
        off = _find_ts_offset(data, start)
        if off is not None:
            return data[off:], True
        return data, False
    if data[0] == 0x47 and is_mpegts_at(data, 0, min_packets=1):
        return data, False
    return data, False


def _clean_title(raw: str) -> str:
    text = htmlmod.unescape(raw or "").strip()
    text = re.sub(r"\s+", " ", text)
    for sep in (" | ", " - ", " — ", " – "):
        lower = text.lower()
        for tail in ("pornhd3x", "pornhd3x.tv", "pornhd 3x"):
            needle = (sep + tail).lower()
            if lower.endswith(needle):
                text = text[: -len(sep + tail)].strip()
                lower = text.lower()
    return text.strip()


def extract_episode_id(html: str) -> str | None:
    if not html:
        return None
    patterns = [
        r'<input[^>]*\bid=["\']uuid["\'][^>]*\bvalue=["\']([^"\']+)',
        r'<input[^>]*\bvalue=["\']([^"\']+)["\'][^>]*\bid=["\']uuid["\']',
        r'var\s+movie\s*=\s*\{[^{}]*?\bid\s*:\s*["\']([^"\']+)',
        r'["\']id["\']\s*:\s*["\']([A-Z0-9]{10,})["\']',
        r'data-episode-id=["\']([^"\']+)',
        r'episode-id=["\']([^"\']+)',
        r'data-first-ep=["\']([^"\']+)',
        r'first-ep[^>]*\b(?:data-id|data-episode|value)=["\']([^"\']+)',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.I | re.S)
        if m:
            eid = m.group(1).strip()
            if eid:
                return eid
    return None


def extract_title(html: str) -> str | None:
    if not html:
        return None
    m = re.search(r'var\s+movie\s*=\s*\{[^{}]*?\bname\s*:\s*["\']([^"\']+)', html, re.I | re.S)
    if m:
        title = _clean_title(m.group(1))
        if title:
            return title
    m = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        html,
        re.I,
    )
    if not m:
        m = re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
            html,
            re.I,
        )
    if m:
        title = _clean_title(m.group(1))
        if title:
            return title
    m = re.search(r"<title[^>]*>([^<]+)", html, re.I)
    if m:
        title = _clean_title(m.group(1))
        if title:
            return title
    return None


def label_height(label: Any) -> int:
    m = re.search(r"(\d+)", str(label or ""))
    return int(m.group(1)) if m else 0


def _is_m3u8_source(file: str, typ: str) -> bool:
    f = (file or "").lower()
    t = (typ or "").lower()
    if "m3u8" in f:
        return True
    return t in {
        "hls",
        "m3u8",
        "application/x-mpegurl",
        "application/vnd.apple.mpegurl",
        "application/vnd.apple.mpegurl; charset=utf-8",
    }


def pick_best_source(payload: dict) -> tuple[str, int, str]:
    """从 get_sources JSON 里选最高清晰度，优先 m3u8。返回 (url, height, label)。"""
    candidates: list[tuple[int, int, str, str]] = []
    for pl in payload.get("playlist") or []:
        if not isinstance(pl, dict):
            continue
        for src in pl.get("sources") or []:
            if not isinstance(src, dict):
                continue
            file = str(src.get("file") or "").strip()
            if not file:
                continue
            label = str(src.get("label") or src.get("quality") or "")
            typ = str(src.get("type") or "")
            height = label_height(label) or label_height(src.get("quality"))
            prefer = 1 if _is_m3u8_source(file, typ) else 0
            candidates.append((prefer, height, file, label or (f"{height}p" if height else typ)))
    if not candidates:
        raise ValueError("get_sources 未返回可用播放地址")
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    prefer, height, file, label = candidates[0]
    if not prefer:
        raise ValueError("get_sources 没有 m3u8，仅有非 HLS 源，已跳过")
    return file, height, label


def count_media_segments(body: str) -> int:
    n = 0
    for ln in (body or "").splitlines():
        s = ln.strip()
        if s and not s.startswith("#") and ".m3u8" not in s.lower():
            n += 1
    return n


class Phd3xSession:
    """Cookie jar + 浏览器头；页面/接口带 Referer，播放列表按站点要求不带 Referer。"""

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

    def set_token_cookie(self, episode_id: str, token: str, host: str | None = None) -> str:
        host = host or (urlparse(self.page_url).hostname or "")
        name = cookie_name_for(episode_id)
        # 任务要求：cookie 设在当前 host、path=/
        self.cj.set_cookie(_make_cookie(name, token, host, path="/", secure=True))
        return name

    def page_headers(self) -> dict[str, str]:
        return {
            "User-Agent": UA,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8,"
                "application/signed-exchange;v=b3;q=0.7"
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

    def ajax_headers(self) -> dict[str, str]:
        return {
            "User-Agent": UA,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.page_url,
            "Origin": self.origin,
            "Sec-Ch-Ua": SEC_CH_UA,
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Connection": "keep-alive",
        }

    def media_headers(self, *, omit_referer: bool = True) -> dict[str, str]:
        hdrs = {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "identity",
            "Sec-Ch-Ua": SEC_CH_UA,
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site",
            "Connection": "keep-alive",
        }
        # 部分 m3u8（如 u8lmahqjne.m3u8）播放器以 referrer=no-referrer 拉列表
        if not omit_referer:
            hdrs["Referer"] = self.page_url
            hdrs["Origin"] = self.origin
        return hdrs

    def get_bytes(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: int = 60,
    ) -> tuple[str, bytes]:
        hdrs = dict(headers or self.media_headers(omit_referer=True))
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                data = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                data = maybe_decompress(data, enc)
                return resp.geturl(), data
        except urllib.error.HTTPError as e:
            # HTTPError 也是响应，尽量读出 body 便于上层报错
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
        final, raw = self.get_bytes(url, headers=headers, timeout=timeout)
        return final, raw.decode("utf-8", "replace")


def fetch_page(session: Phd3xSession, page_url: str, *, tries: int = 3) -> tuple[str, str]:
    last: Exception | None = None
    for i in range(1, tries + 1):
        print(f"[info] pornhd3x 拉页 ({i}/{tries}): {page_url}")
        try:
            final, html = session.get_text(page_url, headers=session.page_headers())
            session.page_url = final
            session.origin = site_origin(final)
            return final, html
        except Exception as e:
            last = e
            print(f"[warn] pornhd3x 拉页失败: {e}")
    assert last is not None
    raise last


def fetch_sources(
    session: Phd3xSession,
    episode_id: str,
    *,
    token: str | None = None,
    tries: int = 3,
) -> tuple[dict, str, str]:
    """设置 token cookie 并请求 get_sources。返回 (json, token, sources_url)。"""
    token = token or random_token()
    host = urlparse(session.page_url).hostname or urlparse(session.origin).hostname or ""
    session.set_token_cookie(episode_id, token, host)
    url = sources_url(session.origin, episode_id, token)
    last: Exception | None = None
    for i in range(1, tries + 1):
        print(f"[info] pornhd3x get_sources ({i}/{tries}): {url}")
        try:
            _final, text = session.get_text(url, headers=session.ajax_headers())
            text = text.strip()
            if not text:
                raise ValueError("get_sources 空响应")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as e:
                raise ValueError(f"get_sources 不是 JSON: {text[:180]!r}") from e
            if not isinstance(payload, dict):
                raise ValueError(f"get_sources 响应类型异常: {type(payload)}")
            return payload, token, url
        except Exception as e:
            last = e
            print(f"[warn] get_sources 失败: {e}")
    assert last is not None
    raise last


def fetch_media_playlist(session: Phd3xSession, media_url: str) -> tuple[str, str]:
    """拉媒体播放列表：不带 Referer。"""
    final, body = session.get_text(media_url, headers=session.media_headers(omit_referer=True))
    if "#EXTM3U" not in body:
        raise ValueError(f"播放列表不是 m3u8（开头: {body[:80]!r}）")
    # 若误拿到 master，展开第一条媒体列表
    if re.search(r"#EXT-X-STREAM-INF", body):
        base = media_url.rsplit("/", 1)[0] + "/"
        child = None
        for raw in body.splitlines():
            s = raw.strip()
            if s and not s.startswith("#"):
                child = s if s.startswith("http") else urljoin(base if base.endswith("/") else base + "/", s)
                break
        if not child:
            raise ValueError("master 播放列表里没有子列表")
        print(f"[info] pornhd3x master → media: {child}")
        return fetch_media_playlist(session, child)
    return final, body


def resolve_phd3x(page_url: str, *, token: str | None = None) -> dict[str, Any]:
    """取链入口。成功时含 session / media_url / body / title / quality 等。"""
    page_url = (page_url or "").strip()
    notes: list[str] = []
    if not page_url.startswith("http"):
        return {"ok": False, "error": "URL 必须以 http/https 开头", "notes": notes}
    if not is_phd3x_page(page_url):
        return {"ok": False, "error": "不是 pornhd3x.tv 影片页", "notes": notes}

    session = Phd3xSession(page_url)
    try:
        final_page, html = fetch_page(session, page_url)
    except Exception as e:
        return {"ok": False, "error": f"打开影片页失败: {e}", "notes": notes}

    episode_id = extract_episode_id(html)
    title = extract_title(html)
    if not episode_id:
        return {
            "ok": False,
            "error": "页面中未找到影片 id（uuid / movie.id / episode-id）",
            "title": title,
            "notes": notes,
        }
    notes.append(f"episode_id={episode_id}")

    try:
        payload, token_used, src_url = fetch_sources(session, episode_id, token=token)
    except Exception as e:
        return {
            "ok": False,
            "error": f"get_sources 失败: {e}",
            "title": title,
            "episode_id": episode_id,
            "notes": notes,
        }

    try:
        media_url, quality, label = pick_best_source(payload)
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "title": title,
            "episode_id": episode_id,
            "notes": notes,
        }
    notes.append(f"quality={label or quality}")

    if not media_url.startswith("http"):
        media_url = urljoin(session.origin + "/", media_url)

    try:
        final_media, body = fetch_media_playlist(session, media_url)
    except Exception as e:
        return {
            "ok": False,
            "error": f"拉取 m3u8 失败: {e}",
            "title": title,
            "episode_id": episode_id,
            "media_url": media_url,
            "quality": quality or None,
            "notes": notes,
        }

    return {
        "ok": True,
        "session": session,
        "page_url": final_page,
        "episode_id": episode_id,
        "title": title,
        "media_url": final_media,
        "source": media_url,
        "body": body,
        "quality": quality or None,
        "quality_label": label,
        "segment_count": count_media_segments(body),
        "token": token_used,
        "sources_url": src_url,
        "notes": notes,
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print("用法: python3 download_phd3x.py <pornhd3x 影片页 URL>")
        return 0
    result = resolve_phd3x(args[0])
    show = {k: v for k, v in result.items() if k not in {"session", "body"}}
    if result.get("ok"):
        show["body_preview"] = (result.get("body") or "")[:400]
    print(json.dumps(show, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
