"""
Listing scrapers for Zillow FSBO, Craigslist, Facebook Marketplace.
Targets For-Sale-By-Owner and distressed listings.
"""
import re
import logging
from datetime import datetime
from typing import Optional
from bs4 import BeautifulSoup
from .base_scraper import BaseScraper

logger = logging.getLogger(__name__)


class ZillowFSBOScraper(BaseScraper):
    """
    Scrape Zillow for FSBO (For Sale By Owner) listings.
    FSBO = no agent = more likely motivated seller = better deal.
    """

    async def scrape(self, cities: list[str] = None) -> list[dict]:
        cities = cities or ["Houston-TX", "San-Antonio-TX", "Dallas-TX",
                            "Fort-Worth-TX", "Austin-TX", "El-Paso-TX"]
        all_props = []
        for city in cities:
            try:
                props = await self._scrape_city(city)
                all_props.extend(props)
                logger.info(f"Zillow FSBO: {len(props)} listings in {city}")
            except Exception as e:
                logger.error(f"Error scraping Zillow FSBO {city}: {e}")
        return all_props

    async def _scrape_city(self, city: str) -> list[dict]:
        """Scrape FSBO listings for a Texas city."""
        properties = []

        # Zillow FSBO search URL pattern
        url = f"https://www.zillow.com/{city}/fsbo/"
        html = await self.fetch(url)
        if not html:
            return properties

        soup = BeautifulSoup(html, "html.parser")

        # Zillow renders data in JSON-LD and data attributes
        # Look for property cards
        cards = soup.find_all("article", {"data-test": re.compile(r"property-card")})
        if not cards:
            # Try alternative selectors
            cards = soup.find_all("div", class_=re.compile(r"list-card|property-card|StyledCard"))

        for card in cards:
            prop = self._parse_zillow_card(card, city)
            if prop:
                properties.append(prop)

        # Also try to extract from JSON data embedded in page
        scripts = soup.find_all("script", type="application/json")
        for script in scripts:
            try:
                import json
                data = json.loads(script.string or "")
                if isinstance(data, dict):
                    props_from_json = self._extract_from_zillow_json(data, city)
                    properties.extend(props_from_json)
            except (json.JSONDecodeError, TypeError):
                continue

        return properties

    def _parse_zillow_card(self, card, city: str) -> Optional[dict]:
        """Parse a Zillow property card."""
        try:
            # Address
            addr_el = card.find("address") or card.find(attrs={"data-test": "property-card-addr"})
            address = addr_el.get_text(strip=True) if addr_el else ""

            # Price
            price_el = card.find(attrs={"data-test": "property-card-price"})
            price = price_el.get_text(strip=True) if price_el else ""

            # Details (beds/baths/sqft)
            details = card.find_all("b") or card.find_all("span", class_=re.compile(r"detail"))

            # Link
            link = card.find("a", href=True)
            url = f"https://www.zillow.com{link['href']}" if link else ""

            if not address:
                return None

            city_name = city.replace("-TX", "").replace("-", " ")
            parts = address.rsplit(",", 2)

            return self.normalize_property({
                "street": parts[0].strip() if parts else address,
                "city": city_name,
                "state": "TX",
                "zip_code": parts[-1].strip() if len(parts) > 2 else "",
                "property_type": "single_family",
                "source": "zillow_fsbo",
                "source_url": url,
                "tax_assessed_value": price.replace("$", "").replace(",", "") if price else None,
            })
        except Exception:
            return None

    def _extract_from_zillow_json(self, data: dict, city: str) -> list[dict]:
        """Extract property data from Zillow's embedded JSON."""
        properties = []
        # Zillow embeds search results in various JSON structures
        results = []
        if "searchResults" in data:
            results = data["searchResults"].get("listResults", [])
        elif "cat1" in data:
            results = data["cat1"].get("searchResults", {}).get("listResults", [])

        city_name = city.replace("-TX", "").replace("-", " ")

        for r in results:
            try:
                prop = self.normalize_property({
                    "street": r.get("addressStreet", ""),
                    "city": r.get("addressCity", city_name),
                    "state": r.get("addressState", "TX"),
                    "zip_code": r.get("addressZipcode", ""),
                    "bedrooms": r.get("beds"),
                    "bathrooms": r.get("baths"),
                    "sqft": r.get("area"),
                    "property_type": r.get("propertyType", "single_family"),
                    "source": "zillow_fsbo",
                    "source_url": r.get("detailUrl", ""),
                    "tax_assessed_value": r.get("unformattedPrice"),
                })
                properties.append(prop)
            except Exception:
                continue

        return properties


