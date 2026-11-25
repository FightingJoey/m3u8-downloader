#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hdporn.gg 离线回归测试（基于实际证据）。

测试内容：
- URL 检测
- SSRF 防护
- 标题清理
- <source> 提取
- 实际 fixture 解析
"""

import json
import os
import struct
import unittest

from download_hdp import (
    _clean_title,
    _is_safe_hostname,
    extract_sources_from_html,
    extract_title,
    extract_video_id,
    is_hdp_page,
    label_height,
    probe_mp4_dimensions,
    resolve_hdp,
)


class TestHdpSSRFProtection(unittest.TestCase):
    """SSRF 防护测试。"""

    def test_reject_localhost(self):
        self.assertFalse(_is_safe_hostname("localhost"))
        self.assertFalse(_is_safe_hostname("localhost.localdomain"))

    def test_reject_loopback_ipv4(self):
        self.assertFalse(_is_safe_hostname("127.0.0.1"))
        self.assertFalse(_is_safe_hostname("127.0.0.2"))

    def test_reject_loopback_ipv6(self):
        self.assertFalse(_is_safe_hostname("::1"))

    def test_reject_private_ipv4(self):
        self.assertFalse(_is_safe_hostname("192.168.1.1"))
        self.assertFalse(_is_safe_hostname("10.0.0.1"))
        self.assertFalse(_is_safe_hostname("172.16.0.1"))

    def test_accept_hdp_domains(self):
        self.assertTrue(_is_safe_hostname("hdporn.gg"))
        self.assertTrue(_is_safe_hostname("www.hdporn.gg"))

    def test_accept_cdn_domains(self):
        self.assertTrue(_is_safe_hostname("fpvcdn.com"))
        self.assertTrue(_is_safe_hostname("d923341.fpvcdn.com"))
        self.assertTrue(_is_safe_hostname("ahcdn.com"))
        self.assertTrue(_is_safe_hostname("ip10.ahcdn.com"))

    def test_reject_unrelated_domains(self):
        self.assertFalse(_is_safe_hostname("example.com"))
        self.assertFalse(_is_safe_hostname("evil.com"))


class TestHdpURLDetection(unittest.TestCase):
    """URL 检测测试。"""

    def test_detect_valid_url(self):
        self.assertTrue(
            is_hdp_page("https://www.hdporn.gg/videos/blonde-hottie-not-found-but-brunette-hottie-found/")
        )
        self.assertTrue(is_hdp_page("https://hdporn.gg/videos/sample/"))

    def test_reject_invalid_url(self):
        self.assertFalse(is_hdp_page("https://example.com/videos/123/"))
        self.assertFalse(is_hdp_page("https://www.hdporn.gg/"))
        self.assertFalse(is_hdp_page(""))

    def test_extract_video_id(self):
        # 从 embed 提取
        self.assertEqual(extract_video_id("https://www.hdporn.gg/embed/93690758"), "93690758")
        # 从 get_file 提取
        self.assertEqual(
            extract_video_id("https://www.hdporn.gg/get_file/N/HASH/93690000/93690758/93690758_720m.mp4/"),
            "93690758"
        )


class TestHdpTitleExtraction(unittest.TestCase):
    """标题提取测试。"""

    def test_extract_title_og(self):
        html = '<meta property="og:title" content="Test Video - HDPorn" />'
        self.assertEqual(extract_title(html), "Test Video")

    def test_extract_title_twitter(self):
        html = '<meta name="twitter:title" content="Amazing Video | HD Porn" />'
        self.assertEqual(extract_title(html), "Amazing Video")

    def test_clean_title(self):
        self.assertEqual(
            _clean_title("My Video - hdporn"),
            "My Video"
        )
        self.assertEqual(
            _clean_title("Test | HD Porn"),
            "Test"
        )


class TestHdpSourceExtraction(unittest.TestCase):
    """<source> 提取测试。"""

    def test_extract_sources_from_fixture(self):
        """使用真实 fixture 测试源提取。"""
        fixture_path = "/home/ubuntu/.cursor/projects/workspace/uploads/page-player-excerpt_b886.html"
        if not os.path.exists(fixture_path):
            self.skipTest(f"证据文件不存在: {fixture_path}")
        
        with open(fixture_path, "r", encoding="utf-8") as f:
            html = f.read()
        
        sources = extract_sources_from_html(html)
        self.assertGreaterEqual(len(sources), 2)  # 至少 720p + 480p
        
        # 检查是否有 720p
        labels = [s.get("label") for s in sources]
        self.assertIn("720p", labels)
        
        # 检查 selected
        selected = [s for s in sources if s.get("selected")]
        self.assertGreater(len(selected), 0)  # 至少有一个 selected

    def test_label_height(self):
        self.assertEqual(label_height("720p"), 720)
        self.assertEqual(label_height("480p"), 480)
        self.assertEqual(label_height("1080p"), 1080)


class TestHdpMP4Probe(unittest.TestCase):
    """MP4 探测测试（复用 FPV 逻辑）。"""

    def _make_ftyp_box(self) -> bytes:
        """构造 ftyp box。"""
        return struct.pack(">I", 32) + b"ftyp" + b"isom" + struct.pack(">I", 512) + b"isomiso2avc1mp41" * 1

    def _make_hdlr_box(self, handler_type: bytes = b"vide") -> bytes:
        """构造 hdlr box。"""
        version_flags = struct.pack(">I", 0)
        pre_defined = struct.pack(">I", 0)
        handler = handler_type
        reserved = b"\x00" * 12
        name = b"VideoHandler\x00"
        payload = version_flags + pre_defined + handler + reserved + name
        size = 8 + len(payload)
        return struct.pack(">I", size) + b"hdlr" + payload

    def _make_tkhd_box(self, width: int, height: int, version: int = 0) -> bytes:
        """构造 tkhd box。"""
        if version == 0:
            version_flags = struct.pack(">I", 0)
            creation_time = struct.pack(">I", 0)
            modification_time = struct.pack(">I", 0)
            track_id = struct.pack(">I", 1)
            reserved1 = struct.pack(">I", 0)
            duration = struct.pack(">I", 100)
        else:
            version_flags = struct.pack(">I", 0x01000000)
            creation_time = struct.pack(">Q", 0)
            modification_time = struct.pack(">Q", 0)
            track_id = struct.pack(">I", 1)
            reserved1 = struct.pack(">I", 0)
            duration = struct.pack(">Q", 100)
        
        reserved2 = struct.pack(">I", 0) + struct.pack(">I", 0)
        layer = struct.pack(">h", 0)
        alternate_group = struct.pack(">h", 0)
        volume = struct.pack(">h", 0)
        reserved3 = struct.pack(">h", 0)
        matrix = struct.pack(">9I", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
        width_fixed = struct.pack(">I", width << 16)
        height_fixed = struct.pack(">I", height << 16)
        
        payload = (version_flags + creation_time + modification_time + track_id + 
                   reserved1 + duration + reserved2 + layer + alternate_group + 
                   volume + reserved3 + matrix + width_fixed + height_fixed)
        size = 8 + len(payload)
        return struct.pack(">I", size) + b"tkhd" + payload

    def _make_moov_trak(self, width: int, height: int, version: int = 0) -> bytes:
        """构造包含 trak/hdlr/tkhd 的 moov box。"""
        hdlr = self._make_hdlr_box(b"vide")
        tkhd = self._make_tkhd_box(width, height, version)
        trak_payload = hdlr + tkhd
        trak_size = 8 + len(trak_payload)
        trak = struct.pack(">I", trak_size) + b"trak" + trak_payload
        moov_payload = trak
        moov_size = 8 + len(moov_payload)
        return struct.pack(">I", moov_size) + b"moov" + moov_payload

    def test_probe_mp4_dimensions_1280x720(self):
        """测试 1280x720 探测（样本分辨率）。"""
        import struct
        ftyp = self._make_ftyp_box()
        moov = self._make_moov_trak(1280, 720)
        data = ftyp + moov
        result = probe_mp4_dimensions(data)
        self.assertEqual(result, (1280, 720))


class TestHdpPlaceholder(unittest.TestCase):
    """真实网络测试（无网络访问时预期失败）。"""

    def test_resolve_hdp_no_network(self):
        """无网络访问时应返回网络错误。"""
        result = resolve_hdp("https://www.hdporn.gg/videos/blonde-hottie-not-found-but-brunette-hottie-found/")
        self.assertFalse(result.get("ok"))
        # 预期网络错误
        self.assertIn("失败", result.get("error", ""))


def run():
    unittest.main(argv=["test_hdp.py"], exit=False, verbosity=2)


if __name__ == "__main__":
    unittest.main()
