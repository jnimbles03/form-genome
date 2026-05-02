"""
Crawler politeness primitives: robots.txt honoring, per-host rate
limiting, and SSRF-safe redirect walking.

Lives at the service level so any caller (synchronous Flask request,
batch script, future Cloud Tasks worker) can opt in. Each primitive is
process-local; cluster-wide coordination is Wave 3 backlog.

Closes audit findings F-CS-10 (robots, 429 backoff, per-host limit,
hostile UA) and the F-CS-05 redirect-hop SSRF gap.
"""
from __future__ import annotations

import contextlib
import email.utils
import ipaddress
import logging
import os
import socket
import threading
import time
import typing as t
import urllib.error
import urllib.request
import urllib.robotparser
from urllib.parse import urlparse, urljoin

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Honest user agent. Identifies the bot, names a contact, points to source.
# Replaces the Chrome-impersonation strings that used to live in crawler.py
# and analyzer.py.
# ---------------------------------------------------------------------------
USER_AGENT = (
    "FormGenomeCrawler/1.0 "
    "(+https://github.com/jnimbles03/form-genome; "
    "contact: patrick.meyer@docusign.com)"
)

# Default knobs (env-tunable).
_DEFAULT_CONCURRENCY = int(os.getenv("CRAWL_HOST_CONCURRENCY", "2"))
_DEFAULT_RATE_PER_SEC = float(os.getenv("CRAWL_HOST_RATE_PER_SEC", "1.0"))
_ROBOTS_TIMEOUT_SEC = 10.0
_ROBOTS_TTL_SEC_DEFAULT = 86400  # 24h

# Hostnames we will never crawl regardless of resolution. Includes the
# common cloud metadata labels that resolve to non-link-local addresses
# in some networks.
_BLOCKED_HOSTNAMES = {
    "metadata.google.internal",
    "metadata",
    "metadata.aws.internal",
    "instance-data",
    "instance-data.ec2.internal",
    "localhost",
    "ip6-localhost",
    "ip6-loopback",
}


class PolitenessError(RuntimeError):
    """Raised when a politeness check refuses an action.

    Examples: robots.txt disallow, SSRF block, redirect loop, max hop
    exceeded. Subclass of RuntimeError so callers that already catch
    broad runtime errors continue to work.
    """


# ---------------------------------------------------------------------------
# SSRF defense
# ---------------------------------------------------------------------------

