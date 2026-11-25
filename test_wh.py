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
    _is_safe_wh_hostname,
    extract_title,
    extract_video_id,
    is_wh_page,
    resolve_wh,
)


class TestWhSSRFProtection(unittest.TestCase):
    """SSRF 防护测试（复用 fpv 逻辑）。"""

    def test_reject_localhost(self):
        self.assertFalse(_is_safe_wh_hostname("localhost"))
        self.assertFalse(_is_safe_wh_hostname("localhost.localdomain"))

    def test_reject_loopback_ipv4(self):
        self.assertFalse(_is_safe_wh_hostname("127.0.0.1"))
        self.assertFalse(_is_safe_wh_hostname("127.0.0.2"))

    def test_reject_loopback_ipv6(self):
        self.assertFalse(_is_safe_wh_hostname("::1"))

    def test_reject_private_ipv4(self):
        self.assertFalse(_is_safe_wh_hostname("192.168.1.1"))
        self.assertFalse(_is_safe_wh_hostname("10.0.0.1"))
        self.assertFalse(_is_safe_wh_hostname("172.16.0.1"))

    def test_accept_wh_domains(self):
        self.assertTrue(_is_safe_wh_hostname("whoreshub.com"))
        self.assertTrue(_is_safe_wh_hostname("www.whoreshub.com"))
        self.assertTrue(_is_safe_wh_hostname("en.whoreshub.com"))

    def test_reject_unrelated_domains(self):
        self.assertFalse(_is_safe_wh_hostname("example.com"))
        self.assertFalse(_is_safe_wh_hostname("evil.com"))


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
    """占位符功能测试（待实现）。"""

    def test_resolve_wh_not_implemented(self):
        """当前应返回"待实现"错误。"""
        result = resolve_wh("https://www.whoreshub.com/videos/730394/title/")
        self.assertFalse(result.get("ok"))
        self.assertIn("待实现", result.get("error", ""))
        self.assertEqual(result.get("video_id"), "730394")


def run():
    unittest.main(argv=["test_wh.py"], exit=False, verbosity=2)


if __name__ == "__main__":
    run()