class CraigslistScraper(BaseScraper):
    """
    Scrape Craigslist real estate listings.
    Great for motivated sellers - FSBO, desperate sellers, estate sales.
    """

    TEXAS_CITIES = {
        "houston": "https://houston.craigslist.org",
        "sanantonio": "https://sanantonio.craigslist.org",
        "dallas": "https://dallas.craigslist.org",
        "fortworth": "https://fortworth.craigslist.org",
        "austin": "https://austin.craigslist.org",
        "elpaso": "https://elpaso.craigslist.org",
        "mcallen": "https://mcallen.craigslist.org",
        "corpuschristi": "https://corpuschristi.craigslist.org",
        "killeen": "https://killeen.craigslist.org",
        "laredo": "https://laredo.craigslist.org",
        "lubbock": "https://lubbock.craigslist.org",
        "amarillo": "https://amarillo.craigslist.org",
        "brownsville": "https://brownsville.craigslist.org",
        "beaumont": "https://beaumont.craigslist.org",
        "waco": "https://waco.craigslist.org",
        "tyler": "https://tyler.craigslist.org",
        "odessa": "https://odessa.craigslist.org",
        "galveston": "https://galveston.craigslist.org",
        "collegestation": "https://collegestation.craigslist.org",
        "sanmarcos": "https://sanmarcos.craigslist.org",
        "wichitafalls": "https://wichitafalls.craigslist.org",
        "sanangelo": "https://sanangelo.craigslist.org",
        "victoriatx": "https://victoriatx.craigslist.org",
        "abilene": "https://abilene.craigslist.org",
        "texoma": "https://texoma.craigslist.org",
        "nacogdoches": "https://nacogdoches.craigslist.org",
    }

    # Keywords that signal motivated sellers
    MOTIVATED_KEYWORDS = [
        "must sell", "motivated", "urgent", "quick sale", "as-is", "as is",
        "below market", "estate sale", "divorce", "relocating", "foreclosure",
        "pre-foreclosure", "fixer upper", "fixer-upper", "handyman special",
        "needs work", "needs tlc", "investor special", "cash only",
        "owner financing", "no agents", "reduced", "price drop",
        # Spanish keywords
        "venta urgente", "debe vender", "motivado", "tal como está",
        "precio reducido", "especial para inversionista", "necesita trabajo",
        "financiamiento del dueño", "sin agentes",
    ]

    async def scrape(self, cities: list[str] = None) -> list[dict]:
        target_cities = cities or list(self.TEXAS_CITIES.keys())
        all_props = []
        for city in target_cities:
            if city in self.TEXAS_CITIES:
                try:
                    props = await self._scrape_city(city)
                    all_props.extend(props)
                    logger.info(f"Craigslist: {len(props)} motivated listings in {city}")
                except Exception as e:
                    logger.error(f"Error scraping Craigslist {city}: {e}")
        return all_props

    async def _scrape_city(self, city: str) -> list[dict]:
        """Scrape Craigslist real estate by owner listings."""
        properties = []
        base_url = self.TEXAS_CITIES[city]

        # Search real estate by owner section
        url = f"{base_url}/search/rea?hasPic=1&postedToday=0&pureMaps=0"
        html = await self.fetch(url)
        if not html:
            return properties

        soup = BeautifulSoup(html, "html.parser")
        listings = soup.find_all("li", class_="cl-static-search-result")
        if not listings:
            listings = soup.find_all("li", class_=re.compile(r"result-row|cl-search-result"))

        for listing in listings:
            prop = self._parse_listing(listing, city, base_url)
            if prop and self._is_motivated(prop.get("_title", "")):
                properties.append(prop)

        return properties

    def _parse_listing(self, listing, city: str, base_url: str) -> Optional[dict]:
        """Parse a Craigslist listing."""
        try:
            # Title
            title_el = listing.find("a", class_="titlestring") or listing.find("a")
            title = title_el.get_text(strip=True) if title_el else ""
            link = title_el.get("href", "") if title_el else ""
            if link and not link.startswith("http"):
                link = base_url + link

            # Price
            price_el = listing.find("span", class_="priceinfo") or listing.find("span", class_="result-price")
            price = price_el.get_text(strip=True) if price_el else ""

            # Location
            loc_el = listing.find("span", class_="result-hood") or listing.find("div", class_="location")
            location = loc_el.get_text(strip=True).strip("() ") if loc_el else city

            if not title:
                return None

            prop = self.normalize_property({
                "street": location or title[:50],
                "city": city.replace("sanantonio", "San Antonio").replace("fortworth", "Fort Worth")
                             .replace("elpaso", "El Paso").replace("mcallen", "McAllen")
                             .replace("corpuschristi", "Corpus Christi").replace("collegestation", "College Station")
                             .replace("sanmarcos", "San Marcos").replace("brownsville", "Brownsville")
                             .title(),
                "state": "TX",
                "source": "craigslist",
                "source_url": link,
                "tax_assessed_value": price.replace("$", "").replace(",", "") if price else None,
            })
            prop["_title"] = title.lower()  # Keep for motivated keyword check
            return prop
        except Exception:
            return None

    def _is_motivated(self, title: str) -> bool:
        """Check if listing title contains motivated seller keywords."""
        title_lower = title.lower()
        return any(kw in title_lower for kw in self.MOTIVATED_KEYWORDS)