def is_safe_crawl_target(url: str) -> tuple[bool, str]:
    """
    Validate that `url` points at a public, non-metadata internet host.

    Returns (ok, reason). `reason` is human-readable when ok is False.

    Checks performed:
      1. URL parses and has http/https scheme.
      2. Hostname is present and not on the explicit blocklist.
      3. Every IP address the hostname resolves to (A and AAAA) passes:
         not loopback, not link-local, not private, not multicast, not
         reserved, not unspecified. Both IPv4 and IPv6 are handled by
         the stdlib ipaddress module.

    Public function so the redirect walker can re-check each hop.
    """
    if not url or not isinstance(url, str):
        return False, "empty or non-string url"

    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"unparseable url: {e}"

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"disallowed scheme: {scheme!r}"

    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False, "missing hostname"

    if host in _BLOCKED_HOSTNAMES:
        return False, f"blocked hostname: {host}"

    # If the hostname is itself an IP literal, validate it directly.
    try:
        ip_literal = ipaddress.ip_address(host)
        if (ip_literal.is_loopback or ip_literal.is_link_local
                or ip_literal.is_private or ip_literal.is_multicast
                or ip_literal.is_reserved or ip_literal.is_unspecified):
            return False, f"blocked IP literal: {host}"
        return True, "ok"
    except ValueError:
        # Not an IP literal — proceed to DNS resolution.
        pass

    try:
        addrinfo = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        return False, f"dns resolution failed: {e}"
    except Exception as e:
        return False, f"dns error: {e}"

    seen: set[str] = set()
    for entry in addrinfo:
        try:
            sockaddr = entry[4]
            ip_str = sockaddr[0]
        except Exception:
            continue
        if ip_str in seen:
            continue
        seen.add(ip_str)
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, f"unparseable resolved address: {ip_str}"
        if (ip.is_loopback or ip.is_link_local or ip.is_private
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return False, f"resolved to blocked address {ip_str} ({host})"

    if not seen:
        return False, f"no addresses resolved for {host}"

    return True, "ok"


# ---------------------------------------------------------------------------
# robots.txt cache
# ---------------------------------------------------------------------------

class RobotsCache:
    """
    Thread-safe robots.txt cache with TTL.

    Open-by-default: if robots.txt 404s, returns 5xx, or fails to fetch,
    `is_allowed` returns True. Failing closed would break crawls of
    agencies that haven't published one (most public-sector sites).

    Cache entries are tuples of (RobotFileParser, fetched_at_unix_ts).
    """

    def __init__(
        self,
        user_agent: str = USER_AGENT,
        ttl_seconds: int = _ROBOTS_TTL_SEC_DEFAULT,
    ) -> None:
        self._user_agent = user_agent
        self._ttl = int(ttl_seconds)
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[urllib.robotparser.RobotFileParser, float]] = {}

    def _robots_url_for(self, url: str) -> tuple[str, str] | None:
        """Return (host_key, robots_url) or None if URL is unusable."""
        try:
            p = urlparse(url)
        except Exception:
            return None
        scheme = (p.scheme or "https").lower()
        if scheme not in ("http", "https"):
            return None
        netloc = p.netloc
        if not netloc:
            return None
        host_key = f"{scheme}://{netloc}"
        return host_key, f"{host_key}/robots.txt"

    def _fetch(self, robots_url: str) -> urllib.robotparser.RobotFileParser:
        """Fetch and parse robots.txt with the honest UA. May raise."""
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(robots_url)
        req = urllib.request.Request(
            robots_url,
            headers={"User-Agent": self._user_agent},
        )
        # urlopen raises HTTPError/URLError on transport problems; let
        # those bubble up to is_allowed which treats them as "open".
        with urllib.request.urlopen(req, timeout=_ROBOTS_TIMEOUT_SEC) as resp:
            raw = resp.read()
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        rp.parse(text.splitlines())
        return rp

    def _get_parser(self, host_key: str, robots_url: str) -> urllib.robotparser.RobotFileParser | None:
        now = time.time()
        with self._lock:
            entry = self._cache.get(host_key)
            if entry is not None:
                rp, fetched = entry
                if (now - fetched) < self._ttl:
                    return rp
        # Fetch outside the lock.
        try:
            rp = self._fetch(robots_url)
        except urllib.error.HTTPError as e:
            # 4xx means "no robots" → open. 5xx is treated open too;
            # being too strict would break crawls during transient errors.
            logger.debug("robots HTTPError %s for %s — treating as open", e.code, robots_url)
            rp = None
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            logger.debug("robots fetch failed for %s (%s) — treating as open", robots_url, e)
            rp = None
        except Exception as e:
            logger.debug("robots unexpected error for %s (%s) — treating as open", robots_url, e)
            rp = None

        with self._lock:
            # Cache the (possibly-None) result so we don't re-hit a dead
            # robots.txt every request. None means "open by default".
            self._cache[host_key] = (rp, now)  # type: ignore[assignment]
        return rp

    def is_allowed(self, url: str) -> bool:
        """Return True if `url` is allowed for our UA per its host's robots.txt.

        Open by default: returns True on any fetch/parse failure.
        """
        info = self._robots_url_for(url)
        if info is None:
            return True
        host_key, robots_url = info
        rp = self._get_parser(host_key, robots_url)
        if rp is None:
            return True
        try:
            return bool(rp.can_fetch(self._user_agent, url))
        except Exception as e:
            logger.debug("robots can_fetch raised for %s (%s) — treating as open", url, e)
            return True


