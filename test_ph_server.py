#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for /proxy SSRF allowlist and MP4 resolve wrappers."""

import unittest
from unittest.mock import patch
from urllib.parse import quote

from download_fpv import is_fpv_page
from download_wh import is_wh_page
from ph_server import (
    _content_disposition_attachment,
    _mp4_download_filename,
    _proxy_host_allowed,
    _proxy_path,
    _redact_url,
    _safe_download_filename,
    _stream_mp4_proxy,
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



class FakeWFile:
    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf.extend(data)


class FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = []
        self.wfile = FakeWFile()
        self.ended = False

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.headers.append((key, value))

    def end_headers(self):
        self.ended = True

    def header_map(self):
        return {k.lower(): v for k, v in self.headers}


class FakeUrllibResp:
    def __init__(self, status=200, headers=None, body=b"ftypfake"):
        self.status = status
        self.headers = headers or {
            "Content-Type": "video/mp4",
            "Content-Length": str(len(body)),
            "Accept-Ranges": "bytes",
        }
        self._body = body
        self.closed = False

    def getcode(self):
        return self.status

    def read(self, n=-1):
        if n is None or n < 0:
            data = self._body
            self._body = b""
            return data
        data, self._body = self._body[:n], self._body[n:]
        return data

    def close(self):
        self.closed = True


class TestDownloadFilename(unittest.TestCase):
    def test_strips_paths_and_adds_extension(self):
        self.assertEqual(_safe_download_filename("../../etc/passwd"), "passwd.mp4")
        self.assertEqual(_safe_download_filename("cute video"), "cute video.mp4")
        self.assertEqual(_safe_download_filename(""), "video.mp4")
        self.assertEqual(_safe_download_filename("film.MP4"), "film.MP4")

    def test_strips_header_injection_and_quotes(self):
        got = _safe_download_filename('evil\r\nX-Inject: 1"name')
        self.assertNotIn("\r", got)
        self.assertNotIn("\n", got)
        self.assertNotIn('"', got)
        self.assertTrue(got.endswith(".mp4"))

    def test_content_disposition_has_ascii_and_utf8(self):
        header = _content_disposition_attachment("中文标题")
        self.assertTrue(header.startswith("attachment; "))
        self.assertIn('filename="', header)
        self.assertIn("filename*=UTF-8''", header)
        self.assertIn(quote("中文标题.mp4", safe=""), header)

    def test_dl_flag_required(self):
        job = {"title": "from-job"}
        self.assertIsNone(_mp4_download_filename("job=abc&u=https://example.com/a.mp4", job))
        self.assertEqual(
            _mp4_download_filename("job=abc&u=https://example.com/a.mp4&dl=1", job),
            "from-job.mp4",
        )

    def test_filename_query_preferred_and_sanitized(self):
        job = {"title": "from-job"}
        query = "dl=1&filename=" + "%2e%2e%2fsecret"
        self.assertEqual(_mp4_download_filename(query, job), "secret.mp4")


class TestStreamMp4Disposition(unittest.TestCase):
    def _run(self, download_filename=None, range_header=None):
        body = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32
        upstream = FakeUrllibResp(body=body)
        handler = FakeHandler()
        job = {"session": object(), "referer": "https://www.whoreshub.com/videos/1/", "site": "wh"}
        with patch("ph_server._iter_urllib_mp4", return_value=upstream):
            _stream_mp4_proxy(
                handler,
                job,
                "https://www.whoreshub.com/get_file/1/x.mp4",
                range_header,
                download_filename,
            )
        return handler, upstream

    def test_attachment_headers_when_downloading(self):
        handler, upstream = self._run(download_filename="演示 视频")
        headers = handler.header_map()
        self.assertEqual(handler.status, 200)
        self.assertTrue(handler.ended)
        self.assertEqual(headers.get("content-type"), "video/mp4")
        self.assertEqual(headers.get("content-length"), str(len(handler.wfile.buf)))
        self.assertEqual(headers.get("accept-ranges"), "bytes")
        disp = headers.get("content-disposition") or ""
        self.assertTrue(disp.startswith("attachment;"))
        self.assertIn("filename*=UTF-8''", disp)
        self.assertIn("Content-Disposition", headers.get("access-control-expose-headers", ""))
        self.assertTrue(upstream.closed)

    def test_no_disposition_without_download_flag(self):
        handler, _ = self._run(download_filename=None)
        headers = handler.header_map()
        self.assertNotIn("content-disposition", headers)
        self.assertEqual(headers.get("content-length"), str(len(handler.wfile.buf)))
        self.assertEqual(headers.get("accept-ranges"), "bytes")
        self.assertNotIn("Content-Disposition", headers.get("access-control-expose-headers", ""))

    def test_range_download_keeps_disposition_and_content_range(self):
        body = b"0123456789"
        upstream = FakeUrllibResp(
            status=206,
            headers={
                "Content-Type": "video/mp4",
                "Content-Length": "5",
                "Content-Range": "bytes 0-4/10",
                "Accept-Ranges": "bytes",
            },
            body=body[:5],
        )
        handler = FakeHandler()
        job = {"session": object(), "referer": "https://www.freepornvideos.xxx/videos/1/", "site": "fpv"}
        with patch("ph_server._iter_urllib_mp4", return_value=upstream):
            _stream_mp4_proxy(
                handler,
                job,
                "https://www.freepornvideos.xxx/get_file/1/x.mp4",
                "bytes=0-4",
                "clip",
            )
        headers = handler.header_map()
        self.assertEqual(handler.status, 206)
        self.assertEqual(headers.get("content-range"), "bytes 0-4/10")
        self.assertEqual(headers.get("content-length"), "5")
        self.assertTrue((headers.get("content-disposition") or "").startswith("attachment;"))



if __name__ == "__main__":
    unittest.main(verbosity=2)
