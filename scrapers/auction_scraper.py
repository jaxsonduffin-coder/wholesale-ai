"""
Auction, HUD, Probate, Code Violation & Absentee Owner Scrapers.
Additional lead sources beyond tax delinquent and FSBO listings.
"""
import re
import logging
from datetime import datetime
from typing import Optional
from bs4 import BeautifulSoup
from .base_scraper import BaseScraper

logger = logging.getLogger(__name__)


class AuctionDotComScraper(BaseScraper):
    """
    Scrape Auction.com for foreclosure and bank-owned properties in Texas.
    These are highly motivated — banks want to sell fast.
    """

    async def scrape(self, cities: list[str] = None) -> list[dict]:
        cities = cities or [
            "Houston", "San Antonio", "Dallas", "Fort Worth", "Austin",
            "El Paso", "Arlington", "Corpus Christi", "Plano", "Laredo",
            "Lubbock", "Garland", "Irving", "Amarillo", "Grand Prairie",
            "Brownsville", "McKinney", "Frisco", "Pasadena", "Mesquite",
        ]
        all_props = []
        for city in cities:
            try:
                props = await self._scrape_city(city)
                all_props.extend(props)
                logger.info(f"Auction.com: {len(props)} properties in {city}")
            except Exception as e:
                logger.error(f"Error scraping Auction.com {city}: {e}")
        return all_props

    async def _scrape_city(self, city: str) -> list[dict]:
        """Scrape auction listings for a Texas city."""
        properties = []
        city_slug = city.lower().replace(" ", "-")

        # Auction.com search URL
        url = f"https://www.auction.com/residential/{city_slug}-tx/"
        html = await self.fetch(url)
        if not html:
            return properties

        soup = BeautifulSoup(html, "html.parser")

        # Parse property cards
        cards = soup.find_all("div", class_=re.compile(r"asset-card|property-card|listing"))
        for card in cards:
            prop = self._parse_card(card, city)
            if prop:
                properties.append(prop)

        # Try embedded JSON data
        scripts = soup.find_all("script", type="application/json")
        for script in scripts:
            try:
                import json
                data = json.loads(script.string or "")
                if isinstance(data, dict) and ("assets" in data or "properties" in data):
                    items = data.get("assets", data.get("properties", []))
                    if isinstance(items, list):
                        for item in items:
                            prop = self._parse_json_item(item, city)
                            if prop:
                                properties.append(prop)
            except Exception:
                continue

        return properties

    def _parse_card(self, card, city: str) -> Optional[dict]:
        try:
            addr_el = card.find(class_=re.compile(r"address|addr|location"))
            address = addr_el.get_text(strip=True) if addr_el else ""
            price_el = card.find(class_=re.compile(r"price|bid|amount"))
            price = price_el.get_text(strip=True) if price_el else ""
            link = card.find("a", href=True)
            url = link["href"] if link else ""
            if url and not url.startswith("http"):
                url = f"https://www.auction.com{url}"

            if not address:
                return None

            return self.normalize_property({
                "street": address.split(",")[0].strip() if "," in address else address,
                "city": city,
                "state": "TX",
                "pre_foreclosure": True,
                "source": "auction_com",
                "source_url": url,
                "tax_assessed_value": price.replace("$", "").replace(",", "") if price else None,
            })
        except Exception:
            return None

    def _parse_json_item(self, item: dict, city: str) -> Optional[dict]:
        try:
            return self.normalize_property({
                "street": item.get("address", item.get("streetAddress", "")),
                "city": item.get("city", city),
                "state": item.get("state", "TX"),
                "zip_code": item.get("zip", item.get("zipCode", "")),
                "bedrooms": item.get("beds", item.get("bedrooms")),
                "bathrooms": item.get("baths", item.get("bathrooms")),
                "sqft": item.get("sqft", item.get("squareFeet")),
                "pre_foreclosure": True,
                "source": "auction_com",
                "source_url": item.get("url", item.get("detailUrl", "")),
                "tax_assessed_value": item.get("currentBid", item.get("price")),
            })
        except Exception:
            return None


