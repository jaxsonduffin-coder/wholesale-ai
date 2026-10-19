"""
Skip Tracing & Contact Finder.
Finds owner phone numbers, emails, and mailing addresses.
Uses free/cheap sources: TruePeopleSearch, FastPeopleSearch,
public records, MX validation, QuickEmailVerification.
"""
import re
import dns.resolver
import logging
import asyncio
from datetime import datetime
from typing import Optional
from bs4 import BeautifulSoup
from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)


class SkipTracer(BaseScraper):
    """Find contact info for property owners using free sources."""

    async def scrape(self) -> list[dict]:
        """Not used directly — use trace_owner instead."""
        return []

    async def trace_owner(self, name: str, city: str = "", state: str = "TX") -> dict:
        """
        Find contact info for a property owner.
        Returns: {phones: [], emails: [], addresses: [], preferred_language: 'en'|'es'}
        """
        result = {
            "phones": [],
            "emails": [],
            "addresses": [],
            "preferred_language": "en",
            "sources": [],
        }

        # Run all lookups in parallel
        tasks = [
            self._search_truepeoplesearch(name, city, state),
            self._search_fastpeoplesearch(name, city, state),
            self._guess_emails(name, city, state),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, dict):
                result["phones"].extend(r.get("phones", []))
                result["emails"].extend(r.get("emails", []))
                result["addresses"].extend(r.get("addresses", []))
                result["sources"].extend(r.get("sources", []))

        # Deduplicate
        result["phones"] = list(dict.fromkeys(result["phones"]))[:3]
        result["emails"] = list(dict.fromkeys(result["emails"]))[:3]
        result["addresses"] = list(dict.fromkeys(result["addresses"]))[:3]

        # Detect if Spanish-speaking (by name heuristic)
        result["preferred_language"] = self._detect_language(name)

        # Verify emails
        verified_emails = []
        for email in result["emails"]:
            if await self._verify_email(email):
                verified_emails.append(email)
        result["emails"] = verified_emails or result["emails"][:1]

        return result

    async def trace_by_address(self, street: str, city: str, state: str = "TX") -> dict:
        """
        Reverse lookup by property address when we don't have an owner name.
        Used for Craigslist and other leads where owner info isn't available.
        Returns: {name: str|None, phones: [], emails: [], preferred_language: 'en'|'es', sources: []}
        """
        result = {
            "name": None,
            "phones": [],
            "emails": [],
            "addresses": [],
            "preferred_language": "en",
            "sources": [],
        }

        # Try TruePeopleSearch address reverse lookup
        tps_result = await self._search_truepeoplesearch_address(street, city, state)
        if isinstance(tps_result, dict):
            if tps_result.get("name"):
                result["name"] = tps_result["name"]
            result["phones"].extend(tps_result.get("phones", []))
            result["emails"].extend(tps_result.get("emails", []))
            result["sources"].extend(tps_result.get("sources", []))

        # Try FastPeopleSearch address lookup
        fps_result = await self._search_fastpeoplesearch_address(street, city, state)
        if isinstance(fps_result, dict):
            if not result["name"] and fps_result.get("name"):
                result["name"] = fps_result["name"]
            result["phones"].extend(fps_result.get("phones", []))
            result["emails"].extend(fps_result.get("emails", []))
            result["sources"].extend(fps_result.get("sources", []))

        # Deduplicate
        result["phones"] = list(dict.fromkeys(result["phones"]))[:3]
        result["emails"] = list(dict.fromkeys(result["emails"]))[:3]

        # Detect language if we found a name
        if result["name"]:
            result["preferred_language"] = self._detect_language(result["name"])

        return result

    async def _search_truepeoplesearch_address(self, street: str, city: str, state: str) -> dict:
        """Reverse address lookup on TruePeopleSearch."""
        result = {"name": None, "phones": [], "emails": [], "sources": []}

        # Clean up street for URL
        street_clean = re.sub(r'[^\w\s]', '', street).strip()
        url = f"https://www.truepeoplesearch.com/resultaddress?streetaddress={street_clean.replace(' ', '+')}&citystatezip={city.replace(' ', '+')}+{state}"

        html = await self.fetch(url)
        if not html:
            return result

        soup = BeautifulSoup(html, "html.parser")

        # Get resident name from the first result card
        name_el = soup.find("div", class_=re.compile(r"card-title|name"))
        if name_el:
            name_link = name_el.find("a")
            if name_link:
                result["name"] = name_link.get_text(strip=True).title()

        # Phone numbers
        phone_links = soup.find_all("a", href=re.compile(r"tel:"))
        for link in phone_links:
            phone = re.sub(r'[^\d]', '', link.get_text(strip=True))
            if len(phone) == 10:
                result["phones"].append(phone)
            elif len(phone) == 11 and phone.startswith("1"):
                result["phones"].append(phone[1:])

        # Email addresses
        email_spans = soup.find_all("span", class_=re.compile(r"email"))
        for span in email_spans:
            email = span.get_text(strip=True)
            if "@" in email:
                result["emails"].append(email.lower())

        if result["name"] or result["phones"] or result["emails"]:
            result["sources"].append("truepeoplesearch_address")

        return result

    async def _search_fastpeoplesearch_address(self, street: str, city: str, state: str) -> dict:
        """Reverse address lookup on FastPeopleSearch."""
        result = {"name": None, "phones": [], "emails": [], "sources": []}

        street_slug = re.sub(r'[^\w\s]', '', street).strip().lower().replace(' ', '-')
        city_slug = city.lower().replace(' ', '-')

        url = f"https://www.fastpeoplesearch.com/address/{street_slug}_{city_slug}-{state.lower()}"
        html = await self.fetch(url)
        if not html:
            return result

        soup = BeautifulSoup(html, "html.parser")

        # Get resident name
        name_el = soup.find("h2", class_=re.compile(r"name|title"))
        if name_el:
            name_link = name_el.find("a")
            if name_link:
                result["name"] = name_link.get_text(strip=True).title()

        # Phone numbers
        phone_els = soup.find_all(text=re.compile(r'\(\d{3}\)\s*\d{3}-\d{4}'))
        for el in phone_els:
            phone = re.sub(r'[^\d]', '', el.strip())
            if len(phone) == 10:
                result["phones"].append(phone)

        # Email
        email_links = soup.find_all("a", href=re.compile(r"mailto:"))
        for link in email_links:
            email = link.get("href", "").replace("mailto:", "").strip()
            if "@" in email:
                result["emails"].append(email.lower())

        if result["name"] or result["phones"] or result["emails"]:
            result["sources"].append("fastpeoplesearch_address")

        return result

    async def _search_truepeoplesearch(self, name: str, city: str, state: str) -> dict:
        """Search TruePeopleSearch for contact info (free)."""
        result = {"phones": [], "emails": [], "addresses": [], "sources": []}

        # Format name for URL
        name_parts = name.strip().split()
        if len(name_parts) < 2:
            return result

        first = name_parts[0].lower()
        last = name_parts[-1].lower()

        url = f"https://www.truepeoplesearch.com/results?name={first}+{last}"
        if city:
            url += f"&citystatezip={city}+{state}"

        html = await self.fetch(url)
        if not html:
            return result

        soup = BeautifulSoup(html, "html.parser")

        # Phone numbers
        phone_links = soup.find_all("a", href=re.compile(r"tel:"))
        for link in phone_links:
            phone = link.get_text(strip=True)
            phone = re.sub(r'[^\d]', '', phone)
            if len(phone) == 10:
                result["phones"].append(phone)
            elif len(phone) == 11 and phone.startswith("1"):
                result["phones"].append(phone[1:])

        # Addresses
        addr_divs = soup.find_all("div", class_=re.compile(r"address"))
        for div in addr_divs:
            addr = div.get_text(strip=True)
            if addr and len(addr) > 10:
                result["addresses"].append(addr)

        # Email addresses
        email_spans = soup.find_all("span", class_=re.compile(r"email"))
        for span in email_spans:
            email = span.get_text(strip=True)
            if "@" in email:
                result["emails"].append(email.lower())

        if result["phones"] or result["emails"]:
            result["sources"].append("truepeoplesearch")

        return result

    async def _search_fastpeoplesearch(self, name: str, city: str, state: str) -> dict:
        """Search FastPeopleSearch for contact info (free)."""
        result = {"phones": [], "emails": [], "addresses": [], "sources": []}

        name_parts = name.strip().split()
        if len(name_parts) < 2:
            return result

        name_slug = "-".join(p.lower() for p in name_parts)
        location_slug = f"{city.lower().replace(' ', '-')}-{state.lower()}" if city else state.lower()

        url = f"https://www.fastpeoplesearch.com/name/{name_slug}_{location_slug}"
        html = await self.fetch(url)
        if not html:
            return result

        soup = BeautifulSoup(html, "html.parser")

        # Phone numbers - FastPeopleSearch shows them in detail cards
        phone_els = soup.find_all(text=re.compile(r'\(\d{3}\)\s*\d{3}-\d{4}'))
        for el in phone_els:
            phone = re.sub(r'[^\d]', '', el.strip())
            if len(phone) == 10:
                result["phones"].append(phone)

        # Email
        email_links = soup.find_all("a", href=re.compile(r"mailto:"))
        for link in email_links:
            email = link.get("href", "").replace("mailto:", "").strip()
            if "@" in email:
                result["emails"].append(email.lower())

        if result["phones"] or result["emails"]:
            result["sources"].append("fastpeoplesearch")

        return result

    async def _guess_emails(self, name: str, city: str, state: str) -> dict:
        """Generate possible email addresses from name patterns."""
        result = {"phones": [], "emails": [], "addresses": [], "sources": []}

        name_parts = name.strip().lower().split()
        if len(name_parts) < 2:
            return result

        first = name_parts[0]
        last = name_parts[-1]

        # Common email patterns - we'll verify these
        domains = ["gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com"]
        patterns = [
            f"{first}.{last}",
            f"{first}{last}",
            f"{first[0]}{last}",
            f"{first}_{last}",
            f"{last}{first}",
        ]

        candidates = []
        for pattern in patterns[:3]:  # Top 3 patterns
            for domain in domains[:3]:  # Top 3 domains
                candidates.append(f"{pattern}@{domain}")

        # Only add emails that pass MX check (domain has mail server)
        for email in candidates:
            domain = email.split("@")[1]
            if await self._check_mx(domain):
                result["emails"].append(email)

        if result["emails"]:
            result["sources"].append("email_guess")

        return result

    async def _verify_email(self, email: str) -> bool:
        """
        Verify an email address using layered approach:
        1. MX record check (free, instant)
        2. Format validation
        3. QuickEmailVerification API (100 free/day)
        """
        if not email or "@" not in email:
            return False

        # Basic format check
        if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email):
            return False

        # MX record check
        domain = email.split("@")[1]
        if not await self._check_mx(domain):
            return False

        # QuickEmailVerification (if API key configured)
        from config.settings import settings
        if settings.email.quickemail_api_key:
            try:
                url = f"https://api.quickemailverification.com/v1/verify"
                result = await self.fetch_json(url, params={
                    "email": email,
                    "apikey": settings.email.quickemail_api_key,
                })
                if result and result.get("result") == "valid":
                    return True
                elif result and result.get("result") == "invalid":
                    return False
            except Exception:
                pass

        # If we got past MX check, it's probably OK
        return True

    async def _check_mx(self, domain: str) -> bool:
        """Check if a domain has valid MX records."""
        try:
            loop = asyncio.get_event_loop()
            mx_records = await loop.run_in_executor(
                None, lambda: dns.resolver.resolve(domain, 'MX')
            )
            return len(list(mx_records)) > 0
        except Exception:
            return False

    def _detect_language(self, name: str) -> str:
        """
        Heuristic to detect if a property owner might prefer Spanish.
        Based on common Hispanic/Latino surnames.
        This is just a starting signal — the voice agent and SMS will
        offer both languages regardless.
        """
        hispanic_surnames = {
            "garcia", "rodriguez", "martinez", "hernandez", "lopez",
            "gonzalez", "perez", "sanchez", "ramirez", "torres",
            "flores", "rivera", "gomez", "diaz", "reyes", "cruz",
            "morales", "ortiz", "gutierrez", "chavez", "ramos",
            "vargas", "castillo", "jimenez", "moreno", "romero",
            "alvarez", "ruiz", "mendoza", "aguilar", "medina",
            "castro", "guzman", "salazar", "delgado", "soto",
            "contreras", "vazquez", "fernandez", "estrada", "herrera",
            "carrillo", "silva", "sandoval", "guerrero", "campos",
            "nunez", "leon", "vega", "dominguez", "acosta",
        }

        name_parts = name.lower().strip().split()
        if any(part in hispanic_surnames for part in name_parts):
            return "es"
        return "en"
