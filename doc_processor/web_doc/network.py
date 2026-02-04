"""
Shared networking utilities for crawling/scraping:
- Managed proxy/IP rotation (HTTP/HTTPS and SOCKS)
- Per-host adaptive pacing (rate limiting with backoff and jitter)
- robots.txt handling (can_fetch + crawl-delay seeding)
- TLS setup with certifi; browser-like headers
- One-shot fallback using requests (per-proxy sessions)
- Clean shutdown of all sessions

Configuration (via .env or environment):
    MAPCODOC_PROXIES="socks5://127.0.0.1:9050;http://127.0.0.1:8118"
"""

from __future__ import annotations

import os
import re
import ssl
import time
import aiohttp
import logging
import asyncio
import requests
import certifi
from dotenv import load_dotenv
from urllib.parse import urlparse
import urllib.robotparser as robotparser
from charset_normalizer import from_bytes
from typing import Optional, Dict, Tuple
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


try:
    from aiohttp_socks import ProxyConnector
    HAS_SOCKS = True
except Exception:
    HAS_SOCKS = False

logger = logging.getLogger(__name__)

# Load environment variables (including MAPCODOC_PROXIES)
load_dotenv()


def _parse_proxy_env() -> list[str]:
    """
    Load MAPCODOC_PROXIES and split by semicolon/comma/newline.
    Examples:
      MAPCODOC_PROXIES="socks5://127.0.0.1:9050;http://127.0.0.1:8118"
    """
    raw = os.getenv("MAPCODOC_PROXIES", "").strip()
    if not raw:
        return []
    return [p.strip() for p in re.split(r"[;\n,]+", raw) if p.strip()]

def _is_socks_proxy(url: Optional[str]) -> bool:
    return bool(url) and url.lower().startswith(("socks5://", "socks4://"))

def _make_ssl_context() -> ssl.SSLContext:
    """
    Create a default SSL context with certifi and minimum TLS v1.2.
    """
    ctx = ssl.create_default_context()
    try:
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    if hasattr(ssl, "TLSVersion"):
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx

def parse_retry_after(h: Optional[str]) -> Optional[float]:
    """
    Parse an HTTP Retry-After header into seconds.
      - "120" -> 120.0
      - "Wed, 21 Oct 2015 07:28:00 GMT" -> seconds until that time
    Returns None if parse fails.
    """
    if not h:
        return None
    h = h.strip()
    try:
        secs = float(h)
        return max(0.0, secs)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(h)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return max(0.0, (dt - now).total_seconds())
    except Exception:
        return None


def _sanitize_header_value(v: str) -> str:
    """Remove CR/LF and surrounding spaces to prevent header injection."""
    return v.replace('\r', '').replace('\n', '').strip()


class HostLimiter:
    """
    Per-host adaptive rate limiter.
      - acquire(host): wait until host is ready based on current delay.
      - on_success(host): gently ramp down the delay.
      - on_backoff(host, retry_after): increase delay (exponential or to Retry-After).
    """

    def __init__(self, base_delay: float = 0.15, max_delay: float = 6.0):
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.state: Dict[str, Dict[str, float]] = {}  # host -> {"delay": float, "last_ts": float}

    async def acquire(self, host: str):
        st = self.state.setdefault(host, {"delay": self.base_delay, "last_ts": 0.0})
        now = time.monotonic()
        wait = max(0.0, (st["last_ts"] + st["delay"]) - now)
        if wait > 0:
            # jitter avoids thundering herd
            await asyncio.sleep(wait + (0.0 if st["delay"] < 0.05 else 0.05))
        st["last_ts"] = time.monotonic()

    def on_success(self, host: str):
        st = self.state.setdefault(host, {"delay": self.base_delay, "last_ts": 0.0})
        st["delay"] = max(self.base_delay, st["delay"] * 0.8)

    def on_backoff(self, host: str, retry_after: Optional[float]):
        st = self.state.setdefault(host, {"delay": self.base_delay, "last_ts": 0.0})
        if retry_after is not None:
            st["delay"] = min(self.max_delay, max(st["delay"], retry_after))
        else:
            st["delay"] = min(self.max_delay, st["delay"] * 2.0)