class HUDHomesScraper(BaseScraper):
    """
    Scrape HUD Homestore for government-owned foreclosures.
    HUD homes are sold below market — great wholesale opportunities.
    """

    async def scrape(self) -> list[dict]:
        properties = []

        # HUD Homestore Texas listings
        url = "https://www.hudhomestore.gov/Listing/PropertySearchResult"
        params = {
            "sState": "TX",
            "sLotSize": "",
            "iPageSize": "50",
            "iPage": "1",
        }

        html = await self.fetch(url, params=params)
        if not html:
            return properties

        soup = BeautifulSoup(html, "html.parser")
        rows = soup.find_all("tr", class_=re.compile(r"row|result|listing"))

        for row in rows:
            prop = self._parse_hud_row(row)
            if prop:
                properties.append(prop)

        # Also try their search results table
        table = soup.find("table", id=re.compile(r"result|listing"))
        if table:
            for row in table.find_all("tr")[1:]:
                prop = self._parse_hud_row(row)
                if prop:
                    properties.append(prop)

        logger.info(f"HUD Homes: {len(properties)} Texas properties")
        return properties

    def _parse_hud_row(self, row) -> Optional[dict]:
        try:
            cells = row.find_all("td")
            if len(cells) < 4:
                return None
            texts = [c.get_text(strip=True) for c in cells]
            link = row.find("a", href=True)
            url = ""
            if link:
                href = link.get("href", "")
                url = f"https://www.hudhomestore.gov{href}" if not href.startswith("http") else href

            return self.normalize_property({
                "street": texts[0] if texts else "",
                "city": texts[1] if len(texts) > 1 else "",
                "state": "TX",
                "zip_code": texts[2] if len(texts) > 2 else "",
                "tax_assessed_value": texts[3].replace("$", "").replace(",", "") if len(texts) > 3 else None,
                "source": "hud_homes",
                "source_url": url,
            })
        except Exception:
            return None


class ProbateScraper(BaseScraper):
    """
    Scrape Texas county probate records for inherited properties.
    Probate = inherited house someone may not want = motivated seller.
    """

    COUNTY_PROBATE_URLS = {
        "Harris": [
            "https://www.cclerk.hctx.net/Applications/WebSearch/CourtSearch.aspx",
        ],
        "Dallas": [
            "https://www.dallascounty.org/department/distclerk/probate.php",
        ],
        "Bexar": [
            "https://www.bexar.org/1568/Probate-Court",
        ],
        "Tarrant": [
            "https://www.tarrantcounty.com/en/county-clerk/probate.html",
        ],
        "Travis": [
            "https://www.traviscountyclerk.org/probate",
        ],
        "Collin": [
            "https://www.collincountytx.gov/county_clerk/Pages/probate.aspx",
        ],
        "Denton": [
            "https://www.dentoncounty.gov/553/Probate",
        ],
        "Fort Bend": [
            "https://www.fortbendcountytx.gov/government/county-offices/county-clerk/probate",
        ],
        "Williamson": [
            "https://www.wilco.org/Departments/County-Clerk/Probate",
        ],
        "Montgomery": [
            "https://www.mctx.org/departments/departments_a_-_c/county_clerk/probate_records.php",
        ],
    }

    async def scrape(self, counties: list[str] = None) -> list[dict]:
        counties = counties or list(self.COUNTY_PROBATE_URLS.keys())
        all_props = []
        for county in counties:
            if county not in self.COUNTY_PROBATE_URLS:
                continue
            try:
                props = await self._scrape_county(county)
                all_props.extend(props)
                logger.info(f"Probate: {len(props)} in {county} County")
            except Exception as e:
                logger.error(f"Error scraping probate {county}: {e}")
        return all_props

    async def _scrape_county(self, county: str) -> list[dict]:
        properties = []
        urls = self.COUNTY_PROBATE_URLS.get(county, [])

        for url in urls:
            html = await self.fetch(url)
            if not html:
                continue

            soup = BeautifulSoup(html, "html.parser")

            # Look for case listings, tables, or notices
            for el in soup.find_all(["tr", "div", "li"], class_=re.compile(r"case|record|result|row")):
                prop = self._parse_probate_entry(el, county)
                if prop:
                    properties.append(prop)

            # Also look for property address patterns in the full text
            text = soup.get_text(" ", strip=True)
            addresses = re.findall(
                r'(\d+\s+(?:[NSEW]\.?\s+)?[\w\s]+(?:St|Ave|Blvd|Dr|Ln|Rd|Ct|Way|Pl|Cir|Pkwy|Hwy)\.?)\s*,?\s*'
                r'([\w\s]+),?\s*TX\s*(\d{5})?',
                text, re.IGNORECASE
            )
            for addr_match in addresses[:20]:  # Limit to 20 per page
                prop = self.normalize_property({
                    "street": addr_match[0].strip(),
                    "city": addr_match[1].strip(),
                    "state": "TX",
                    "zip_code": addr_match[2] if len(addr_match) > 2 else "",
                    "county": county,
                    "probate": True,
                    "source": f"{county.lower()}_probate",
                    "source_url": url,
                })
                if prop.get("street"):
                    properties.append(prop)

        return properties

    def _parse_probate_entry(self, el, county: str) -> Optional[dict]:
        try:
            text = el.get_text(" ", strip=True)
            addr_match = re.search(
                r'(\d+\s+[\w\s]+(?:St|Ave|Blvd|Dr|Ln|Rd|Ct|Way|Pl|Cir)\.?)',
                text, re.IGNORECASE
            )
            if not addr_match:
                return None

            # Try to extract owner name (usually "Estate of XXXX" or "In re: XXXX")
            name_match = re.search(r'(?:estate\s+of|in\s+re:?)\s+([\w\s,]+?)(?:\s*[,;(]|\s*$)', text, re.IGNORECASE)
            owner = name_match.group(1).strip() if name_match else ""

            return self.normalize_property({
                "street": addr_match.group(1).strip(),
                "county": county,
                "state": "TX",
                "probate": True,
                "owner_name": owner,
                "source": f"{county.lower()}_probate",
            })
        except Exception:
            return None


