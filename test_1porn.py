#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
1porn.tv 适配器单元测试

基于实际证据文件的测试套件
"""

import unittest
import sys
import os
import struct

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(__file__))

from download_1porn import (
    _is_safe_hostname,
    _validate_url_safety,
    _extract_sources_from_html,
    _extract_title_from_html,
    _extract_language_prefix,
    is_1porn_page,
    probe_mp4_dimensions
)


class TestSSRFProtection(unittest.TestCase):
    """SSRF 保护测试"""
    
    def test_accept_1porn_tv(self):
        """接受 1porn.tv 主域名"""
        self.assertTrue(_is_safe_hostname("1porn.tv"))
        self.assertTrue(_is_safe_hostname("www.1porn.tv"))
    
    def test_accept_1porn_tv_subdomains(self):
        """接受 1porn.tv 子域名"""
        self.assertTrue(_is_safe_hostname("img.1porn.tv"))
    
    def test_accept_fpvcdn_com(self):
        """接受 fpvcdn.com (与 hdporn 共享 CDN)"""
        self.assertTrue(_is_safe_hostname("fpvcdn.com"))
    
    def test_accept_fpvcdn_subdomains(self):
        """接受 fpvcdn.com 动态节点"""
        self.assertTrue(_is_safe_hostname("d923341.fpvcdn.com"))
        self.assertTrue(_is_safe_hostname("d494211.fpvcdn.com"))
    
    def test_reject_localhost(self):
        """拒绝 localhost"""
        self.assertFalse(_is_safe_hostname("localhost"))
    
    def test_reject_loopback_ipv4(self):
        """拒绝回环 IPv4"""
        self.assertFalse(_is_safe_hostname("127.0.0.1"))
        self.assertFalse(_is_safe_hostname("127.0.0.100"))
    
    def test_reject_private_ipv4_192(self):
        """拒绝私有 IPv4 (192.168.x.x)"""
        self.assertFalse(_is_safe_hostname("192.168.1.1"))
        self.assertFalse(_is_safe_hostname("192.168.100.50"))
    
    def test_reject_private_ipv4_10(self):
        """拒绝私有 IPv4 (10.x.x.x)"""
        self.assertFalse(_is_safe_hostname("10.0.0.1"))
        self.assertFalse(_is_safe_hostname("10.255.255.255"))
    
    def test_reject_private_ipv4_172(self):
        """拒绝私有 IPv4 (172.16-31.x.x)"""
        self.assertFalse(_is_safe_hostname("172.16.0.1"))
        self.assertFalse(_is_safe_hostname("172.31.255.255"))
    
    def test_reject_link_local_ipv4(self):
        """拒绝链路本地 IPv4"""
        self.assertFalse(_is_safe_hostname("169.254.1.1"))
    
    def test_reject_loopback_ipv6(self):
        """拒绝回环 IPv6"""
        self.assertFalse(_is_safe_hostname("::1"))
    
    def test_reject_link_local_ipv6(self):
        """拒绝链路本地 IPv6"""
        self.assertFalse(_is_safe_hostname("fe80::1"))
    
    def test_reject_multicast_ipv4(self):
        """拒绝组播 IPv4"""
        self.assertFalse(_is_safe_hostname("224.0.0.1"))
    
    def test_reject_other_domains(self):
        """拒绝其他域名"""
        self.assertFalse(_is_safe_hostname("evil.com"))
        self.assertFalse(_is_safe_hostname("1porn.tv.evil.com"))
    
    def test_validate_url_https_only(self):
        """只接受 HTTPS"""
        self.assertTrue(_validate_url_safety("https://www.1porn.tv/videos/test/"))
        self.assertFalse(_validate_url_safety("http://www.1porn.tv/videos/test/"))
        self.assertFalse(_validate_url_safety("ftp://www.1porn.tv/file"))


class TestHTMLParsing(unittest.TestCase):
    """HTML 解析测试 (基于实际证据)"""
    
    def setUp(self):
        """加载实际 HTML fixture"""
        fixture_path = "/home/ubuntu/.cursor/projects/workspace/uploads/watch-video-block_fe4f.html"
        if os.path.exists(fixture_path):
            with open(fixture_path, "r", encoding="utf-8") as f:
                self.real_html = f.read()
        else:
            # 回退到嵌入式样本 (从证据文件提取)
            self.real_html = '''<video id='video-1795573399' class='video-js' playsinline controls preload='none' poster='https://img.1porn.tv/93788000/93788402/medium@2x/1.jpg' data-setup='{}'>
<source src='https://www.1porn.tv/zh/get_file/8512/REDACTED_SIG/93788000/93788402/93788402_2160m.mp4/' type='video/mp4' label="2160p">
<source src='https://www.1porn.tv/zh/get_file/8512/REDACTED_SIG/93788000/93788402/93788402_720m.mp4/' type='video/mp4' label="720p" selected="true">
<source src='https://www.1porn.tv/zh/get_file/8512/REDACTED_SIG/93788000/93788402/93788402_480m.mp4/' type='video/mp4' label="480p">
</video>'''
    
    def test_extract_sources_from_real_html(self):
        """从实际 HTML 提取 sources"""
        sources = _extract_sources_from_html(self.real_html)
        
        # 应该找到 3 个源
        self.assertEqual(len(sources), 3)
        
        # 按标签高度降序
        self.assertEqual(sources[0]["label"], "2160p")
        self.assertEqual(sources[0]["label_height"], 2160)
        self.assertEqual(sources[1]["label"], "720p")
        self.assertEqual(sources[1]["label_height"], 720)
        self.assertEqual(sources[2]["label"], "480p")
        self.assertEqual(sources[2]["label_height"], 480)
        
        # 检查 URL 包含 get_file
        for src in sources:
            self.assertIn("get_file", src["url"])
            self.assertIn(".mp4", src["url"])
    
    def test_extract_sources_with_language_prefix(self):
        """sources 包含语言前缀 /zh/"""
        sources = _extract_sources_from_html(self.real_html)
        
        for src in sources:
            self.assertIn("/zh/get_file/", src["url"])
    
    def test_extract_title(self):
        """提取标题"""
        html_with_title = '''
        <html>
        <head>
            <title>Big Dick Big Problems - 1porn.tv</title>
        </head>
        </html>
        '''
        title = _extract_title_from_html(html_with_title)
        self.assertEqual(title, "Big Dick Big Problems")
    
    def test_extract_title_removes_site_suffix(self):
        """移除站点名称后缀"""
        html = "<title>Test Video - www.1porn.tv</title>"
        title = _extract_title_from_html(html)
        self.assertEqual(title, "Test Video")
    
    def test_extract_language_prefix_zh(self):
        """提取中文前缀"""
        url = "https://www.1porn.tv/zh/videos/big-dick-big-problems-margo-von-teese-gets-shaken/"
        prefix = _extract_language_prefix(url)
        self.assertEqual(prefix, "/zh")
    
    def test_extract_language_prefix_none(self):
        """无语言前缀"""
        url = "https://www.1porn.tv/videos/big-dick-big-problems-margo-von-teese-gets-shaken/"
        prefix = _extract_language_prefix(url)
        self.assertEqual(prefix, "")
    
    def test_extract_language_prefix_other_langs(self):
        """其他语言前缀"""
        for lang in ["pt", "es", "fr", "de", "ja", "ko"]:
            url = f"https://www.1porn.tv/{lang}/videos/test/"
            prefix = _extract_language_prefix(url)
            self.assertEqual(prefix, f"/{lang}")


class TestURLDetection(unittest.TestCase):
    """URL 检测测试"""
    
    def test_detect_1porn_zh(self):
        """检测中文页"""
        url = "https://www.1porn.tv/zh/videos/big-dick-big-problems-margo-von-teese-gets-shaken/"
        self.assertTrue(is_1porn_page(url))
    
    def test_detect_1porn_en(self):
        """检测英文页"""
        url = "https://www.1porn.tv/videos/big-dick-big-problems-margo-von-teese-gets-shaken/"
        self.assertTrue(is_1porn_page(url))
    
    def test_detect_1porn_other_lang(self):
        """检测其他语言"""
        url = "https://www.1porn.tv/ja/videos/test/"
        self.assertTrue(is_1porn_page(url))
    
    def test_reject_embed(self):
        """embed 页不是主页面"""
        url = "https://www.1porn.tv/zh/embed/93788402"
        self.assertFalse(is_1porn_page(url))
    
    def test_reject_other_sites(self):
        """拒绝其他站点"""
        self.assertFalse(is_1porn_page("https://www.pornhub.com/view_video.php?viewkey=123"))
        self.assertFalse(is_1porn_page("https://www.freepornvideos.xxx/videos/123/test/"))
        self.assertFalse(is_1porn_page("https://www.whoreshub.com/videos/123/test/"))


class TestMP4Probe(unittest.TestCase):
    """MP4 探测测试 (复用 fpv/hdp 测试模式)"""
    
    def _make_box(self, box_type: bytes, payload: bytes) -> bytes:
        """构造 MP4 盒子"""
        size = 8 + len(payload)
        return struct.pack(">I", size) + box_type + payload
    
    def _make_ftyp_box(self) -> bytes:
        """构造 ftyp 盒子"""
        payload = b"isom" + struct.pack(">I", 512) + b"isomiso2avc1mp41"
        return self._make_box(b"ftyp", payload)
    
    def _make_hdlr_box(self, handler_type: bytes = b"vide") -> bytes:
        """构造 hdlr 盒子"""
        payload = (
            b"\x00" * 8 +  # version + flags + pre_defined
            handler_type +  # handler_type (vide/soun)
            b"\x00" * 12 +  # reserved
            b"VideoHandler\x00"
        )
        return self._make_box(b"hdlr", payload)
    
    def _make_tkhd_box(self, width: int, height: int, version: int = 0) -> bytes:
        """构造 tkhd 盒子"""
        if version == 0:
            # Version 0: 32位时间戳
            payload = (
                bytes([version]) + b"\x00\x00\x00" +  # version + flags
                struct.pack(">I", 0) +  # creation_time
                struct.pack(">I", 0) +  # modification_time
                struct.pack(">I", 1) +  # track_ID
                struct.pack(">I", 0) +  # reserved
                struct.pack(">I", 1000) +  # duration
                b"\x00" * 8 +  # reserved
                struct.pack(">h", 0) +  # layer
                struct.pack(">h", 0) +  # alternate_group
                struct.pack(">h", 0x0100) +  # volume (1.0)
                struct.pack(">h", 0) +  # reserved
                (b"\x00\x01\x00\x00" + b"\x00" * 12 + b"\x00\x01\x00\x00" + b"\x00" * 12 + b"\x40\x00\x00\x00") +  # matrix
                struct.pack(">I", width << 16) +  # width (16.16 fixed)
                struct.pack(">I", height << 16)   # height (16.16 fixed)
            )
        else:
            # Version 1: 64位时间戳
            payload = (
                bytes([version]) + b"\x00\x00\x00" +  # version + flags
                struct.pack(">Q", 0) +  # creation_time
                struct.pack(">Q", 0) +  # modification_time
                struct.pack(">I", 1) +  # track_ID
                struct.pack(">I", 0) +  # reserved
                struct.pack(">Q", 1000) +  # duration
                b"\x00" * 8 +  # reserved
                struct.pack(">h", 0) +  # layer
                struct.pack(">h", 0) +  # alternate_group
                struct.pack(">h", 0x0100) +  # volume (1.0)
                struct.pack(">h", 0) +  # reserved
                (b"\x00\x01\x00\x00" + b"\x00" * 12 + b"\x00\x01\x00\x00" + b"\x00" * 12 + b"\x40\x00\x00\x00") +  # matrix
                struct.pack(">I", width << 16) +  # width (16.16 fixed)
                struct.pack(">I", height << 16)   # height (16.16 fixed)
            )
        
        return self._make_box(b"tkhd", payload)
    
    def _make_moov_trak(self, width: int, height: int, tkhd_version: int = 0) -> bytes:
        """构造 moov/trak 结构"""
        hdlr = self._make_hdlr_box(b"vide")
        tkhd = self._make_tkhd_box(width, height, tkhd_version)
        trak = self._make_box(b"trak", hdlr + tkhd)
        moov = self._make_box(b"moov", trak)
        return moov
    
    def test_probe_1920x1080(self):
        """探测 1920×1080 (1porn 2160p 标签实际尺寸)"""
        import struct
        ftyp = self._make_ftyp_box()
        moov = self._make_moov_trak(1920, 1080)
        data = ftyp + moov
        
        dims = probe_mp4_dimensions(data)
        self.assertIsNotNone(dims)
        self.assertEqual(dims, (1920, 1080))
    
    def test_probe_1280x720(self):
        """探测 1280×720"""
        import struct
        ftyp = self._make_ftyp_box()
        moov = self._make_moov_trak(1280, 720)
        data = ftyp + moov
        
        dims = probe_mp4_dimensions(data)
        self.assertIsNotNone(dims)
        self.assertEqual(dims, (1280, 720))
    
    def test_probe_852x480(self):
        """探测 852×480 (1porn 480p)"""
        import struct
        ftyp = self._make_ftyp_box()
        moov = self._make_moov_trak(852, 480)
        data = ftyp + moov
        
        dims = probe_mp4_dimensions(data)
        self.assertIsNotNone(dims)
        self.assertEqual(dims, (852, 480))
    
    def test_probe_audio_track_ignored(self):
        """忽略音频轨"""
        import struct
        ftyp = self._make_ftyp_box()
        
        # 音频轨
        hdlr_audio = self._make_hdlr_box(b"soun")
        tkhd_audio = self._make_tkhd_box(0, 0)
        trak_audio = self._make_box(b"trak", hdlr_audio + tkhd_audio)
        
        # 视频轨
        hdlr_video = self._make_hdlr_box(b"vide")
        tkhd_video = self._make_tkhd_box(1920, 1080)
        trak_video = self._make_box(b"trak", hdlr_video + tkhd_video)
        
        moov = self._make_box(b"moov", trak_audio + trak_video)
        data = ftyp + moov
        
        dims = probe_mp4_dimensions(data)
        self.assertIsNotNone(dims)
        self.assertEqual(dims, (1920, 1080))  # 只返回视频轨
    
    def test_probe_version1_tkhd(self):
        """支持 tkhd version 1 (64位时间戳)"""
        import struct
        ftyp = self._make_ftyp_box()
        moov = self._make_moov_trak(1280, 720, tkhd_version=1)
        data = ftyp + moov
        
        dims = probe_mp4_dimensions(data)
        self.assertIsNotNone(dims)
        self.assertEqual(dims, (1280, 720))


if __name__ == "__main__":
    # 添加 struct 导入 (测试需要)
    import struct
    
    unittest.main(verbosity=2)