class ProxyManager:
    """
    Simple proxy router with per-proxy concurrency and cooldowns.

    Proxies can be HTTP/HTTPS or SOCKS URLs:
      - http://user:pass@host:port
      - socks5://user:pass@host:port  (requires aiohttp_socks)
    """

    DIRECT = None  # sentinel for direct routing

    def __init__(self, proxies: list[str], per_proxy_max_concurrency: int = 3):
        self.proxies = proxies[:] or [self.DIRECT]
        self.sem = {p: asyncio.Semaphore(per_proxy_max_concurrency) for p in self.proxies}
        self.state = {p: {"score": 1.0, "cooldown_until": 0.0, "per_host": {}} for p in self.proxies}
        self.sessions: Dict[Optional[str], aiohttp.ClientSession] = {}

    async def get_session(
        self,
        proxy: Optional[str],
        ssl_context: ssl.SSLContext,
        headers: Dict[str, str],
        timeout: aiohttp.ClientTimeout,
        connector_limit: int = 0,
    ) -> aiohttp.ClientSession:
        """
        Return a cached aiohttp.ClientSession for the given proxy (or direct).
        Reuse if open; if stale/closed, recreate to prevent leaks.
        """
        sess = self.sessions.get(proxy)
        if sess is not None and not sess.closed:
            return sess

        # Close stale session (if any)
        if sess is not None:
            try:
                await sess.close()
            except Exception:
                pass

        # Build connector:
        if proxy is not None and _is_socks_proxy(proxy):
            if not HAS_SOCKS:
                raise RuntimeError("SOCKS proxy requested but aiohttp_socks is not installed.")
            connector = ProxyConnector.from_url(proxy)  # connector handles routing for SOCKS
            # For SOCKS, DO NOT pass proxy= in session.get; the connector routes it.
        else:
            connector = aiohttp.TCPConnector(
                limit=connector_limit,
                ssl=ssl_context,
                ttl_dns_cache=300,
                keepalive_timeout=60,
            )

        sess = aiohttp.ClientSession(connector=connector, headers=headers, timeout=timeout)
        self.sessions[proxy] = sess
        return sess

    async def acquire(self, host: str) -> tuple[Optional[str], asyncio.Semaphore]:
        """
        Choose an eligible proxy (or direct). Cooldowns are applied per-proxy and per-host.
        """
        now = time.monotonic()
        eligible = []
        for p, st in self.state.items():
            if now < st["cooldown_until"]:
                continue
            hst = st["per_host"].get(host, {})
            if now < hst.get("cooldown_until", 0.0):
                continue
            eligible.append((st["score"], p))

        if not eligible:
            # If all are cooling down, wait briefly and pick the earliest to recover
            await asyncio.sleep(0.5)
            p = min(self.state.items(), key=lambda kv: kv[1]["cooldown_until"])[0]
            return p, self.sem[p]

        p = sorted(eligible, reverse=True)[0][1]
        return p, self.sem[p]

    def report(self, host: str, proxy: Optional[str], status: Optional[int], latency: Optional[float], retry_after_s: Optional[float]):
        """
        Update proxy health and per-host cooldown based on the last request outcome.
        """
        st = self.state[proxy]
        if status in (429, 403):
            hst = st["per_host"].setdefault(host, {})
            cooldown = retry_after_s or min(6.0, (hst.get("cooldown_until", 0.0) + 1.0))
            hst["cooldown_until"] = time.monotonic() + cooldown
            st["score"] = max(0.1, st["score"] * 0.8)
        elif status and 200 <= status < 300:
            st["score"] = min(5.0, st["score"] + 0.1)

    async def close(self):
        """Close all aiohttp sessions created for proxies/direct routing."""
        for s in list(self.sessions.values()):
            try:
                await s.close()
            except Exception:
                pass
        self.sessions.clear()


class RobotsCache:
    """robots.txt loader/cache per host; captures crawl-delay to seed the limiter."""

    def __init__(self, user_agent: str):
        self.user_agent = user_agent
        self.cache: Dict[str, Tuple[Optional[robotparser.RobotFileParser], Optional[float]]] = {}

    async def ensure(self, session: aiohttp.ClientSession, url: str):
        parsed = urlparse(url)
        netloc = parsed.netloc
        if netloc in self.cache:
            return
        robots_url = f"{parsed.scheme}://{netloc}/robots.txt"
        rp = robotparser.RobotFileParser()
        try:
            async with session.get(robots_url, headers={'User-Agent': self.user_agent}, allow_redirects=True) as r:
                if r.status == 200:
                    text = await r.text(errors='ignore')
                    rp.parse(text.splitlines())
                    delay = rp.crawl_delay(self.user_agent) or rp.crawl_delay('*')
                    self.cache[netloc] = (rp, float(delay) if delay else None)
                else:
                    self.cache[netloc] = (None, None)
        except Exception:
            self.cache[netloc] = (None, None)

    def can_fetch(self, url: str) -> bool:
        parsed = urlparse(url)
        rp, _ = self.cache.get(parsed.netloc, (None, None))
        if rp is None:
            return True  # no robots info -> allow
        try:
            return rp.can_fetch(self.user_agent, url)
        except Exception:
            return True

    def crawl_delay(self, url: string) -> Optional[float]:
        parsed = urlparse(url)
        _, delay = self.cache.get(parsed.netloc, (None, None))
        return delay


