"""
Base scraper with rate limiting, retries, rotating user agents, and anti-detection.
All scrapers inherit from this.
"""
import asyncio
import random
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional
import aiohttp
from config.settings import settings

logger = logging.getLogger(__name__)

# ── Rotating user agents — looks like different real browsers ──
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.144 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
]

# ── Rotating referers — look like traffic from different sources ──
REFERERS = [
    "https://www.google.com/",
    "https://www.google.com/search?q=texas+property+for+sale",
    "https://www.bing.com/",
    "https://www.facebook.com/",
    "https://www.google.com/search?q=foreclosure+homes+texas",
    "",  # Sometimes no referer is most natural
]


class BaseScraper(ABC):
    """Base class for all property scrapers."""

    def __init__(self, **kwargs):
        self.config = settings.scraping
        self.session: Optional[aiohttp.ClientSession] = None
        self._request_count = 0
        self._last_request_time = 0

    async def __aenter__(self):
        headers = self._random_headers()
        self.session = aiohttp.ClientSession(headers=headers)
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    def _random_headers(self) -> dict:
        """Generate random browser-like headers for each session."""
        ua = random.choice(USER_AGENTS)
        ref = random.choice(REFERERS)
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        }
        if ref:
            headers["Referer"] = ref
        return headers

    async def _rate_limit(self):
        """Respect rate limits with randomized human-like delays."""
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_request_time
        # Random delay between 2-6 seconds — mimics human browsing
        delay = random.uniform(2.0, 6.0)
        if elapsed < delay:
            await asyncio.sleep(delay - elapsed)
        self._last_request_time = asyncio.get_event_loop().time()

    async def fetch(self, url: str, **kwargs) -> Optional[str]:
        """Fetch a URL with rate limiting, retries, and rotating headers."""
        for attempt in range(self.config.max_retries):
            try:
                await self._rate_limit()

                # Rotate user agent on each retry
                if attempt > 0:
                    self.session._default_headers.update(
                        {"User-Agent": random.choice(USER_AGENTS)}
                    )

                async with self.session.get(url, **kwargs, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        self._request_count += 1
                        return await resp.text()
                    elif resp.status == 429:
                        wait = 60 * (attempt + 1) + random.uniform(10, 30)
                        logger.warning(f"Rate limited on {url}, waiting {wait:.0f}s")
                        await asyncio.sleep(wait)
                    elif resp.status == 403:
                        logger.warning(f"Blocked on {url}, rotating headers and waiting")
                        await asyncio.sleep(random.uniform(30, 60))
                    else:
                        logger.warning(f"HTTP {resp.status} on {url}")
                        await asyncio.sleep(random.uniform(5, 15))
            except asyncio.TimeoutError:
                logger.warning(f"Timeout on {url}, attempt {attempt + 1}")
                await asyncio.sleep(random.uniform(5, 10))
            except Exception as e:
                logger.error(f"Error fetching {url}: {e}")
                await asyncio.sleep(random.uniform(3, 8))
        return None

    async def fetch_json(self, url: str, **kwargs) -> Optional[dict]:
        """Fetch JSON data."""
        for attempt in range(self.config.max_retries):
            try:
                await self._rate_limit()
                async with self.session.get(url, **kwargs, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        return await resp.json()
            except Exception as e:
                logger.error(f"Error fetching JSON {url}: {e}")
                await asyncio.sleep(random.uniform(3, 8))
        return None

    @abstractmethod
    async def scrape(self) -> list[dict]:
        """Override in subclass. Returns list of property dicts."""
        pass

    def normalize_property(self, raw: dict) -> dict:
        """Normalize raw scraped data into standard property dict."""
        return {
            "street": raw.get("street", "").strip().title(),
            "city": raw.get("city", "").strip().title(),
            "county": raw.get("county", "").strip().title(),
            "state": raw.get("state", "TX").strip().upper(),
            "zip_code": str(raw.get("zip_code", "")).strip()[:10],
            "property_type": raw.get("property_type", "single_family"),
            "bedrooms": self._safe_int(raw.get("bedrooms")),
            "bathrooms": self._safe_float(raw.get("bathrooms")),
            "sqft": self._safe_int(raw.get("sqft")),
            "lot_size_sqft": self._safe_int(raw.get("lot_size_sqft")),
            "year_built": self._safe_int(raw.get("year_built")),
            "parcel_id": raw.get("parcel_id", "").strip(),
            "tax_assessed_value": self._safe_float(raw.get("tax_assessed_value")),
            "tax_delinquent": bool(raw.get("tax_delinquent", False)),
            "tax_delinquent_amount": self._safe_float(raw.get("tax_delinquent_amount")),
            "pre_foreclosure": bool(raw.get("pre_foreclosure", False)),
            "probate": bool(raw.get("probate", False)),
            "vacant": bool(raw.get("vacant", False)),
            "source": raw.get("source", "unknown"),
            "source_url": raw.get("source_url", ""),
            "scraped_at": datetime.utcnow(),
            # Owner info if available
            "owner_name": raw.get("owner_name", "").strip(),
            "owner_mailing_address": raw.get("owner_mailing_address", "").strip(),
        }

    @staticmethod
    def _safe_int(val) -> Optional[int]:
        if val is None:
            return None
        try:
            return int(str(val).replace(",", "").strip())
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _safe_float(val) -> Optional[float]:
        if val is None:
            return None
        try:
            return float(str(val).replace(",", "").replace("$", "").strip())
        except (ValueError, TypeError):
            return None
