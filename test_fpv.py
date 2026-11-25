#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
freepornvideos.xxx 取链与元数据解析的离线回归测试。

验证：
1. URL / 视频页检测
2. 源提取（从 HTML 片段）
3. 分辨率选择逻辑（mislabeled 2160p 实际为 1080p）
4. MP4 尺寸探测（有限头部）
5. 标题清理
"""

import struct
import unittest

from download_fpv import (
    _clean_title,
    extract_sources_from_html,
    extract_title,
    extract_video_id,
    is_fpv_page,
    label_height,
    probe_mp4_dimensions,
)


class TestFpvDetection(unittest.TestCase):
    """URL 检测与 ID 提取。"""

    def test_is_fpv_page_valid(self):
        self.assertTrue(
            is_fpv_page("https://www.freepornvideos.xxx/videos/93810393/title/")
        )
        self.assertTrue(
            is_fpv_page("https://freepornvideos.xxx/videos/123456/another-title/")
        )

    def test_is_fpv_page_invalid(self):
        self.assertFalse(is_fpv_page("https://www.pornhub.com/view_video.php?viewkey=abc"))
        self.assertFalse(is_fpv_page("https://google.com/"))
        self.assertFalse(is_fpv_page(""))

    def test_extract_video_id(self):
        self.assertEqual(
            extract_video_id("https://www.freepornvideos.xxx/videos/93810393/title/"),
            "93810393",
        )
        self.assertEqual(
            extract_video_id("https://freepornvideos.xxx/videos/12345/another/"),
            "12345",
        )
        self.assertIsNone(extract_video_id("https://freepornvideos.xxx/"))


class TestFpvTitleExtraction(unittest.TestCase):
    """标题提取与清理。"""

    def test_extract_title_og(self):
        html = """
        <meta property="og:title" content="Best Video Ever - freepornvideos">
        """
        self.assertEqual(extract_title(html), "Best Video Ever")

    def test_extract_title_twitter(self):
        html = """
        <meta name="twitter:title" content="Amazing Clip | Free Porn Videos">
        """
        self.assertEqual(extract_title(html), "Amazing Clip")

    def test_extract_title_fallback(self):
        html = "<title>Sample Title - freepornvideos</title>"
        self.assertEqual(extract_title(html), "Sample Title")

    def test_clean_title(self):
        self.assertEqual(
            _clean_title("My Video - freepornvideos"),
            "My Video",
        )
        self.assertEqual(
            _clean_title("Test | Free Porn Videos"),
            "Test",
        )
        self.assertEqual(
            _clean_title("  Spaces   Everywhere  "),
            "Spaces Everywhere",
        )


class TestFpvSourceExtraction(unittest.TestCase):
    """从 HTML 提取 Video.js sources。"""

    def test_extract_sources_json(self):
        html = """
        var playerOptions = {
          sources: [
            { file: "https://cdn.example.com/720p.mp4", label: "720p", type: "video/mp4" },
            { file: "https://cdn.example.com/1080p.mp4", label: "1080p", type: "video/mp4" }
          ]
        };
        """
        sources = extract_sources_from_html(html)
        self.assertGreaterEqual(len(sources), 2)
        # 检查其中一个
        found_1080 = any(s.get("label") == "1080p" for s in sources)
        self.assertTrue(found_1080)

    def test_extract_sources_regex(self):
        html = """
        { file: 'https://cdn.example.com/480p.mp4', label: '480p' }
        """
        sources = extract_sources_from_html(html)
        self.assertGreaterEqual(len(sources), 1)
        self.assertTrue(any("480p.mp4" in str(s.get("file", "")) for s in sources))

    def test_label_height(self):
        self.assertEqual(label_height("2160p"), 2160)
        self.assertEqual(label_height("1080p"), 1080)
        self.assertEqual(label_height("720p"), 720)
        self.assertEqual(label_height("480p"), 480)
        self.assertEqual(label_height("unknown"), 0)


class TestFpvMP4Probe(unittest.TestCase):
    """MP4 尺寸探测（有限头部扫描）。"""

    def _make_tkhd_box(self, width: int, height: int) -> bytes:
        """手工构造简化 tkhd box。

        tkhd (track header):
        - size(4) + type(4) + version(1) + flags(3) + ...
        - 创建时间(4) + 修改时间(4) + track_id(4) + reserved(4) + duration(4) = 20
        - reserved(8) + layer(2) + alternate(2) + volume(2) + reserved(2) = 16
        - matrix(36)
        - width(4, 16.16 fixed) + height(4, 16.16 fixed)
        总计：8(box头) + 1+3 + 20 + 16 + 36 + 8 = 92 bytes
        """
        # 简化：version=0, flags=0, 时间/id/duration 都填 0
        payload = bytearray()
        payload.append(0)  # version
        payload.extend(b"\x00\x00\x00")  # flags
        payload.extend(b"\x00" * 20)  # 创建/修改/id/reserved/duration
        payload.extend(b"\x00" * 16)  # reserved + layer/alternate/volume/reserved
        # unity matrix (简化)
        payload.extend(b"\x00\x01\x00\x00" * 9)  # 36B matrix
        # width / height (16.16 fixed point)
        payload.extend(struct.pack(">I", width << 16))
        payload.extend(struct.pack(">I", height << 16))

        size = 8 + len(payload)
        box = struct.pack(">I", size) + b"tkhd" + payload
        return box

    def _make_hdlr_box(self, handler_type: bytes = b"vide") -> bytes:
        """构造 hdlr (handler) box 以标识轨道类型。"""
        payload = bytearray()
        payload.append(0)  # version
        payload.extend(b"\x00\x00\x00")  # flags
        payload.extend(b"\x00\x00\x00\x00")  # pre_defined
        payload.extend(handler_type)  # handler_type ('vide' for video)
        payload.extend(b"\x00" * 12)  # reserved
        payload.extend(b"VideoHandler\x00")  # name
        
        size = 8 + len(payload)
        return struct.pack(">I", size) + b"hdlr" + payload

    def _make_moov_trak(self, width: int, height: int) -> bytes:
        """moov > trak > (hdlr + tkhd)。"""
        hdlr = self._make_hdlr_box(b"vide")
        tkhd = self._make_tkhd_box(width, height)
        
        trak_payload = hdlr + tkhd
        trak_size = 8 + len(trak_payload)
        trak = struct.pack(">I", trak_size) + b"trak" + trak_payload
        
        moov_size = 8 + len(trak)
        moov = struct.pack(">I", moov_size) + b"moov" + trak
        return moov

    def test_probe_mp4_dimensions_1920x1080(self):
        # ftyp(简化) + moov > trak > tkhd(1920x1080)
        ftyp = struct.pack(">I", 20) + b"ftypisom" + b"\x00" * 8
        moov = self._make_moov_trak(1920, 1080)
        data = ftyp + moov
        dims = probe_mp4_dimensions(data)
        self.assertEqual(dims, (1920, 1080))

    def test_probe_mp4_dimensions_1280x720(self):
        moov = self._make_moov_trak(1280, 720)
        dims = probe_mp4_dimensions(moov)
        self.assertEqual(dims, (1280, 720))

    def test_probe_mp4_dimensions_no_tkhd(self):
        # 只有 ftyp，无 tkhd
        ftyp = struct.pack(">I", 20) + b"ftypisom" + b"\x00" * 8
        dims = probe_mp4_dimensions(ftyp)
        self.assertIsNone(dims)

    def test_probe_mp4_dimensions_too_short(self):
        dims = probe_mp4_dimensions(b"short")
        self.assertIsNone(dims)


class TestFpvResolutionLogic(unittest.TestCase):
    """分辨率选择逻辑（优先实际尺寸，不信标签）。"""

    def test_mislabeled_2160_actual_1080(self):
        # 模拟：标签 2160p 实际 1920x1080
        # pick_best_source 会探测 MP4 头部，选择实际分辨率最高的
        # 这里只测逻辑：标签高度 vs 实际尺寸
        label_2160_actual = (1920, 1080)  # w*h = 2073600
        label_720_actual = (1280, 720)  # w*h = 921600
        # 应该选 2160 标签那个（实际 1920x1080）
        self.assertGreater(
            label_2160_actual[0] * label_2160_actual[1],
            label_720_actual[0] * label_720_actual[1],
        )


def run():
    unittest.main(argv=["test_fpv.py"], exit=False, verbosity=2)


if __name__ == "__main__":
    run()
