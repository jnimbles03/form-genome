"""
Smoke tests for app.services.politeness.

All network calls are mocked so this runs offline. Uses stdlib unittest.
"""
from __future__ import annotations

import io
import unittest
from unittest import mock

from app.services import politeness


class TestIsSafeCrawlTarget(unittest.TestCase):
    def test_rejects_metadata_ip(self):
        ok, reason = politeness.is_safe_crawl_target("http://169.254.169.254/")
        self.assertFalse(ok)
        self.assertIn("blocked", reason.lower())

    def test_rejects_rfc1918_10(self):
        ok, _ = politeness.is_safe_crawl_target("http://10.0.0.1/")
        self.assertFalse(ok)

    def test_rejects_rfc1918_192(self):
        ok, _ = politeness.is_safe_crawl_target("http://192.168.1.1/")
        self.assertFalse(ok)

    def test_rejects_loopback(self):
        ok, _ = politeness.is_safe_crawl_target("http://127.0.0.1/")
        self.assertFalse(ok)

    def test_rejects_metadata_hostname(self):
        ok, reason = politeness.is_safe_crawl_target("http://metadata.google.internal/")
        self.assertFalse(ok)
        self.assertIn("blocked hostname", reason)

    def test_rejects_disallowed_scheme(self):
        ok, _ = politeness.is_safe_crawl_target("file:///etc/passwd")
        self.assertFalse(ok)

    def test_rejects_empty_url(self):
        ok, _ = politeness.is_safe_crawl_target("")
        self.assertFalse(ok)

    def test_accepts_public_ip(self):
        # 8.8.8.8 is a literal public IP — no DNS needed.
        ok, reason = politeness.is_safe_crawl_target("https://8.8.8.8/")
        self.assertTrue(ok, reason)

    def test_accepts_public_hostname(self):
        # Mock DNS so the test doesn't need a real network.
        fake_addrinfo = [(0, 0, 0, "", ("93.184.216.34", 0))]
        with mock.patch("app.services.politeness.socket.getaddrinfo",
                        return_value=fake_addrinfo):
            ok, reason = politeness.is_safe_crawl_target("https://example.com/path")
        self.assertTrue(ok, reason)


class TestRobotsCache(unittest.TestCase):
    def test_open_when_fetch_raises(self):
        cache = politeness.RobotsCache(ttl_seconds=60)
        with mock.patch("app.services.politeness.urllib.request.urlopen",
                        side_effect=OSError("network down")):
            self.assertTrue(cache.is_allowed("https://example.com/anything"))

    def test_respects_disallow(self):
        robots_body = b"User-agent: *\nDisallow: /admin/\n"

        class _FakeResp:
            def __init__(self, body): self._body = body
            def read(self): return self._body
            def __enter__(self): return self
            def __exit__(self, *a): return False

        cache = politeness.RobotsCache(ttl_seconds=60)
        with mock.patch("app.services.politeness.urllib.request.urlopen",
                        return_value=_FakeResp(robots_body)):
            self.assertFalse(cache.is_allowed("https://example.com/admin/secret"))
            self.assertTrue(cache.is_allowed("https://example.com/public"))

    def test_cache_reuses_parser(self):
        robots_body = b"User-agent: *\nAllow: /\n"

        class _FakeResp:
            def __init__(self, body): self._body = body
            def read(self): return self._body
            def __enter__(self): return self
            def __exit__(self, *a): return False

        cache = politeness.RobotsCache(ttl_seconds=60)
        with mock.patch("app.services.politeness.urllib.request.urlopen",
                        return_value=_FakeResp(robots_body)) as urlopen_mock:
            cache.is_allowed("https://example.com/a")
            cache.is_allowed("https://example.com/b")
            self.assertEqual(urlopen_mock.call_count, 1)


class _FakeResponse:
    def __init__(self, status_code, location=None):
        self.status_code = status_code
        self.headers = {}
        if location is not None:
            self.headers["Location"] = location
        self.text = ""

    def close(self):
        pass


class TestSafeRedirectWalk(unittest.TestCase):
    def test_returns_terminal_response(self):
        session = mock.MagicMock()
        session.request.return_value = _FakeResponse(200)
        resp = politeness.safe_redirect_walk(
            "https://8.8.8.8/", session, max_hops=3,
            ssrf_check=lambda u: (True, "ok"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(session.request.call_count, 1)
        # Must call with allow_redirects=False so we walk manually.
        _, kwargs = session.request.call_args
        self.assertFalse(kwargs.get("allow_redirects", True))

    def test_raises_after_max_hops(self):
        session = mock.MagicMock()
        # Always 302 to a new URL.
        def _resp(*args, **kwargs):
            url = args[1] if len(args) >= 2 else kwargs.get("url")
            return _FakeResponse(302, location=url + "x")
        session.request.side_effect = _resp
        with self.assertRaises(politeness.PolitenessError):
            politeness.safe_redirect_walk(
                "https://8.8.8.8/", session, max_hops=3,
                ssrf_check=lambda u: (True, "ok"),
            )

    def test_blocks_on_ssrf_at_redirect(self):
        session = mock.MagicMock()
        session.request.return_value = _FakeResponse(302, location="http://169.254.169.254/")
        # First hop public, second hop blocked.
        seen: list[str] = []

        def _check(u):
            seen.append(u)
            if "169.254.169.254" in u:
                return False, "blocked IP literal"
            return True, "ok"

        with self.assertRaises(politeness.PolitenessError):
            politeness.safe_redirect_walk(
                "https://8.8.8.8/", session, max_hops=5, ssrf_check=_check,
            )
        # Must have rejected on the second URL, not the first.
        self.assertEqual(len(seen), 2)


class TestParseRetryAfter(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(politeness.parse_retry_after("30"), 30.0)

    def test_zero(self):
        self.assertEqual(politeness.parse_retry_after("0"), 0.0)

    def test_invalid(self):
        self.assertIsNone(politeness.parse_retry_after("not a number"))

    def test_none(self):
        self.assertIsNone(politeness.parse_retry_after(None))

    def test_empty(self):
        self.assertIsNone(politeness.parse_retry_after(""))


class TestHostRateLimiter(unittest.TestCase):
    def test_acquire_releases_slot(self):
        limiter = politeness.HostRateLimiter(default_concurrency=1, default_rate_per_sec=1000.0)
        # Two sequential acquires must both succeed.
        with limiter.acquire("https://example.com/a"):
            pass
        with limiter.acquire("https://example.com/b"):
            pass

    def test_set_retry_after_records_window(self):
        limiter = politeness.HostRateLimiter(default_concurrency=1, default_rate_per_sec=1000.0)
        limiter.set_retry_after("example.com", 5.0)
        # Internal state: a future timestamp recorded.
        with mock.patch.object(politeness, "time") as t_mock:
            t_mock.time.return_value = 0.0
            self.assertGreater(limiter._retry_until.get("example.com", 0.0), 0.0)


if __name__ == "__main__":
    unittest.main()