class CodeViolationScraper(BaseScraper):
    """
    Scrape city code violation databases.
    Properties with code violations = neglected = often vacant = motivated seller.
    """

    CITY_CODE_URLS = {
        "Houston": "https://cohweb.houstontx.gov/ActiveCases/ActiveCases.aspx",
        "San Antonio": "https://webapp9.sanantonio.gov/CodeEnforcement/",
        "Dallas": "https://dallascityhall.com/departments/codecompliance/Pages/code-violations.aspx",
        "Fort Worth": "https://www.fortworthtexas.gov/departments/code-compliance",
        "Austin": "https://www.austintexas.gov/department/code-compliance",
        "El Paso": "https://www.elpasotexas.gov/code-enforcement/",
        "Arlington": "https://www.arlingtontx.gov/city_hall/departments/code_compliance",
        "Corpus Christi": "https://www.cctexas.com/departments/code-enforcement",
        "Plano": "https://www.plano.gov/193/Code-Compliance",
        "Laredo": "https://www.cityoflaredo.com/Building/code.html",
        "Lubbock": "https://www.mylubbock.us/departmental-websites/departments/code-enforcement",
        "Garland": "https://www.garlandtx.gov/436/Code-Compliance",
        "Irving": "https://www.cityofirving.org/612/Code-Enforcement",
        "Amarillo": "https://www.amarillo.gov/departments/community-development/code-enforcement",
        "Grand Prairie": "https://www.gptx.org/city-government/city-departments/development-services/code-enforcement",
        "Brownsville": "https://www.cob.us/222/Code-Enforcement",
        "McKinney": "https://www.mckinneytexas.org/163/Code-Compliance",
        "Frisco": "https://www.friscotexas.gov/141/Code-Enforcement",
        "Pasadena": "https://www.ci.pasadena.tx.us/code-enforcement",
        "Mesquite": "https://www.cityofmesquite.com/289/Code-Enforcement",
    }

    async def scrape(self, cities: list[str] = None) -> list[dict]:
        cities = cities or list(self.CITY_CODE_URLS.keys())
        all_props = []
        for city in cities:
            if city not in self.CITY_CODE_URLS:
                continue
            try:
                props = await self._scrape_city(city)
                all_props.extend(props)
                logger.info(f"Code violations: {len(props)} in {city}")
            except Exception as e:
                logger.error(f"Error scraping code violations {city}: {e}")
        return all_props

    async def _scrape_city(self, city: str) -> list[dict]:
        properties = []
        url = self.CITY_CODE_URLS.get(city, "")
        if not url:
            return properties

        html = await self.fetch(url)
        if not html:
            return properties

        soup = BeautifulSoup(html, "html.parser")

        # Look for case/violation tables
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            for row in rows[1:]:
                cells = row.find_all("td")
                if len(cells) >= 3:
                    prop = self._parse_violation_row(cells, city)
                    if prop:
                        properties.append(prop)

        # Look for violation entries in divs/lists
        for el in soup.find_all(["div", "li", "tr"], class_=re.compile(r"case|violation|complaint|record")):
            prop = self._parse_violation_entry(el, city)
            if prop:
                properties.append(prop)

        # Extract addresses from page text as fallback
        text = soup.get_text(" ", strip=True)
        addresses = re.findall(
            r'(\d+\s+(?:[NSEW]\.?\s+)?[\w\s]+(?:St|Ave|Blvd|Dr|Ln|Rd|Ct|Way|Pl|Cir|Pkwy)\.?)',
            text, re.IGNORECASE
        )
        seen = {p.get("street", "").lower() for p in properties}
        for addr in addresses[:30]:
            addr_clean = addr.strip()
            if addr_clean.lower() not in seen and len(addr_clean) > 10:
                properties.append(self.normalize_property({
                    "street": addr_clean,
                    "city": city,
                    "state": "TX",
                    "vacant": True,  # Code violations often indicate vacancy
                    "source": f"{city.lower().replace(' ', '_')}_code_violation",
                    "source_url": url,
                }))
                seen.add(addr_clean.lower())

        return properties

    def _parse_violation_row(self, cells, city: str) -> Optional[dict]:
        try:
            texts = [c.get_text(strip=True) for c in cells]
            # Look for the cell most likely to be an address
            for text in texts:
                if re.search(r'\d+\s+\w+\s+(?:St|Ave|Blvd|Dr|Ln|Rd)', text, re.IGNORECASE):
                    return self.normalize_property({
                        "street": text,
                        "city": city,
                        "state": "TX",
                        "vacant": True,
                        "source": f"{city.lower().replace(' ', '_')}_code_violation",
                    })
            return None
        except Exception:
            return None

    def _parse_violation_entry(self, el, city: str) -> Optional[dict]:
        try:
            text = el.get_text(" ", strip=True)
            addr_match = re.search(
                r'(\d+\s+[\w\s]+(?:St|Ave|Blvd|Dr|Ln|Rd|Ct|Way|Pl|Cir)\.?)',
                text, re.IGNORECASE
            )
            if not addr_match:
                return None
            return self.normalize_property({
                "street": addr_match.group(1).strip(),
                "city": city,
                "state": "TX",
                "vacant": True,
                "source": f"{city.lower().replace(' ', '_')}_code_violation",
            })
        except Exception:
            return None