# ---------------------------------------------------------------------------
# Per-host rate limiter
# ---------------------------------------------------------------------------

class HostRateLimiter:
    """
    Per-host concurrency cap + token-bucket-ish throttle.

    - `default_concurrency`: max simultaneous in-flight requests per host.
    - `default_rate_per_sec`: throttle ceiling per host. Implemented as a
      minimum interval between successive `acquire` releases for the
      same host.

    Honors a per-host `Retry-After` window set by `set_retry_after`
    (use this when the upstream returns 429). The next acquire for that
    host will block until the window expires.

    Process-local. Multiple Cloud Run instances will not coordinate
    until Wave 3 introduces a shared limiter (Redis token bucket or
    similar).
    """

    def __init__(
        self,
        default_concurrency: int = _DEFAULT_CONCURRENCY,
        default_rate_per_sec: float = _DEFAULT_RATE_PER_SEC,
    ) -> None:
        self._concurrency = max(1, int(default_concurrency))
        self._rate = max(0.001, float(default_rate_per_sec))
        self._min_interval = 1.0 / self._rate
        self._lock = threading.RLock()
        self._semaphores: dict[str, threading.Semaphore] = {}
        self._last_fetch: dict[str, float] = {}
        self._retry_until: dict[str, float] = {}

    @staticmethod
    def _host_key(url: str) -> str:
        try:
            netloc = (urlparse(url).netloc or "").lower()
        except Exception:
            netloc = ""
        return netloc or "_unknown_"

    def _get_sem(self, host: str) -> threading.Semaphore:
        with self._lock:
            sem = self._semaphores.get(host)
            if sem is None:
                sem = threading.Semaphore(self._concurrency)
                self._semaphores[host] = sem
            return sem

    def set_retry_after(self, host: str, seconds: float) -> None:
        """Record a 429-driven backoff window for `host` (seconds from now).

        `host` should be the netloc (e.g. "www.example.com"). If a full
        URL is passed accidentally we'll extract the netloc.
        """
        try:
            seconds_f = max(0.0, float(seconds))
        except Exception:
            return
        if "://" in host:
            host = self._host_key(host)
        host = (host or "").lower()
        until = time.time() + seconds_f
        with self._lock:
            prev = self._retry_until.get(host, 0.0)
            if until > prev:
                self._retry_until[host] = until
        logger.info("rate limiter: host=%s backoff for %.1fs (Retry-After)", host, seconds_f)

    @contextlib.contextmanager
    def acquire(self, url: str):
        """Context manager: blocks until a slot is available for the URL's host.

        Usage:
            with limiter.acquire(url):
                resp = session.get(url, ...)
        """
        host = self._host_key(url)
        sem = self._get_sem(host)
        sem.acquire()
        try:
            self._wait_for_slot(host)
            yield
        finally:
            with self._lock:
                self._last_fetch[host] = time.time()
            sem.release()

    def _wait_for_slot(self, host: str) -> None:
        """Block until any Retry-After window has passed AND the rate
        interval has elapsed since the last fetch for this host."""
        # Cap total wait to avoid pathologically long sleeps if a server
        # sets a multi-hour Retry-After. Caller will retry next pass.
        max_total_wait = 300.0
        deadline = time.time() + max_total_wait
        while True:
            now = time.time()
            with self._lock:
                retry_until = self._retry_until.get(host, 0.0)
                last = self._last_fetch.get(host, 0.0)
            wait = 0.0
            if retry_until > now:
                wait = retry_until - now
            interval_left = (last + self._min_interval) - now
            if interval_left > wait:
                wait = interval_left
            if wait <= 0:
                return
            if now + wait > deadline:
                # Sleep the remainder and bail; the server's Retry-After
                # is longer than we want to hold the worker for.
                wait = max(0.0, deadline - now)
                if wait > 0:
                    time.sleep(wait)
                return
            time.sleep(min(wait, 5.0))


# ---------------------------------------------------------------------------
# SSRF-safe redirect walker
# ---------------------------------------------------------------------------

