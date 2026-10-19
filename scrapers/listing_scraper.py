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
    in county records and newspapers. Uses REAL county/state URLs.
    """

    # Real foreclosure listing URLs for Texas counties
    COUNTY_FORECLOSURE_URLS = {
        "Harris": [
            "https://www.hctax.net/Property/TaxSales",
            "https://www.harriscountytx.gov/Departments/Clerk/Foreclosures",
        ],
        "Dallas": [
            "https://www.dallascounty.org/departments/tax/delinquent-tax-sales.php",
            "https://www.dallascounty.org/departments/clerk/foreclosure-postings.php",
        ],
        "Tarrant": [
            "https://www.tarrantcounty.com/en/tax/tax-sale.html",
            "https://www.tarrantcounty.com/en/county-clerk/real-property/foreclosure-postings.html",
        ],
        "Bexar": [
            "https://www.bexar.org/3645/Tax-Sales",
            "https://www.bexar.org/1568/Foreclosure-Sales",
        ],
        "Travis": [
            "https://www.traviscountytx.gov/tax-assessor/delinquent-tax-sales",
            "https://www.traviscountytx.gov/county-clerk/real-property/foreclosure-postings",
        ],
        "El Paso": [
            "https://www.epcounty.com/taxoffice/taxsales.htm",
        ],
        "Hidalgo": [
            "https://www.hidalgocounty.us/292/Tax-Sales",
        ],
        "Fort Bend": [
            "https://www.fortbendcountytx.gov/government/departments/tax-assessor-collector/tax-sales",
        ],
        "Collin": [
            "https://www.collincountytx.gov/tax_assessor_collector/Pages/tax-sales.aspx",
        ],
        "Denton": [
            "https://www.dentoncounty.gov/669/Tax-Resales",
        ],
        "Williamson": [
            "https://www.wilco.org/Departments/Tax-Assessor-Collector/Tax-Sales",
        ],
        "Montgomery": [
            "https://www.mctx.org/departments/departments_a_-_c/county_tax_assessor-collector/tax_sales.php",
        ],
        "Nueces": [
            "https://www.nuecesco.com/county-services/tax-office/delinquent-tax-sales",
        ],
        "Cameron": [
            "https://www.cameroncounty.us/tax-office/delinquent-tax-sales/",
        ],
        "Lubbock": [
            "https://www.co.lubbock.tx.us/departments/tax-assessor-collector/tax-sales",
        ],
        "Galveston": [
            "https://www.galvestoncountytx.gov/our-county/tax-office/tax-sales",
        ],
        "McLennan": [
            "https://www.mclennancountytx.gov/285/Tax-Sales",
        ],
        "Bell": [
            "https://www.bellcountytx.com/county_government/tax_assessor_collector/tax_sales.php",
        ],
        "Midland": [
            "https://www.co.midland.tx.us/221/Tax-Sales",
        ],
        "Smith": [
            "https://www.smith-county.com/government/departments/tax-office/tax-sales",
        ],
        "Webb": [
            "https://www.webbcountytx.gov/TaxAssessor/TaxSales/",
        ],
    }

    # Statewide foreclosure aggregator sites
    STATEWIDE_URLS = [
        "https://www.foreclosure.com/listing/Texas",
        "https://www.texasforeclosures.com/listings",
        "https://www.realtytrac.com/mapsearch/texas/foreclosures/",
    ]

    async def scrape(self, counties: list[str] = None) -> list[dict]:
        counties = counties or list(self.COUNTY_FORECLOSURE_URLS.keys())
        all_props = []

        # Scrape county-specific URLs
        for county in counties:
            try:
                props = await self._scrape_county(county)
                all_props.extend(props)
                logger.info(f"Pre-foreclosure: {len(props)} in {county} County")
            except Exception as e:
                logger.error(f"Error scraping pre-foreclosure {county}: {e}")

        # Scrape statewide aggregator sites
        for url in self.STATEWIDE_URLS:
            try:
                props = await self._scrape_aggregator(url)
                all_props.extend(props)
                logger.info(f"Pre-foreclosure aggregator: {len(props)} from {url}")
            except Exception as e:
                logger.error(f"Error scraping aggregator {url}: {e}")

        return all_props

    async def _scrape_county(self, county: str) -> list[dict]:
        """Scrape pre-foreclosure notices from real county URLs."""
        properties = []
        urls = self.COUNTY_FORECLOSURE_URLS.get(county, [])

        for url in urls:
            html = await self.fetch(url)
            if not html:
                continue

            soup = BeautifulSoup(html, "html.parser")

            # Look for property data in tables (most county sites use tables)
            tables = soup.find_all("table")
            for table in tables:
                rows = table.find_all("tr")
                for row in rows:
                    prop = self._parse_table_row(row, county)
                    if prop:
                        properties.append(prop)

            # Also look for foreclosure notices in divs/articles/list items
            notices = soup.find_all(
                ["div", "tr", "article", "li", "p", "span"],
                class_=re.compile(
                    r"notice|foreclosure|listing|result|property|sale|delinquent|parcel",
                    re.IGNORECASE,
                ),
            )
            for notice in notices:
                prop = self._parse_foreclosure_notice(notice, county)
                if prop:
                    properties.append(prop)

            # Look for links to individual property/sale pages
            links = soup.find_all("a", href=True)
            for link in links:
                link_text = link.get_text(strip=True).lower()
                if any(kw in link_text for kw in ["property", "parcel", "sale", "foreclosure", "detail"]):
                    prop = self._parse_foreclosure_notice(link.parent, county)
                    if prop:
                        properties.append(prop)

        return properties

    async def _scrape_aggregator(self, url: str) -> list[dict]:
        """Scrape statewide foreclosure aggregator sites."""
        properties = []
        html = await self.fetch(url)
        if not html:
            return properties

        soup = BeautifulSoup(html, "html.parser")

        # Common patterns for aggregator sites
        cards = soup.find_all(
            ["div", "article", "li"],
            class_=re.compile(r"listing|property|result|card|item", re.IGNORECASE),
        )

        for card in cards:
            text = card.get_text(" ", strip=True)
            prop = self._extract_property_from_text(text, "aggregator")
            if prop:
                properties.append(prop)

        return properties

    def _parse_table_row(self, row, county: str) -> Optional[dict]:
        """Parse a table row for property info (common in county tax sale pages)."""
        try:
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                return None

            text = " ".join(c.get_text(strip=True) for c in cells)
            return self._extract_property_from_text(text, f"{county.lower()}_foreclosure")
        except Exception:
            return None

    def _extract_property_from_text(self, text: str, source: str) -> Optional[dict]:
        """Extract property info from any block of text."""
        if not text or len(text) < 10:
            return None

        # Try to extract address patterns
        addr_match = re.search(
            r'(\d+\s+[\w\s\.]+(?:St(?:reet)?|Ave(?:nue)?|Blvd|Boulevard|Dr(?:ive)?|'
            r'Ln|Lane|Rd|Road|Ct|Court|Way|Pl(?:ace)?|Cir(?:cle)?|Pkwy|Hwy|'
            r'Trl|Trail|Loop|Pass|Run|Path|Xing|Crossing)\.?)',
            text, re.IGNORECASE,
        )
        if not addr_match:
            return None

        # Try to find a city
        city_match = re.search(
            r'(?:Houston|San Antonio|Dallas|Fort Worth|Austin|El Paso|'
            r'Arlington|Corpus Christi|Plano|Laredo|Lubbock|Garland|'
            r'Irving|Amarillo|Grand Prairie|Brownsville|McKinney|'
            r'Frisco|Pasadena|McAllen|Killeen|Midland|Waco|'
            r'Beaumont|Round Rock|Odessa|Tyler|College Station|'
            r'Pearland|Sugar Land|Denton|Galveston|San Marcos)',
            text, re.IGNORECASE,
        )

        # Try to find a zip code
        zip_match = re.search(r'\b(7\d{4})\b', text)

        return self.normalize_property({
            "street": addr_match.group(1).strip(),
            "city": city_match.group(0) if city_match else "",
            "state": "TX",
            "zip_code": zip_match.group(1) if zip_match else "",
            "pre_foreclosure": True,
            "source": source,
        })

    def _parse_foreclosure_notice(self, notice, county: str) -> Optional[dict]:
        """Parse a foreclosure notice element for property info."""
        try:
            text = notice.get_text(" ", strip=True)
            return self._extract_property_from_text(text, f"{county.lower()}_foreclosure")
        except Exception:
            return None