class PreForeclosureScraper(BaseScraper):
    """
    Scrape pre-foreclosure data from public records.
    Texas posts foreclosure notices (Notice of Default / Notice of Trustee Sale)
    in county records and newspapers.
    """

    async def scrape(self, counties: list[str] = None) -> list[dict]:
        counties = counties or [
            "Harris", "Bexar", "Dallas", "Tarrant", "Travis",
            "El Paso", "Hidalgo", "Fort Bend", "Collin", "Denton",
            "Williamson", "Montgomery", "Brazoria", "Nueces", "Cameron",
            "Webb", "Lubbock",
        ]
        all_props = []
        for county in counties:
            try:
                props = await self._scrape_county(county)
                all_props.extend(props)
                logger.info(f"Pre-foreclosure: {len(props)} in {county} County")
            except Exception as e:
                logger.error(f"Error scraping pre-foreclosure {county}: {e}")
        return all_props

    async def _scrape_county(self, county: str) -> list[dict]:
        """Scrape pre-foreclosure notices from county records."""
        properties = []

        # Texas foreclosure postings - many counties use similar systems
        urls = [
            f"https://www.{county.lower()}countyclerk.com/foreclosures",
            f"https://www.county{county.lower()}.com/public-notices/foreclosure",
        ]

        for url in urls:
            html = await self.fetch(url)
            if not html:
                continue

            soup = BeautifulSoup(html, "html.parser")
            # Look for property listings in foreclosure notices
            notices = soup.find_all(["div", "tr", "article"],
                                    class_=re.compile(r"notice|foreclosure|listing|result"))

            for notice in notices:
                prop = self._parse_foreclosure_notice(notice, county)
                if prop:
                    properties.append(prop)

        return properties

    def _parse_foreclosure_notice(self, notice, county: str) -> Optional[dict]:
        """Parse a foreclosure notice for property info."""
        try:
            text = notice.get_text(" ", strip=True)
            # Try to extract address pattern
            addr_match = re.search(
                r'(\d+\s+[\w\s]+(?:St|Ave|Blvd|Dr|Ln|Rd|Ct|Way|Pl|Cir)\.?)',
                text, re.IGNORECASE
            )
            if not addr_match:
                return None

            return self.normalize_property({
                "street": addr_match.group(1).strip(),
                "county": county,
                "state": "TX",
                "pre_foreclosure": True,
                "source": f"{county.lower()}_foreclosure",
            })
        except Exception:
            return None
