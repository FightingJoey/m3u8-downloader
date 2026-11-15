#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke: pornhd3x cookie/md5 构造 + PNG 包装 TS 剥壳。"""
from __future__ import annotations

import hashlib
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from download_phd3x import (
    SECRET,
    SIG_SALT,
    cookie_name_for,
    extract_episode_id,
    extract_title,
    is_phd3x_page,
    label_height,
    pick_best_source,
    sources_signature,
    sources_url,
    strip_png_wrapped_ts,
)


def assert_eq(got, expect, msg: str) -> None:
    if got != expect:
        raise AssertionError(f"{msg}: got={got!r} expect={expect!r}")


def assert_true(cond, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _png_chunk(typ: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(typ + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", crc)


def _minimal_png() -> bytes:
    # 合法小 PNG：IHDR 500x500 + 一小段 IDAT + IEND（接近站点包装形态）
    ihdr = struct.pack(">IIBBBBB", 500, 500, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00" * 16)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def _ts_packets(n: int = 4) -> bytes:
    out = bytearray()
    for i in range(n):
        pkt = bytearray(188)
        pkt[0] = 0x47
        pkt[1] = (i + 1) & 0xFF
        pkt[2] = 0x00
        out.extend(pkt)
    return bytes(out)


def test_strip_png_wrapped_ts() -> None:
    png = _minimal_png()
    padding = b"\xff" * 40
    packets = _ts_packets(5)
    blob = png + padding + packets
    out, stripped = strip_png_wrapped_ts(blob)
    assert_true(stripped, "PNG 包装应被识别并剥掉")
    assert_eq(out, packets, "剥壳后应为连续 188 字节 TS")
    assert_true(all(out[i] == 0x47 for i in range(0, len(out), 188)), "每包同步字节 0x47")

    raw = _ts_packets(3)
    out2, stripped2 = strip_png_wrapped_ts(raw)
    assert_true(not stripped2, "裸 TS 不应标记为 stripped")
    assert_eq(out2, raw, "裸 TS 原样通过")

    fmp4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 24
    out3, stripped3 = strip_png_wrapped_ts(fmp4)
    assert_true(not stripped3, "fMP4 不应被剥壳")
    assert_eq(out3, fmp4, "fMP4 原样通过")

    empty, st_empty = strip_png_wrapped_ts(b"")
    assert_eq(empty, b"", "空输入")
    assert_true(not st_empty, "空输入 stripped=False")


def test_cookie_and_signature() -> None:
    eid = "SKX2FSCAFENMEHJX"
    token = "abc123"  # 固定 token，避免随机
    expect_name = SECRET[13:37] + eid + SECRET[40:64]
    assert_eq(cookie_name_for(eid), expect_name, "cookie 名 = secret[13:37]+id+secret[40:64]")
    assert_true(expect_name.startswith("826avrbi6m49vd7shxkn985m"), "cookie 名前缀")
    assert_true(expect_name.endswith("k06twz87wwxtp3dqiicks2df"), "cookie 名后缀")
    assert_true(eid in expect_name, "cookie 名含 episode id")

    expect_sig = hashlib.md5((eid + token + SIG_SALT).encode("utf-8")).hexdigest()
    assert_eq(sources_signature(eid, token), expect_sig, "md5(id+token+salt)")
    assert_eq(len(expect_sig), 32, "md5 hex 长度")

    origin = "https://www9.pornhd3x.tv"
    url = sources_url(origin, eid, token)
    assert_eq(
        url,
        f"{origin}/ajax/get_sources/{eid}/{expect_sig}?count=1&mobile=false",
        "get_sources URL",
    )


def test_is_phd3x_page() -> None:
    assert_true(is_phd3x_page("https://www9.pornhd3x.tv/movies/foo"), "www9")
    assert_true(is_phd3x_page("https://www.pornhd3x.tv/movies/foo"), "www")
    assert_true(is_phd3x_page("https://pornhd3x.tv/movies/foo"), "apex")
    assert_true(is_phd3x_page("https://www12.pornhd3x.tv/movies/bar?x=1"), "wwwN")
    assert_true(not is_phd3x_page("https://cn.pornhub.com/view_video.php?viewkey=abc"), "not PH")
    assert_true(not is_phd3x_page("https://example.com/movies/foo"), "other /movies/")


def test_extract_id_and_title() -> None:
    html = """
    <html><head>
      <meta property="og:title" content="Double DTF | Pornhd3x">
      <title>Double DTF | Pornhd3x</title>
    </head><body>
      <input type="hidden" id="uuid" value="SKX2FSCAFENMEHJX">
      <script>var movie={id:'SKX2FSCAFENMEHJX',name:'Brazzers Angela White Double DTF'};</script>
    </body></html>
    """
    assert_eq(extract_episode_id(html), "SKX2FSCAFENMEHJX", "input#uuid")
    assert_eq(extract_title(html), "Brazzers Angela White Double DTF", "movie.name 优先")

    html2 = '<input value="ABCDEFGHIJKLMNOP" id="uuid" type="hidden">'
    assert_eq(extract_episode_id(html2), "ABCDEFGHIJKLMNOP", "uuid 属性顺序互换")


def test_pick_highest_m3u8() -> None:
    payload = {
        "playlist": [
            {
                "image": "x.jpg",
                "sources": [
                    {"file": "https://cdn.example/a.mp4", "type": "mp4", "label": "1080p"},
                    {"file": "https://cdn.example/b.m3u8?md5=1", "type": "hls", "label": "480p"},
                    {"file": "https://cdn.example/c.m3u8?md5=2", "type": "hls", "label": "720p", "default": True},
                ],
            }
        ]
    }
    url, height, label = pick_best_source(payload)
    assert_eq(height, 720, "多档时选最高 m3u8（忽略更高的 mp4）")
    assert_eq(url, "https://cdn.example/c.m3u8?md5=2", "选 720p m3u8")
    assert_eq(label_height(label), 720, "label 高度")


def main() -> int:
    test_strip_png_wrapped_ts()
    test_cookie_and_signature()
    test_is_phd3x_page()
    test_extract_id_and_title()
    test_pick_highest_m3u8()
    print("OK: all pornhd3x smoke tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
