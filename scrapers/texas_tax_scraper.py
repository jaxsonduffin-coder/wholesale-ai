"""
Texas County Tax Delinquent & Property Scraper.
Scrapes county tax assessor sites for delinquent properties.
Each Texas county has its own CAD (Central Appraisal District).
"""
import re
import logging
from datetime import datetime
from typing import Optional
from bs4 import BeautifulSoup
from .base_scraper import BaseScraper

logger = logging.getLogger(__name__)

# Texas county appraisal district search URLs
TEXAS_CAD_URLS = {
    "Harris": {
        "search": "https://public.hcad.org/records/quicksearch.asp",
        "name": "Harris County Appraisal District",
        "platform": "hcad",
    },
    "Bexar": {
        "search": "https://bexar.trueautomation.com/clientdb/PropertySearch.aspx",
        "name": "Bexar County Appraisal District",
        "platform": "trueautomation",
    },
    "Dallas": {
        "search": "https://www.dallascad.org/SearchAddr.aspx",
        "name": "Dallas Central Appraisal District",
        "platform": "dallascad",
    },
    "Tarrant": {
        "search": "https://www.tad.org/property-search",
        "name": "Tarrant Appraisal District",
        "platform": "tad",
        "city": "Fort Worth",
        "zips": ["76101", "76102", "76103", "76104", "76105", "76106", "76107",
                 "76108", "76109", "76110", "76111", "76112", "76115", "76116",
                 "76117", "76118", "76119", "76120", "76123", "76126", "76131",
                 "76132", "76133", "76134", "76135", "76137", "76140", "76148",
                 "76155", "76164", "76177", "76179", "76244", "76248"],
    },
    "Travis": {
        "search": "https://www.traviscad.org/property-search",
        "name": "Travis Central Appraisal District",
        "platform": "traviscad",
        "city": "Austin",
        "zips": ["78701", "78702", "78703", "78704", "78705", "78721", "78722",
                 "78723", "78724", "78725", "78726", "78727", "78728", "78729",
                 "78730", "78731", "78732", "78733", "78734", "78735", "78736",
                 "78737", "78738", "78739", "78741", "78742", "78744", "78745",
                 "78746", "78747", "78748", "78749", "78750", "78751", "78752",
                 "78753", "78754", "78756", "78757", "78758", "78759"],
    },
    "El Paso": {
        "search": "https://www.epcad.org/Search",
        "name": "El Paso Central Appraisal District",
        "platform": "epcad",
        "city": "El Paso",
        "zips": ["79901", "79902", "79903", "79904", "79905", "79907", "79911",
                 "79912", "79915", "79922", "79924", "79925", "79927", "79928",
                 "79930", "79932", "79934", "79935", "79936", "79938"],
    },
    "Hidalgo": {
        "search": "https://www.hidalgoad.org/",
        "name": "Hidalgo County Appraisal District",
        "platform": "hidalgoad",
        "city": "McAllen",
        "zips": ["78501", "78503", "78504", "78516", "78539", "78541",
                 "78572", "78573", "78577", "78589"],
    },
    "Fort Bend": {
        "search": "https://www.fbcad.org/",
        "name": "Fort Bend Central Appraisal District",
        "platform": "fbcad",
        "city": "Sugar Land",
        "zips": ["77459", "77469", "77478", "77479", "77489", "77498",
                 "77406", "77407", "77441", "77461", "77471", "77477",
                 "77494", "77545", "77583"],
    },
    "Collin": {
        "search": "https://www.collincad.org/",
        "name": "Collin Central Appraisal District",
        "platform": "collincad",
        "city": "Plano",
        "zips": ["75002", "75009", "75013", "75023", "75024", "75025",
                 "75034", "75035", "75048", "75069", "75070", "75071",
                 "75074", "75075", "75078", "75080", "75082", "75093",
                 "75094", "75252"],
    },
    "Denton": {
        "search": "https://www.dentoncad.com/",
        "name": "Denton Central Appraisal District",
        "platform": "dentoncad",
        "city": "Denton",
        "zips": ["76201", "76205", "76207", "76208", "76209", "76210",
                 "76226", "76227", "76234", "76247", "76249", "76258",
                 "76259", "76262", "76266", "75007", "75010", "75019",
                 "75028", "75056", "75057", "75065", "75067", "75068",
                 "75077"],
    },
    "Williamson": {
        "search": "https://www.wcad.org/",
        "name": "Williamson Central Appraisal District",
        "platform": "wcad",
        "city": "Round Rock",
        "zips": ["78613", "78628", "78634", "78641", "78664", "78665",
                 "78681", "76527", "76537", "76574"],
    },
    "Montgomery": {
        "search": "https://www.mcad-tx.org/",
        "name": "Montgomery Central Appraisal District",
        "platform": "mcad",
        "city": "Conroe",
        "zips": ["77301", "77302", "77303", "77304", "77306", "77316",
                 "77318", "77354", "77356", "77362", "77365", "77372",
                 "77378", "77380", "77381", "77382", "77384", "77385",
                 "77386", "77389"],
    },
    "Brazoria": {
        "search": "https://www.brazoriacad.org/",
        "name": "Brazoria County Appraisal District",
        "platform": "brazoriacad",
        "city": "Pearland",
        "zips": ["77566", "77581", "77584", "77515", "77531", "77534",
                 "77541", "77578", "77583"],
    },
    "Nueces": {
        "search": "https://www.nuecescad.net/",
        "name": "Nueces County Appraisal District",
        "platform": "nuecescad",
        "city": "Corpus Christi",
        "zips": ["78401", "78402", "78404", "78405", "78408", "78410",
                 "78411", "78412", "78413", "78414", "78415", "78416",
                 "78417", "78418"],
    },
    "Cameron": {
        "search": "https://www.cameroncad.org/",
        "name": "Cameron County Appraisal District",
        "platform": "cameroncad",
        "city": "Brownsville",
        "zips": ["78520", "78521", "78526", "78550", "78552", "78559",
                 "78566", "78575", "78578", "78586"],
    },
    "Webb": {
        "search": "https://www.webbcad.org/",
        "name": "Webb County Appraisal District",
        "platform": "webbcad",
        "city": "Laredo",
        "zips": ["78040", "78041", "78043", "78045", "78046"],
    },
    "Lubbock": {
        "search": "https://www.lubbockcad.org/",
        "name": "Lubbock Central Appraisal District",
        "platform": "lubbockcad",
        "city": "Lubbock",
        "zips": ["79401", "79403", "79404", "79407", "79410", "79411",
                 "79412", "79413", "79414", "79415", "79416", "79423",
                 "79424"],
    },
}


