#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke: pick_best_hls_source / pick_1080_source chooses highest height."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from download_ph_1080 import (
    collect_m3u8_with_heights,
    pick_1080_source,
    pick_best_hls_source,
    quality_height_from_field,
    quality_height_from_url,
)


def u(height: int, name: str = "index-v1-a1.m3u8", style: str = "validfrom") -> str:
    if style == "validfrom":
        q = "validfrom=1&hash=abc"
    else:
        q = "h=xyz&e=999"
    return (
        f"https://ev-h.phncdn.com/hls/videos/202401/{height}P_2000K_12345/{name}?{q}"
    )


def assert_eq(got, expect, msg: str) -> None:
    if got != expect:
        raise AssertionError(f"{msg}: got={got!r} expect={expect!r}")


def test_height_helpers() -> None:
    assert_eq(quality_height_from_url(u(720)), 720, "url 720")
    assert_eq(quality_height_from_url(u(1080)), 1080, "url 1080")
    assert_eq(quality_height_from_field("480"), 480, "field str")
    assert_eq(quality_height_from_field(720), 720, "field int")
    assert_eq(quality_height_from_field(["240", "480"]), 480, "field list max")


def test_pick_720_over_480() -> None:
    urls = [u(480), u(720)]
    picked = pick_best_hls_source(urls)
    assert_eq(quality_height_from_url(picked), 720, "480+720 -> 720")


def test_pick_1080_over_lower() -> None:
    urls = [u(480), u(720), u(1080)]
    picked = pick_1080_source(urls)
    assert_eq(quality_height_from_url(picked), 1080, "480+720+1080 -> 1080")


def test_pick_only_480() -> None:
    urls = [u(480, style="h_e"), u(480, style="validfrom")]
    picked = pick_best_hls_source(urls)
    assert_eq(quality_height_from_url(picked), 480, "only 480")
    assert "validfrom=" in picked, "prefer validfrom within 480"


def test_media_definitions_html() -> None:
    html = (
        '<script>var flashvars_123 = {"mediaDefinitions": ['
        '{"quality": "480", "videoUrl": "https://ev-h.phncdn.com/hls/videos/202401/480P_1000K_1/index-v1-a1.m3u8?validfrom=1&hash=a"},'
        '{"quality": "720", "videoUrl": "https://ev-h.phncdn.com/hls/videos/202401/720P_2000K_1/index-v1-a1.m3u8?validfrom=1&hash=b"},'
        '{"quality": "1080", "videoUrl": "https://hv-h.phncdn.com/hls/videos/202401/1080P_4000K_1/master.m3u8?h=x&e=1"}'
        "]};</script>"
    )
    html480_720 = (
        '<script>var flashvars_1 = {"mediaDefinitions": ['
        '{"quality": 480, "videoUrl": "https://ev-h.phncdn.com/hls/videos/x/480P_1000K/index-v1-a1.m3u8?validfrom=1&hash=a"},'
        '{"quality": "720", "videoUrl": "https://ev-h.phncdn.com/hls/videos/x/720P_2000K/index-v1-a1.m3u8?validfrom=1&hash=b"}'
        "]};</script>"
    )
    urls2, hb2 = collect_m3u8_with_heights(html480_720)
    picked = pick_1080_source(urls2, hb2)
    assert_eq(hb2.get(picked) or quality_height_from_url(picked), 720, "html 480+720 -> 720")

    html_only_480 = (
        '{"mediaDefinitions": ['
        '{"quality": "480", "videoUrl": "https://ev-h.phncdn.com/hls/videos/x/480P_1000K/index-v1-a1.m3u8?validfrom=1&hash=a"}'
        "]}"
    )
    urls3, hb3 = collect_m3u8_with_heights(html_only_480)
    picked3 = pick_1080_source(urls3, hb3)
    assert_eq(quality_height_from_url(picked3), 480, "html only 480 -> 480")

    urls4, hb4 = collect_m3u8_with_heights(html)
    picked4 = pick_1080_source(urls4, hb4)
    assert_eq(hb4.get(picked4) or quality_height_from_url(picked4), 1080, "html with 1080 -> 1080")


def main() -> int:
    test_height_helpers()
    test_pick_720_over_480()
    test_pick_1080_over_lower()
    test_pick_only_480()
    test_media_definitions_html()
    print("OK: all pick-quality smoke tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
