#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for /proxy SSRF allowlist and MP4 resolve wrappers."""

import unittest
from unittest.mock import patch

from download_fpv import is_fpv_page
from download_wh import is_wh_page
from ph_server import (
    _proxy_host_allowed,
    _proxy_path,
    _redact_url,
    resolve_1porn_job_wrapper,
    resolve_fpx_job_wrapper,
    resolve_page,
)


class TestProxyAllowlist(unittest.TestCase):
    def test_allows_new_site_media_hosts(self):
        for host in (
            "www.whoreshub.com",
            "origin36-direct.cdntrex.com",
            "wh.cdntrex.com",
            "www.hdporn.gg",
            "ip10.ahcdn.com",
            "www.1porn.tv",
            "d923341.fpvcdn.com",
            "www.freepornvideos.xxx",
            "www.fullpornxxx.net",
            "api.ok.ru",
            "vd750.okcdn.ru",
            "di.phncdn.com",
            "www.pornhd3x.tv",
        ):
            self.assertTrue(_proxy_host_allowed(host), host)

    def test_rejects_private_and_unknown_hosts(self):
        for host in (
            "localhost",
            "127.0.0.1",
            "10.0.0.1",
            "192.168.1.1",
            "169.254.1.1",
            "evil.com",
            "whoreshub.com.evil.com",
            "not-cdntrex.com",
        ):
            self.assertFalse(_proxy_host_allowed(host), host)


class TestDispatch(unittest.TestCase):
    def test_whoreshub_is_not_classified_as_fpv(self):
        url = "https://www.whoreshub.com/videos/730394/title/"
        self.assertTrue(is_wh_page(url))
        self.assertFalse(is_fpv_page(url))

    def test_unknown_host_is_rejected_without_network(self):
        got = resolve_page("https://example.com/videos/1/")
        self.assertFalse(got.get("ok"))
        self.assertIn("请输入", got.get("error") or "")


class TestRedactUrl(unittest.TestCase):
    def test_redacts_v_acctoken(self):
        url = "https://www.whoreshub.com/get_file/7/abc/file.mp4/?v-acctoken=SUPERSECRET&x=1"
        out = _redact_url(url)
        self.assertNotIn("SUPERSECRET", out)
        self.assertIn("v-acctoken=REDACTED", out)
        self.assertIn("x=1", out)

    def test_redacts_token_inside_access_log_line(self):
        line = (
            '"GET /proxy?job=abc&u=https%3A%2F%2Fwww.whoreshub.com%2Fget_file%2F7%2Fx.mp4%2F%3Fv-acctoken%3DSUPERSECRET '
            'HTTP/1.1" 206 -'
        )
        out = _redact_url(line)
        self.assertNotIn("SUPERSECRET", out)
        self.assertIn("HTTP/1.1", out)
        self.assertIn("206", out)


class TestMp4Wrappers(unittest.TestCase):
    def test_1porn_wrapper_returns_proxied_is_mp4(self):
        fake = {
            "job_id": "abcd1234",
            "url": "https://d923341.fpvcdn.com/video.mp4",
            "title": "demo",
            "quality_label": "720p",
            "quality_note": "实际分辨率 1280×720 (标签: 720p)",
            "session": object(),
            "page_url": "https://www.1porn.tv/zh/videos/demo/",
        }
        with patch("ph_server.resolve_1porn_job", return_value=fake):
            got = resolve_1porn_job_wrapper(fake["page_url"])
        self.assertTrue(got.get("ok"))
        self.assertTrue(got.get("is_mp4"))
        self.assertTrue(got["mp4_url"].startswith("/proxy?job=abcd1234&u="))
        self.assertIn("fpvcdn.com", got["mp4_url"])
        self.assertEqual(got.get("site"), "1porn")

    def test_fpx_wrapper_returns_proxied_is_mp4(self):
        fake = {
            "job_id": "efgh5678",
            "url": "https://vd750.okcdn.ru/video.mp4",
            "title": "demo",
            "quality_label": "720",
            "quality_note": "实际分辨率 1280×720 (标签: 720)",
            "session": object(),
            "page_url": "https://www.fullpornxxx.net/video/1/demo/",
        }
        with patch("ph_server.resolve_fpx_job", return_value=fake):
            got = resolve_fpx_job_wrapper(fake["page_url"])
        self.assertTrue(got.get("ok"))
        self.assertTrue(got.get("is_mp4"))
        self.assertTrue(got["mp4_url"].startswith("/proxy?job=efgh5678&u="))
        self.assertIn("okcdn.ru", got["mp4_url"])
        self.assertEqual(got.get("site"), "fpx")

    def test_proxy_path_encodes_query(self):
        path = _proxy_path("https://www.whoreshub.com/get_file/1/x.mp4/?v-acctoken=abc&d=1", "job1")
        self.assertTrue(path.startswith("/proxy?job=job1&u="))
        self.assertNotIn("&v-acctoken=", path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
