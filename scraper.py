"""Scrape every Bellhaven community from the public website.

Crawls the paginated /communities directory, then visits each detail page for the
full address and care offerings. Also follows community links on the homepage,
because a newly announced location can appear there before it reaches the directory.

Output: data/website_locations.json
"""
import json
import os
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from config import BASE_URL, DATA_DIR

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "bellhaven-crm-sync/1.0"


def get_soup(path):
    resp = SESSION.get(urljoin(BASE_URL, path), timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def collect_community_links():
    """Return {slug: source} for every community link found in the directory and homepage."""
    links = {}
    page_path = "/communities"
    seen_pages = set()
    while page_path and page_path not in seen_pages:
        seen_pages.add(page_path)
        soup = get_soup(page_path)
        for a in soup.select(".card h3 a[href^='/communities/']"):
            links.setdefault(a["href"], "directory")
        nxt = next((a for a in soup.select(".pager a") if "Next" in a.get_text()), None)
        page_path = nxt["href"] if nxt else None

    home = get_soup("/")
    for a in home.select("a[href^='/communities/']"):
        links.setdefault(a["href"], "homepage")
    return links


def parse_detail(path):
    soup = get_soup(path)
    fields = {}
    for dt in soup.select("dl.detail dt"):
        dd = dt.find_next_sibling("dd")
        fields[dt.get_text(strip=True).lower()] = dd

    addr_lines = [s.strip() for s in fields["address"].stripped_strings]
    street = addr_lines[0]
    m = re.match(r"^(.*),\s*([A-Z]{2})\s+(\d{5})", addr_lines[-1])
    city, state, zip_code = (m.group(1), m.group(2), m.group(3)) if m else ("", "", "")

    return {
        "slug": path.rsplit("/", 1)[-1],
        "url": urljoin(BASE_URL, path),
        "name": soup.select_one("h1").get_text(strip=True),
        "street": street,
        "city": city,
        "state": state,
        "zip": zip_code,
        "care_offerings": [b.get_text(strip=True) for b in fields["care offerings"].select(".badge")],
        "administrator": fields.get("administrator").get_text(strip=True) if fields.get("administrator") else "",
        "phone": fields.get("phone").get_text(strip=True) if fields.get("phone") else "",
    }


def scrape():
    links = collect_community_links()
    locations = []
    for path, source in sorted(links.items()):
        loc = parse_detail(path)
        loc["found_on"] = source
        locations.append(loc)
    return locations


if __name__ == "__main__":
    locs = scrape()
    out = os.path.join(DATA_DIR, "website_locations.json")
    with open(out, "w") as f:
        json.dump(locs, f, indent=2)
    print(f"Scraped {len(locs)} locations -> {out}")