class URLFetcher:
    """
    High-level fetcher:
      - Proxy rotation (HTTP/HTTPS and SOCKS)
      - robots.txt + crawl-delay
      - per-host adaptive limiter
      - TLS + browser headers
      - requests fallback
      - clean shutdown
    """

    def __init__(self, proxies: Optional[list[str]] = None):
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
        self.default_headers = {
            'User-Agent': self.user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
        }
        self.ssl_context = _make_ssl_context()
        self.timeout = aiohttp.ClientTimeout(total=15, connect=8)

        # Build proxy manager (add DIRECT routing if no proxies provided)
        self.proxies = proxies[:] if proxies is not None else _parse_proxy_env()
        self.proxy_manager = ProxyManager(self.proxies or [ProxyManager.DIRECT], per_proxy_max_concurrency=10)

        self.host_limiter = HostLimiter(base_delay=0.05, max_delay=6.0)
        self.robots = RobotsCache(self.user_agent)

        # Direct/default session (without proxy)
        self._direct_connector = aiohttp.TCPConnector(
            limit=100,
            ssl=self.ssl_context,
            ttl_dns_cache=300,
            keepalive_timeout=60,
        )
        self._direct_session: Optional[aiohttp.ClientSession] = None

        # requests fallback pool per proxy key (proxy URL or "DIRECT")
        self._requests_pool: Dict[str, requests.Session] = {}

    async def __aenter__(self) -> "URLFetcher":
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.shutdown()

    async def _ensure_direct_session(self) -> aiohttp.ClientSession:
        if self._direct_session is None or self._direct_session.closed:
            self._direct_session = aiohttp.ClientSession(
                connector=self._direct_connector,
                headers=self.default_headers,
                timeout=self.timeout,
            )
        return self._direct_session

    def _get_requests_session_for_proxy(self, proxy_url: Optional[str]) -> requests.Session:
        key = proxy_url or "DIRECT"
        sess = self._requests_pool.get(key)
        if sess is not None:
            return sess
        s = requests.Session()
        retries = Retry(total=2, backoff_factor=0.2, status_forcelist=[403, 429, 500, 502, 503, 504])
        try:
            adapter = HTTPAdapter(max_retries=retries, ssl_context=self.ssl_context)
        except TypeError:
            adapter = HTTPAdapter(max_retries=retries)
        s.mount('https://', adapter)
        s.mount('http://', adapter)
        self._requests_pool[key] = s
        return s

    async def get_html(self, url: str, referer: Optional[str] = None) -> Optional[str]:
        """
        Fetch HTML for a single URL, honoring robots, rate limiting, and proxy rotation.
        Returns HTML text on success, or None on failure.
        """
        # Ensure robots loaded; honor can_fetch and crawl-delay
        direct_sess = await self._ensure_direct_session()
        url = _sanitize_header_value(url)
        await self.robots.ensure(direct_sess, url)
        if not self.robots.can_fetch(url):
            logger.debug(f"robots.txt disallows: {url}")
            return None
        delay = self.robots.crawl_delay(url)
        host = urlparse(url).netloc
        if delay:
            st = self.host_limiter.state.setdefault(host, {"delay": self.host_limiter.base_delay, "last_ts": 0.0})
            st["delay"] = max(st["delay"], delay)

        # Per-host pacing
        await self.host_limiter.acquire(host)

        # Acquire a proxy (or direct)
        proxy_url, proxy_sem = await self.proxy_manager.acquire(host)
        t0 = time.monotonic()
        safe_ref = _sanitize_header_value(referer or url)
        hdrs = {'Referer': safe_ref}

        async with proxy_sem:
            try:
                # Select session and build request context
                if proxy_url is ProxyManager.DIRECT:
                    session = await self._ensure_direct_session()
                    rq = session.get(url, headers=hdrs, allow_redirects=True)
                else:
                    session = await self.proxy_manager.get_session(
                        proxy_url, self.ssl_context, self.default_headers, self.timeout, connector_limit=0
                    )
                    if _is_socks_proxy(proxy_url):
                        # SOCKS: connector routes the request; do NOT pass proxy=
                        rq = session.get(url, headers=hdrs, allow_redirects=True)
                    else:
                        # HTTP/HTTPS proxies: pass proxy per request
                        rq = session.get(url, proxy=proxy_url, headers=hdrs, allow_redirects=True)

                async with rq as resp:
                    ctype = (resp.headers.get('content-type') or '').lower()
                    if resp.status == 200 and ('html' in ctype or 'xhtml' in ctype):
                        # txt = await resp.text()
                        raw = await resp.read()  # always decode from bytes
                        enc = resp.charset  # aiohttp's charset from Content-Type (may be None)
                        if not enc:
                            try:
                                cn = from_bytes(raw).best()
                                enc = cn.encoding if cn and cn.encoding else 'utf-8'
                            except Exception:
                                enc = 'utf-8'

                        txt = raw.decode(enc, errors='replace')
                        self.host_limiter.on_success(host)
                        self.proxy_manager.report(host, proxy_url, resp.status, time.monotonic() - t0, None)
                        return txt

                    # Rate limit or block: back off and try one-shot fallback
                    if resp.status in (429, 403):
                        ra = parse_retry_after(resp.headers.get('Retry-After'))
                        self.host_limiter.on_backoff(host, ra)
                        self.proxy_manager.report(host, proxy_url, resp.status, time.monotonic() - t0, ra)
                        # Fallback via requests with same proxy (or direct)
                        html = await self._requests_fallback(url, referer, proxy_url)
                        if html:
                            return html

                    # Other non-200: back off a bit to avoid hammering
                    self.host_limiter.on_backoff(host, None)
                    self.proxy_manager.report(host, proxy_url, resp.status, time.monotonic() - t0, None)
                    return None

            except aiohttp.ClientError as e:
                logger.warning(f"Network error fetching {url}: {e}")
                self.host_limiter.on_backoff(host, None)
                self.proxy_manager.report(host, proxy_url, None, time.monotonic() - t0, None)
                # Fallback via requests with same proxy (or direct)
                html = await self._requests_fallback(url, referer, proxy_url)
                return html
            except Exception as e:
                logger.error(f"Error fetching {url}: {e}")
                self.host_limiter.on_backoff(host, None)
                self.proxy_manager.report(host, proxy_url, None, time.monotonic() - t0, None)
                return None

    async def _requests_fallback(self, url: str, referer: Optional[str], proxy_url: Optional[str]) -> Optional[str]:
        """
        One-shot fallback using requests
        Returns HTML or None.
        """
        safe_ref = _sanitize_header_value(referer or url)
        sess = self._get_requests_session_for_proxy(proxy_url)
        proxies = None if (proxy_url is ProxyManager.DIRECT or proxy_url is None) else {"http": proxy_url, "https": proxy_url}
        try:
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: sess.get(
                    url,
                    headers={'User-Agent': self.user_agent, 'Referer': safe_ref},
                    proxies=proxies,
                    timeout=8,
                    allow_redirects=True,
                )
            )
            if resp is not None and resp.status_code == 200 and 'html' in (resp.headers.get('content-type', '').lower()):
                # return resp.text
                raw = resp.content
                enc = resp.encoding  # requests’ guess from headers
                if not enc:
                    try:
                        cn = from_bytes(raw).best()
                        enc = cn.encoding if cn and cn.encoding else 'utf-8'
                    except Exception:
                        enc = 'utf-8'
                return raw.decode(enc, errors='replace')
        except Exception:
            pass
        return None

    async def shutdown(self):
        """Close direct aiohttp session, all proxy sessions, and any requests sessions."""
        try:
            if self._direct_session is not None:
                await self._direct_session.close()
                self._direct_session = None
        except Exception:
            pass
        try:
            await self.proxy_manager.close()
        except Exception:
            pass
        try:
            for s in self._requests_pool.values():
                try:
                    s.close()
                except Exception:
                    pass
            self._requests_pool.clear()
        except Exception:
            pass