"""
Base scraper with rate limiting, retries, and anti-detection.
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


class BaseScraper(ABC):
    """Base class for all property scrapers."""

    def __init__(self):
        self.config = settings.scraping
        self.session: Optional[aiohttp.ClientSession] = None
        self._request_count = 0
        self._last_request_time = 0

    async def __aenter__(self):
        headers = {
            "User-Agent": self.config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        }
        self.session = aiohttp.ClientSession(headers=headers)
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    async def _rate_limit(self):
        """Respect rate limits with randomized delays."""
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_request_time
        min_delay = self.config.delay_between_requests
        # Add jitter to look more human
        delay = min_delay + random.uniform(0.5, 2.0)
        if elapsed < delay:
            await asyncio.sleep(delay - elapsed)
        self._last_request_time = asyncio.get_event_loop().time()

    async def fetch(self, url: str, **kwargs) -> Optional[str]:
        """Fetch a URL with rate limiting and retries."""
        for attempt in range(self.config.max_retries):
            try:
                await self._rate_limit()
                async with self.session.get(url, **kwargs, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        self._request_count += 1
                        return await resp.text()
                    elif resp.status == 429:
                        wait = 60 * (attempt + 1)
                        logger.warning(f"Rate limited on {url}, waiting {wait}s")
                        await asyncio.sleep(wait)
                    elif resp.status == 403:
                        logger.warning(f"Blocked on {url}, rotating headers")
                        await asyncio.sleep(30)
                    else:
                        logger.warning(f"HTTP {resp.status} on {url}")
            except asyncio.TimeoutError:
                logger.warning(f"Timeout on {url}, attempt {attempt + 1}")
            except Exception as e:
                logger.error(f"Error fetching {url}: {e}")
                await asyncio.sleep(5)
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
                await asyncio.sleep(5)
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