class TexasTaxScraper(BaseScraper):
    """
    Scrapes Texas county tax delinquent lists.
    Most Texas counties publish delinquent tax lists as PDFs or web pages.
    We also scrape property details from CAD sites.
    """

    def __init__(self, counties: list[str] = None):
        super().__init__()
        self.counties = counties or ["Harris", "Bexar", "Dallas"]

    async def scrape(self) -> list[dict]:
        """Scrape all target counties for distressed properties."""
        all_properties = []
        for county in self.counties:
            logger.info(f"Scraping {county} County tax records...")
            try:
                props = await self._scrape_county(county)
                all_properties.extend(props)
                logger.info(f"Found {len(props)} properties in {county} County")
            except Exception as e:
                logger.error(f"Error scraping {county}: {e}")
        return all_properties

    async def _scrape_county(self, county: str) -> list[dict]:
        """Scrape a single county's tax delinquent properties."""
        properties = []

        # Strategy 1: Check for published delinquent tax lists
        delinquent = await self._scrape_delinquent_list(county)
        properties.extend(delinquent)

        # Strategy 2: Search CAD for high-value indicators
        # (owner != mailing address = possible absentee owner = motivated seller)
        cad_results = await self._search_cad(county)
        properties.extend(cad_results)

        return properties

    async def _scrape_delinquent_list(self, county: str) -> list[dict]:
        """
        Scrape the county's delinquent tax list.
        Most Texas counties publish these on their tax assessor-collector website.
        """
        properties = []

        # Common URL patterns for Texas county delinquent lists
        delinquent_urls = [
            f"https://www.{county.lower()}countytx.gov/tax-sales",
            f"https://www.{county.lower()}county.org/delinquent-taxes",
            f"https://tax.{county.lower()}countytx.gov/delinquent",
        ]

        for url in delinquent_urls:
            html = await self.fetch(url)
            if not html:
                continue

            soup = BeautifulSoup(html, "html.parser")

            # Look for tables with property data
            tables = soup.find_all("table")
            for table in tables:
                rows = table.find_all("tr")
                for row in rows[1:]:  # Skip header
                    cells = row.find_all(["td", "th"])
                    if len(cells) >= 3:
                        prop = self._parse_delinquent_row(cells, county)
                        if prop:
                            properties.append(prop)

            # Also look for downloadable lists (CSV/Excel links)
            links = soup.find_all("a", href=True)
            for link in links:
                href = link["href"].lower()
                if any(ext in href for ext in [".csv", ".xlsx", ".xls"]):
                    logger.info(f"Found downloadable list: {href}")
                    # TODO: Download and parse spreadsheet files

        return properties

    async def _search_cad(self, county: str) -> list[dict]:
        """
        Search County Appraisal District for properties.
        Look for distress signals: high tax-to-value ratio, absentee owners.
        """
        properties = []
        cad_info = TEXAS_CAD_URLS.get(county)
        if not cad_info:
            return properties

        # For Harris County (largest in TX), use their API-like search
        if county == "Harris":
            properties = await self._scrape_harris_cad()
        elif county == "Bexar":
            properties = await self._scrape_bexar_cad()
        elif county == "Dallas":
            properties = await self._scrape_dallas_cad()

        return properties

    async def _scrape_harris_cad(self) -> list[dict]:
        """Scrape Harris County Appraisal District (Houston area)."""
        properties = []

        # HCAD has a searchable database. We search by neighborhood/zip
        # to find properties with distress indicators
        houston_zips = [
            "77002", "77003", "77004", "77005", "77006", "77007", "77008",
            "77009", "77010", "77011", "77012", "77013", "77015", "77016",
            "77017", "77018", "77019", "77020", "77021", "77022", "77023",
            "77024", "77025", "77026", "77027", "77028", "77029", "77030",
            "77033", "77034", "77035", "77036", "77037", "77038", "77039",
            "77040", "77041", "77042", "77043", "77044", "77045", "77047",
            "77048", "77049", "77050", "77051", "77053", "77054", "77055",
            "77056", "77057", "77058", "77059", "77060", "77061", "77062",
        ]

        for zip_code in houston_zips[:10]:  # Start with first 10 zips
            url = f"https://public.hcad.org/records/quicksearch.asp"
            html = await self.fetch(url, params={"search_type": "zip", "search_value": zip_code})
            if not html:
                continue

            soup = BeautifulSoup(html, "html.parser")
            # Parse HCAD results - each result has account number, address, owner
            results = soup.find_all("tr", class_=re.compile(r"row|result|record"))
            for result in results:
                prop = self._parse_hcad_result(result, zip_code)
                if prop:
                    properties.append(prop)

        return properties

    async def _scrape_bexar_cad(self) -> list[dict]:
        """Scrape Bexar County (San Antonio area)."""
        properties = []
        # Bexar uses TrueAutomation platform
        sa_zips = ["78201", "78202", "78203", "78204", "78205",
                   "78207", "78208", "78210", "78211", "78212",
                   "78213", "78214", "78215", "78216", "78217"]

        for zip_code in sa_zips[:10]:
            url = "https://bexar.trueautomation.com/clientdb/PropertySearch.aspx"
            html = await self.fetch(url, params={"ZipCode": zip_code})
            if html:
                props = self._parse_trueautomation_results(html, "Bexar", zip_code)
                properties.extend(props)

        return properties

    async def _scrape_dallas_cad(self) -> list[dict]:
        """Scrape Dallas Central Appraisal District."""
        properties = []
        dallas_zips = ["75201", "75202", "75203", "75204", "75205",
                       "75206", "75207", "75208", "75209", "75210",
                       "75211", "75212", "75214", "75215", "75216"]

        for zip_code in dallas_zips[:10]:
            url = f"https://www.dallascad.org/SearchAddr.aspx"
            html = await self.fetch(url, params={"zipcode": zip_code})
            if html:
                props = self._parse_dallas_cad_results(html, zip_code)
                properties.extend(props)

        return properties

    def _parse_delinquent_row(self, cells: list, county: str) -> Optional[dict]:
        """Parse a row from a delinquent tax table."""
        try:
            texts = [cell.get_text(strip=True) for cell in cells]
            if len(texts) < 3:
                return None

            # Common format: Account#, Owner, Address, Amount Owed
            return self.normalize_property({
                "parcel_id": texts[0],
                "owner_name": texts[1] if len(texts) > 1 else "",
                "street": texts[2] if len(texts) > 2 else "",
                "city": "",
                "county": county,
                "state": "TX",
                "tax_delinquent": True,
                "tax_delinquent_amount": texts[-1] if len(texts) > 3 else "0",
                "source": f"{county.lower()}_county_tax",
                "source_url": f"https://www.{county.lower()}countytx.gov/tax-sales",
            })
        except Exception:
            return None

    def _parse_hcad_result(self, row, zip_code: str) -> Optional[dict]:
        """Parse a Harris County CAD search result row."""
        try:
            cells = row.find_all("td")
            if len(cells) < 4:
                return None

            texts = [c.get_text(strip=True) for c in cells]
            return self.normalize_property({
                "parcel_id": texts[0],
                "owner_name": texts[1],
                "street": texts[2],
                "city": "Houston",
                "county": "Harris",
                "state": "TX",
                "zip_code": zip_code,
                "tax_assessed_value": texts[3] if len(texts) > 3 else None,
                "source": "hcad",
                "source_url": "https://public.hcad.org",
            })
        except Exception:
            return None

    def _parse_trueautomation_results(self, html: str, county: str, zip_code: str) -> list[dict]:
        """Parse TrueAutomation platform results (used by Bexar, others)."""
        properties = []
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.find_all("tr", class_=re.compile(r"DataRow|row"))
        for row in rows:
            cells = row.find_all("td")
            if len(cells) >= 4:
                texts = [c.get_text(strip=True) for c in cells]
                prop = self.normalize_property({
                    "parcel_id": texts[0],
                    "owner_name": texts[1],
                    "street": texts[2],
                    "city": "San Antonio" if county == "Bexar" else "",
                    "county": county,
                    "state": "TX",
                    "zip_code": zip_code,
                    "source": f"{county.lower()}_cad",
                })
                properties.append(prop)
        return properties

    def _parse_dallas_cad_results(self, html: str, zip_code: str) -> list[dict]:
        """Parse Dallas CAD search results."""
        properties = []
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.find_all("tr")
        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) >= 3:
                texts = [c.get_text(strip=True) for c in cells]
                prop = self.normalize_property({
                    "parcel_id": texts[0],
                    "owner_name": texts[1] if len(texts) > 1 else "",
                    "street": texts[2] if len(texts) > 2 else "",
                    "city": "Dallas",
                    "county": "Dallas",
                    "state": "TX",
                    "zip_code": zip_code,
                    "source": "dallas_cad",
                })
                properties.append(prop)
        return properties
