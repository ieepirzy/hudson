"""dtcdecode.com second-opinion DTC lookup.

Fetches the manufacturer-specific DTC definition from dtcdecode.com and
returns the short "Definition" string.  Results are cached in-memory for
the session so repeat scans don't hammer the site.

URL format: https://dtcdecode.com/{Make}/{CODE}
Spaces in make names are replaced with hyphens (e.g. "Land Rover" →
"Land-Rover").  The site issues a 301 redirect to www.DTCDecode.com which
httpx follows automatically.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# Canonical make names exactly as dtcdecode.com expects them in the URL.
MAKES: list[str] = [
    "Acura",
    "Alfa Romeo",
    "Audi",
    "BMW",
    "Buick",
    "Cadillac",
    "Chevrolet",
    "Chrysler",
    "Daewoo",
    "Dodge",
    "Eagle",
    "FIAT",
    "Ford",
    "Geo",
    "GMC",
    "Honda",
    "HUMMER",
    "Hyundai",
    "Infiniti",
    "Isuzu",
    "Jaguar",
    "Jeep",
    "Kia",
    "Land Rover",
    "Lexus",
    "Lincoln",
    "Mazda",
    "Mercedes-Benz",
    "Mercury",
    "MINI",
    "Mitsubishi",
    "Nissan",
    "Oldsmobile",
    "Plymouth",
    "Pontiac",
    "RAM",
    "Saab",
    "Saturn",
    "Scion",
    "Subaru",
    "Suzuki",
    "Toyota",
    "Volkswagen",
    "Volvo",
]

# Maps internal manufacturer_name → dtcdecode make, for unambiguous cases.
# Ambiguous entries (e.g. "VW/Audi") are intentionally absent so the user
# gets prompted to choose.
AUTO_MAKE_MAP: dict[str, str] = {
    "Ford": "Ford",
    "Toyota": "Toyota",
    "Volvo": "Volvo",
    "BMW": "BMW",
    "Mercedes-Benz": "Mercedes-Benz",
    "Honda": "Honda",
    "Chevrolet": "Chevrolet",
    "GMC": "GMC",
    "Dodge": "Dodge",
    "Chrysler": "Chrysler",
    "Jeep": "Jeep",
    "RAM": "RAM",
    "Nissan": "Nissan",
    "Hyundai": "Hyundai",
    "Kia": "Kia",
    "Mazda": "Mazda",
    "Subaru": "Subaru",
    "Mitsubishi": "Mitsubishi",
    "Suzuki": "Suzuki",
    "Infiniti": "Infiniti",
    "Lexus": "Lexus",
    "Acura": "Acura",
    "Lincoln": "Lincoln",
    "Buick": "Buick",
    "Cadillac": "Cadillac",
    "Saturn": "Saturn",
    "Pontiac": "Pontiac",
    "Oldsmobile": "Oldsmobile",
    "Mercury": "Mercury",
    "Isuzu": "Isuzu",
    "Jaguar": "Jaguar",
    "Land Rover": "Land Rover",
    "Volvo": "Volvo",
    "Saab": "Saab",
    "Scion": "Scion",
    "MINI": "MINI",
    "FIAT": "FIAT",
    "Alfa Romeo": "Alfa Romeo",
    "Audi": "Audi",
    "Volkswagen": "Volkswagen",
}

_MAX_CACHE = 1024
_cache: OrderedDict[tuple[str, str], str | None] = OrderedDict()
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            headers={"User-Agent": "Hudson OBD2 scanner/0.1"},
            follow_redirects=True,
            timeout=3.0,
        )
    return _client


def _make_url_slug(make: str) -> str:
    return make.replace(" ", "-")


def _parse_definition(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    dl = soup.find("dl", class_="dlResult")
    if not dl:
        return None
    items = dl.find_all(["dt", "dd"])
    for i, tag in enumerate(items):
        if tag.name == "dt" and tag.get_text(strip=True) == "Definition:":
            if i + 1 < len(items):
                text = items[i + 1].get_text(strip=True)
                return text if text else None
    return None


async def fetch_definition(make: str, code: str) -> str | None:
    """Fetch the short DTC definition from dtcdecode.com.

    Returns the definition string, or None if not found or on any error.
    Results are cached in-memory keyed by (make, code).
    """
    key = (make, code.upper())
    if key in _cache:
        return _cache[key]

    slug = _make_url_slug(make)
    url = f"https://dtcdecode.com/{slug}/{code.upper()}"
    try:
        resp = await _get_client().get(url)
        if resp.status_code == 404:
            if len(_cache) >= _MAX_CACHE:
                _cache.popitem(last=False)
            _cache[key] = None
            return None
        resp.raise_for_status()
        result = _parse_definition(resp.text)
        if len(_cache) >= _MAX_CACHE:
            _cache.popitem(last=False)
        _cache[key] = result
        return result
    except Exception:
        log.debug("dtcdecode.com lookup failed for %s/%s", make, code)
        return None
