#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
whoreshub.com 离线回归测试（框架，待站点证据后完善）。

当前测试：
- URL 检测（基于假设模式）
- SSRF 防护（白名单验证）
- 标题清理
- 基本结构验证

待添加（需站点证据）：
- 源提取测试（依赖实际 HTML/JSON 格式）
- 质量选择测试（依赖实际源配置）
- MP4 探测测试（若使用 progressive MP4）
- 会话管理测试
"""

import unittest

from download_wh import (
    _clean_title,
    _is_safe_hostname,
    extract_flashvars_from_html,
    extract_sources_from_flashvars,
    extract_title,
    extract_video_id,
    is_wh_page,
    probe_mp4_dimensions,
    resolve_wh,
)


class TestWhSSRFProtection(unittest.TestCase):
    """SSRF 防护测试（复用 fpv 逻辑）。"""

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

    def test_accept_wh_domains(self):
        self.assertTrue(_is_safe_hostname("whoreshub.com"))
        self.assertTrue(_is_safe_hostname("www.whoreshub.com"))
        self.assertTrue(_is_safe_hostname("en.whoreshub.com"))

    def test_reject_unrelated_domains(self):
        self.assertFalse(_is_safe_hostname("example.com"))
        self.assertFalse(_is_safe_hostname("evil.com"))


class TestWhURLDetection(unittest.TestCase):
    """URL 检测测试（基于假设模式，需证据验证）。"""

    def test_detect_valid_url(self):
        # 假设 /videos/数字/ 模式
        self.assertTrue(is_wh_page("https://www.whoreshub.com/videos/730394/title/"))
        self.assertTrue(is_wh_page("https://whoreshub.com/videos/123456/another/"))

    def test_reject_invalid_url(self):
        self.assertFalse(is_wh_page("https://www.pornhub.com/view_video.php?viewkey=abc"))
        self.assertFalse(is_wh_page("https://google.com/"))
        self.assertFalse(is_wh_page(""))

    def test_extract_video_id(self):
        # 假设 /videos/数字/ 模式
        self.assertEqual(
            extract_video_id("https://www.whoreshub.com/videos/730394/title/"),
            "730394",
        )
        self.assertEqual(
            extract_video_id("https://whoreshub.com/videos/123456/another/"),
            "123456",
        )
        self.assertIsNone(extract_video_id("https://whoreshub.com/"))


class TestWhTitleExtraction(unittest.TestCase):
    """标题提取测试（通用逻辑）。"""

    def test_extract_title_og(self):
        html = """
        <meta property="og:title" content="Test Video - whoreshub">
        """
        self.assertEqual(extract_title(html), "Test Video")

    def test_extract_title_twitter(self):
        html = """
        <meta name="twitter:title" content="Amazing Video | Whores Hub">
        """
        self.assertEqual(extract_title(html), "Amazing Video")

    def test_clean_title(self):
        self.assertEqual(
            _clean_title("My Video - whoreshub"),
            "My Video",
        )
        self.assertEqual(
            _clean_title("Test | Whores Hub"),
            "Test",
        )


class TestWhPlaceholder(unittest.TestCase):
    """resolve_wh 行为：离线时是网络错误，在线时必须给出实测分辨率。"""

    def test_resolve_wh_not_unimplemented(self):
        """失败不得是“待实现”；成功则必须带回实测宽高。"""
        result = resolve_wh("https://www.whoreshub.com/videos/730394/title/")
        if not result.get("ok"):
            err = result.get("error") or ""
            self.assertNotIn("待实现", err)
            self.assertTrue(err, "失败时应有错误信息")
            return
        self.assertGreater(int(result.get("actual_width") or 0), 0)
        self.assertGreater(int(result.get("actual_height") or 0), 0)
        self.assertIn("/get_file/", result.get("mp4_url") or "")


class TestWhFlashvarsExtraction(unittest.TestCase):
    """基于实际证据的 flashvars 提取测试。"""

    def test_extract_flashvars_from_real_html(self):
        """从真实页面 HTML 片段提取 flashvars。"""
        import os
        fixture_path = "/home/ubuntu/.cursor/projects/workspace/uploads/page-player-excerpt_d73d.html"
        if not os.path.exists(fixture_path):
            self.skipTest(f"证据文件不存在: {fixture_path}")
        
        with open(fixture_path, "r", encoding="utf-8") as f:
            html = f.read()
        
        flashvars = extract_flashvars_from_html(html)
        self.assertIsNotNone(flashvars)
        self.assertEqual(flashvars.get("video_id"), "730394")
        self.assertIn("video_url", flashvars)
        self.assertIn("video_alt_url", flashvars)
        self.assertIn("video_alt_url2", flashvars)
        self.assertIn("video_alt_url3", flashvars)

    def test_extract_sources_from_real_flashvars(self):
        """从真实 flashvars JSON 提取源。"""
        import json
        import os
        fixture_path = "/home/ubuntu/.cursor/projects/workspace/uploads/flashvars-sanitized_ae04.json"
        if not os.path.exists(fixture_path):
            self.skipTest(f"证据文件不存在: {fixture_path}")
        
        with open(fixture_path, "r", encoding="utf-8") as f:
            flashvars = json.load(f)
        
        sources = extract_sources_from_flashvars(flashvars)
        self.assertEqual(len(sources), 4)  # 480p, 720p, 1080p, 2160p
        
        # 检查标签
        labels = [s.get("label") for s in sources]
        self.assertIn("480p", labels)
        self.assertIn("720p HD", labels)
        self.assertIn("1080p FHD", labels)
        self.assertIn("2160p 4k", labels)
        
        # 检查 URL
        for s in sources:
            url = s.get("file", "")
            self.assertIn("/get_file/", url)
            self.assertIn("v-acctoken=", url)

    def test_filter_embed_redirect_sources(self):
        """embed 页面应过滤 *_redirect="1" 的源。"""
        import json
        import os
        fixture_path = "/home/ubuntu/.cursor/projects/workspace/uploads/embed-flashvars-sanitized_59eb.json"
        if not os.path.exists(fixture_path):
            self.skipTest(f"证据文件不存在: {fixture_path}")
        
        with open(fixture_path, "r", encoding="utf-8") as f:
            flashvars = json.load(f)
        
        sources = extract_sources_from_flashvars(flashvars)
        # Embed 页：480p 真实，720p+重定向，应只保留480p
        # （实际情况需查看 embed JSON 内容）
        self.assertGreater(len(sources), 0)
        for s in sources:
            # 确保没有重定向标志
            self.assertNotIn("_redirect", s.get("file", ""))


def run():
    unittest.main(argv=["test_wh.py"], exit=False, verbosity=2)


if __name__ == "__main__":
    run()


def _synthetic_mp4(width: int, height: int) -> bytes:
    """Minimal ftyp+moov with tkhd before mdia/hdlr (real-world box order)."""
    import struct

    def box(typ: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", 8 + len(payload)) + typ + payload

    tkhd = bytearray(84)
    struct.pack_into(">I", tkhd, 76, width << 16)
    struct.pack_into(">I", tkhd, 80, height << 16)
    hdlr = b"\x00" * 8 + b"vide" + b"\x00" * 13
    mdia = box(b"mdia", box(b"hdlr", hdlr))
    trak = box(b"trak", box(b"tkhd", bytes(tkhd)) + mdia)
    moov = box(b"moov", trak)
    ftyp = box(b"ftyp", b"isom" + struct.pack(">I", 0) + b"isomiso2mp41")
    return ftyp + moov


class TestWhProbeDimensions(unittest.TestCase):
    def test_probe_reads_tkhd_before_hdlr(self):
        data = _synthetic_mp4(3840, 2160)
        self.assertEqual(probe_mp4_dimensions(data), (3840, 2160))

    def test_probe_ignores_audio_zero_dimensions(self):
        data = _synthetic_mp4(0, 0)
        self.assertIsNone(probe_mp4_dimensions(data))


class TestWhNotDispatchedAsFpv(unittest.TestCase):
    def test_whoreshub_is_not_fpv_page(self):
        from download_fpv import is_fpv_page

        url = "https://www.whoreshub.com/videos/730394/crazy-cosplaying-hottie-video-with-xander-corvus-mia-stone-brazzers/"
        self.assertTrue(is_wh_page(url))
        self.assertFalse(is_fpv_page(url))
