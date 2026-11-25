#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fullpornxxx.net 适配器测试套件

基于实际证据文件的离线测试
"""

import unittest
import sys
import os
import struct
import base64
import json

sys.path.insert(0, os.path.dirname(__file__))

from download_fpx import (
    _is_safe_hostname,
    _validate_url_safety,
    _extract_generate_mp4_call,
    _extract_title_from_html,
    _extract_video_id_from_url,
    decrypt_session_key,
    parse_ok_sources,
    is_fpx_page,
    probe_mp4_dimensions,
    QUALITY_LABELS
)


class TestSSRFProtection(unittest.TestCase):
    """SSRF 保护测试"""
    
    def test_accept_fullpornxxx_net(self):
        """接受 fullpornxxx.net"""
        self.assertTrue(_is_safe_hostname("fullpornxxx.net"))
        self.assertTrue(_is_safe_hostname("www.fullpornxxx.net"))
    
    def test_accept_ok_ru(self):
        """接受 api.ok.ru"""
        self.assertTrue(_is_safe_hostname("api.ok.ru"))
    
    def test_accept_okcdn_ru(self):
        """接受 okcdn.ru CDN"""
        self.assertTrue(_is_safe_hostname("okcdn.ru"))
        self.assertTrue(_is_safe_hostname("vd750.okcdn.ru"))
    
    def test_reject_localhost(self):
        """拒绝 localhost"""
        self.assertFalse(_is_safe_hostname("localhost"))
    
    def test_reject_loopback_ipv4(self):
        """拒绝回环 IPv4"""
        self.assertFalse(_is_safe_hostname("127.0.0.1"))
    
    def test_reject_private_ipv4(self):
        """拒绝私有 IPv4"""
        self.assertFalse(_is_safe_hostname("192.168.1.1"))
        self.assertFalse(_is_safe_hostname("10.0.0.1"))
    
    def test_reject_other_domains(self):
        """拒绝其他域名"""
        self.assertFalse(_is_safe_hostname("evil.com"))
    
    def test_validate_url_https_only(self):
        """只接受 HTTPS"""
        self.assertTrue(_validate_url_safety("https://www.fullpornxxx.net/video/123/"))
        self.assertFalse(_validate_url_safety("http://www.fullpornxxx.net/video/123/"))


class TestHTMLParsing(unittest.TestCase):
    """HTML 解析测试"""
    
    def test_extract_generate_mp4_call(self):
        """提取 generate_mp4 调用"""
        html = "generate_mp4('BLOB', 'PASS', '8563402746241', '35975');"
        result = _extract_generate_mp4_call(html)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "BLOB")
        self.assertEqual(result[1], "PASS")
        self.assertEqual(result[2], "8563402746241")
        self.assertEqual(result[3], "35975")
    
    def test_extract_title(self):
        """提取标题"""
        html = "<title>Test Video - fullpornxxx.net</title>"
        title = _extract_title_from_html(html)
        self.assertEqual(title, "Test Video")
    
    def test_extract_video_id(self):
        """提取 video_id"""
        url = "https://www.fullpornxxx.net/video/35975/gimme-all-2_v2/"
        video_id = _extract_video_id_from_url(url)
        self.assertEqual(video_id, "35975")


class TestCrypto(unittest.TestCase):
    """加密解密测试"""
    
    def test_decrypt_session_key_synthetic(self):
        """测试解密 (合成数据)"""
        # 创建合成的加密数据
        from Crypto.Cipher import AES
        from Crypto.Protocol.KDF import PBKDF2
        from Crypto.Hash import SHA512
        import os
        
        plaintext = "test_session_key_12345"
        passphrase = "test_pass"
        iterations = 999
        
        salt = os.urandom(32)
        iv = os.urandom(16)
        
        key = PBKDF2(
            passphrase.encode("utf-8"),
            salt,
            dkLen=32,
            count=iterations,
            hmac_hash_module=SHA512
        )
        
        # PKCS7 padding
        padding_len = 16 - (len(plaintext) % 16)
        padded = plaintext.encode("utf-8") + bytes([padding_len] * padding_len)
        
        cipher = AES.new(key, AES.MODE_CBC, iv)
        ciphertext = cipher.encrypt(padded)
        
        # 构造 JSON
        crypto_json = json.dumps({
            "ciphertext": base64.b64encode(ciphertext).decode("utf-8"),
            "iv": iv.hex(),
            "salt": salt.hex(),
            "iterations": iterations
        })
        
        cipher_blob = base64.b64encode(crypto_json.encode("utf-8")).decode("utf-8")
        
        # 解密
        result = decrypt_session_key(cipher_blob, passphrase)
        self.assertEqual(result, plaintext)


class TestOKAPI(unittest.TestCase):
    """OK.ru API 解析测试"""
    
    def test_parse_ok_sources(self):
        """解析 OK.ru API 响应"""
        api_response = {
            "videos": [{
                "url_mobile": "https://vd750.okcdn.ru/?type=4&id=1",
                "url_tiny": "https://vd750.okcdn.ru/?type=0&id=2",
                "url_low": "https://vd750.okcdn.ru/?type=1&id=3",
                "url_medium": "https://vd750.okcdn.ru/?type=2&id=4",
                "url_high": "https://vd750.okcdn.ru/?type=3&id=5"
            }]
        }
        
        sources = parse_ok_sources(api_response)
        
        # 应该按标签高度降序
        self.assertEqual(len(sources), 5)
        self.assertEqual(sources[0]["label"], "720")
        self.assertEqual(sources[1]["label"], "480")
        self.assertEqual(sources[2]["label"], "360")
        self.assertEqual(sources[3]["label"], "240")
        self.assertEqual(sources[4]["label"], "144")


class TestURLDetection(unittest.TestCase):
    """URL 检测测试"""
    
    def test_detect_fpx_page(self):
        """检测 fullpornxxx 页面"""
        url = "https://www.fullpornxxx.net/video/35975/gimme-all-2_v2/"
        self.assertTrue(is_fpx_page(url))
    
    def test_reject_other_sites(self):
        """拒绝其他站点"""
        self.assertFalse(is_fpx_page("https://www.pornhub.com/view_video.php?viewkey=123"))
        self.assertFalse(is_fpx_page("https://www.1porn.tv/videos/test/"))


class TestMP4Probe(unittest.TestCase):
    """MP4 探测测试 (复用 1porn 测试模式)"""
    
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
            b"\x00" * 8 +
            handler_type +
            b"\x00" * 12 +
            b"VideoHandler\x00"
        )
        return self._make_box(b"hdlr", payload)
    
    def _make_tkhd_box(self, width: int, height: int) -> bytes:
        """构造 tkhd 盒子 (version 0)"""
        payload = (
            b"\x00\x00\x00\x00" +  # version + flags
            struct.pack(">I", 0) +  # creation_time
            struct.pack(">I", 0) +  # modification_time
            struct.pack(">I", 1) +  # track_ID
            struct.pack(">I", 0) +  # reserved
            struct.pack(">I", 1000) +  # duration
            b"\x00" * 8 +  # reserved
            struct.pack(">h", 0) +  # layer
            struct.pack(">h", 0) +  # alternate_group
            struct.pack(">h", 0x0100) +  # volume
            struct.pack(">h", 0) +  # reserved
            (b"\x00\x01\x00\x00" + b"\x00" * 12 + b"\x00\x01\x00\x00" + b"\x00" * 12 + b"\x40\x00\x00\x00") +  # matrix
            struct.pack(">I", width << 16) +  # width
            struct.pack(">I", height << 16)   # height
        )
        return self._make_box(b"tkhd", payload)
    
    def _make_moov_trak(self, width: int, height: int) -> bytes:
        """构造 moov/trak 结构"""
        hdlr = self._make_hdlr_box(b"vide")
        tkhd = self._make_tkhd_box(width, height)
        trak = self._make_box(b"trak", hdlr + tkhd)
        moov = self._make_box(b"moov", trak)
        return moov
    
    def test_probe_1280x720(self):
        """探测 1280×720 (fullpornxxx 样本最高)"""
        ftyp = self._make_ftyp_box()
        moov = self._make_moov_trak(1280, 720)
        data = ftyp + moov
        
        dims = probe_mp4_dimensions(data)
        self.assertIsNotNone(dims)
        self.assertEqual(dims, (1280, 720))
    
    def test_probe_852x480(self):
        """探测 852×480"""
        ftyp = self._make_ftyp_box()
        moov = self._make_moov_trak(852, 480)
        data = ftyp + moov
        
        dims = probe_mp4_dimensions(data)
        self.assertIsNotNone(dims)
        self.assertEqual(dims, (852, 480))


if __name__ == "__main__":
    unittest.main(verbosity=2)