def safe_redirect_walk(
    url: str,
    session,
    max_hops: int = 5,
    ssrf_check: t.Callable[[str], tuple[bool, str]] | None = None,
    timeout: t.Any = None,
    headers: dict | None = None,
    verify: t.Any = None,
    method: str = "GET",
    stream: bool = False,
):
    """
    Manually walk redirects with SSRF re-validation at each hop.

    Closes the F-CS-05 "redirect hop" gap: requests's default
    `allow_redirects=True` follows 3xx without reapplying the SSRF
    check, so a public host can 302 the crawler to 169.254.169.254
    or an internal service.

    Args:
        url: Initial URL.
        session: A requests.Session (or compatible) used to issue the
            request. Caller is responsible for setting the honest UA on
            session.headers.
        max_hops: Max number of 3xx hops to follow. Raises
            PolitenessError if exceeded.
        ssrf_check: Callable returning (ok, reason). Defaults to
            `is_safe_crawl_target`. Raises PolitenessError if any hop
            fails.
        timeout, headers, verify, stream: Passed through to
            session.request on each hop.
        method: HTTP method on the FIRST request only; redirects are
            followed as GET (matching browser behavior for 301/302/303).

    Returns the final non-3xx requests.Response.
    """
    if ssrf_check is None:
        ssrf_check = is_safe_crawl_target

    current_url = url
    current_method = (method or "GET").upper()
    seen_urls: list[str] = []
    last_resp = None

    for hop in range(max_hops + 1):
        ok, reason = ssrf_check(current_url)
        if not ok:
            raise PolitenessError(
                f"SSRF check failed at hop {hop}: {reason} (url={current_url})"
            )
        if current_url in seen_urls:
            raise PolitenessError(f"redirect loop detected at hop {hop}: {current_url}")
        seen_urls.append(current_url)

        kwargs: dict[str, t.Any] = {"allow_redirects": False}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if headers is not None:
            kwargs["headers"] = headers
        if verify is not None:
            kwargs["verify"] = verify
        if stream:
            kwargs["stream"] = True

        resp = session.request(current_method, current_url, **kwargs)
        last_resp = resp

        if resp.status_code not in (301, 302, 303, 307, 308):
            return resp

        location = resp.headers.get("Location")
        if not location:
            return resp

        # Resolve relative redirects against the current URL.
        next_url = urljoin(current_url, location)

        # Per RFC 7231 §6.4.4, 303 always becomes GET; 301/302 are
        # historically downgraded to GET as well. 307/308 preserve the
        # method. We use GET for 301/302/303 to match browser behavior.
        if resp.status_code in (301, 302, 303):
            current_method = "GET"
        # Drain/close the intermediate response body so the connection
        # can be reused.
        try:
            resp.close()
        except Exception:
            pass

        current_url = next_url

    raise PolitenessError(
        f"max_hops={max_hops} redirect limit exceeded; last url={current_url}"
    )


# ---------------------------------------------------------------------------
# Module-level singletons. Callers can also instantiate their own.
# ---------------------------------------------------------------------------

_DEFAULT_ROBOTS = RobotsCache()
_DEFAULT_LIMITER = HostRateLimiter()


def default_robots_cache() -> RobotsCache:
    return _DEFAULT_ROBOTS


def default_host_limiter() -> HostRateLimiter:
    return _DEFAULT_LIMITER


def parse_retry_after(value: str | None) -> float | None:
    """
    Parse a Retry-After header (RFC 7231 §7.1.3). Returns seconds from
    now, or None if unparseable.

    Accepts either an integer number of seconds or an HTTP-date.
    """
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return max(0.0, float(int(value)))
    except (TypeError, ValueError):
        pass
    try:
        # HTTP-date → epoch seconds
        dt = email.utils.parsedate_to_datetime(value)
        if dt is None:
            return None
        delta = dt.timestamp() - time.time()
        return max(0.0, float(delta))
    except Exception:
        return None