class HomepathScraper(BaseScraper):
    """
    Scrape Fannie Mae HomePath for bank-owned (REO) properties.
    Bank-owned = motivated institutional seller = below market.
    """

    async def scrape(self) -> list[dict]:
        properties = []

        # HomePath API-style URL for Texas
        url = "https://www.homepath.com/listing/search"
        params = {
            "state": "TX",
            "pageSize": "50",
            "page": "1",
        }

        html = await self.fetch(url, params=params)
        if not html:
            # Try the browse URL
            url = "https://www.homepath.com/listing/browse/TX"
            html = await self.fetch(url)
            if not html:
                return properties

        soup = BeautifulSoup(html, "html.parser")

        # Parse listing cards
        cards = soup.find_all(class_=re.compile(r"listing|property|card"))
        for card in cards:
            try:
                addr_el = card.find(class_=re.compile(r"address|addr"))
                address = addr_el.get_text(strip=True) if addr_el else ""
                price_el = card.find(class_=re.compile(r"price"))
                price = price_el.get_text(strip=True) if price_el else ""
                link = card.find("a", href=True)
                card_url = link["href"] if link else ""
                if card_url and not card_url.startswith("http"):
                    card_url = f"https://www.homepath.com{card_url}"

                if address:
                    parts = address.split(",")
                    properties.append(self.normalize_property({
                        "street": parts[0].strip() if parts else address,
                        "city": parts[1].strip() if len(parts) > 1 else "",
                        "state": "TX",
                        "zip_code": parts[-1].strip() if len(parts) > 2 else "",
                        "source": "homepath",
                        "source_url": card_url,
                        "tax_assessed_value": price.replace("$", "").replace(",", "") if price else None,
                    }))
            except Exception:
                continue

        # Try embedded JSON
        for script in soup.find_all("script", type="application/json"):
            try:
                import json
                data = json.loads(script.string or "")
                if isinstance(data, dict):
                    listings = data.get("listings", data.get("properties", []))
                    if isinstance(listings, list):
                        for item in listings:
                            properties.append(self.normalize_property({
                                "street": item.get("address", ""),
                                "city": item.get("city", ""),
                                "state": "TX",
                                "zip_code": item.get("zip", ""),
                                "bedrooms": item.get("beds"),
                                "bathrooms": item.get("baths"),
                                "sqft": item.get("sqft"),
                                "source": "homepath",
                                "tax_assessed_value": item.get("listPrice"),
                            }))
            except Exception:
                continue

        logger.info(f"HomePath: {len(properties)} REO properties in Texas")
        return properties


class FreddieMacScraper(BaseScraper):
    """
    Scrape Freddie Mac HomeSteps for bank-owned properties.
    Another source of below-market bank-owned homes.
    """

    async def scrape(self) -> list[dict]:
        properties = []

        url = "https://www.homesteps.com/listings/search"
        params = {"state": "TX", "pageSize": "50"}

        html = await self.fetch(url, params=params)
        if not html:
            url = "https://www.homesteps.com/listings/TX"
            html = await self.fetch(url)
            if not html:
                return properties

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.find_all(class_=re.compile(r"listing|property|result"))

        for card in cards:
            try:
                addr_el = card.find(class_=re.compile(r"address"))
                if not addr_el:
                    continue
                address = addr_el.get_text(strip=True)
                parts = address.split(",")

                properties.append(self.normalize_property({
                    "street": parts[0].strip() if parts else address,
                    "city": parts[1].strip() if len(parts) > 1 else "",
                    "state": "TX",
                    "source": "homesteps",
                }))
            except Exception:
                continue

        logger.info(f"HomeSteps: {len(properties)} properties in Texas")
        return properties
