#!/usr/bin/env python3
"""
Apollo.io Scraper
Intercepts Apollo's internal API calls to export people or company search results.

Usage:
  python apollo_scraper.py --url "https://app.apollo.io/#/people?sortAscending=false" \
    --email user@example.com --password mypassword --output leads.csv

  # Via environment variables:
  APOLLO_EMAIL=user@example.com APOLLO_PASSWORD=mypassword \
    python apollo_scraper.py --url "https://app.apollo.io/#/people" --output leads.csv

  # JSON output:
  python apollo_scraper.py --url "https://app.apollo.io/#/companies" --output companies.json

  # Show browser window for debugging:
  python apollo_scraper.py --url "..." --show-browser
"""

import asyncio
import csv
import json
import os
import argparse
import sys
import math
from typing import Optional

try:
    from playwright.async_api import async_playwright, Page, BrowserContext
except ImportError:
    print("Error: playwright not installed.")
    print("Run: pip install playwright && playwright install chromium")
    sys.exit(1)

APOLLO_BASE = "https://app.apollo.io"

PEOPLE_API_PATH = "/api/v1/mixed_people/search"
COMPANIES_API_PATH = "/api/v1/mixed_companies/search"

PEOPLE_FIELDS = [
    "first_name",
    "last_name",
    "name",
    "title",
    "headline",
    "email",
    "sanitized_phone",
    "linkedin_url",
    "city",
    "state",
    "country",
    "organization_name",
    "organization_website_url",
    "organization_industry",
    "organization_num_employees_ranges",
    "organization_founded_year",
    "organization_linkedin_url",
]

COMPANY_FIELDS = [
    "name",
    "website_url",
    "linkedin_url",
    "industry",
    "num_employees",
    "annual_revenue_printed",
    "city",
    "state",
    "country",
    "short_description",
    "keywords",
    "founded_year",
]

MAX_PAGES = 45  # Apollo caps most plans at 45 pages (450 results per search)
PAGE_WAIT_MS = 2500


class ApolloScraper:
    def __init__(self, email: str, password: str, headless: bool = True):
        self.email = email
        self.password = password
        self.headless = headless
        self._records: list[dict] = []
        self._total_count: int = 0
        self._data_type: str = "people"
        self._page_size: int = 25

    # ------------------------------------------------------------------
    # Network interception
    # ------------------------------------------------------------------

    async def _on_response(self, response) -> None:
        url = response.url
        if PEOPLE_API_PATH not in url and COMPANIES_API_PATH not in url:
            return
        if response.status != 200:
            return

        try:
            data = await response.json()
        except Exception:
            return

        if PEOPLE_API_PATH in url:
            self._data_type = "people"
            records = data.get("people", []) + data.get("contacts", [])
        else:
            self._data_type = "companies"
            records = data.get("organizations", data.get("companies", []))

        pagination = data.get("pagination", {})
        if not self._total_count and pagination.get("total_entries"):
            self._total_count = pagination["total_entries"]
            self._page_size = pagination.get("per_page", 25)
            total_pages = math.ceil(self._total_count / max(self._page_size, 1))
            print(
                f"  Found {self._total_count} total records "
                f"({total_pages} pages of {self._page_size})"
            )

        self._records.extend(records)
        print(f"  Page captured: +{len(records)} records ({len(self._records)} total so far)")

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _login(self, page: Page) -> None:
        print("Navigating to Apollo.io login...")
        await page.goto(f"{APOLLO_BASE}/login", wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(2000)

        await page.fill('input[name="email"], input[type="email"]', self.email)
        await page.fill('input[name="password"], input[type="password"]', self.password)
        await page.click('button[type="submit"]')

        # Wait until URL changes away from /login
        try:
            await page.wait_for_function(
                "() => !window.location.href.includes('/login')",
                timeout=30_000,
            )
        except Exception:
            raise RuntimeError(
                "Login failed — check your credentials or complete any "
                "CAPTCHA / 2FA in --show-browser mode."
            )

        print("Login successful.")
        await page.wait_for_timeout(2000)

    # ------------------------------------------------------------------
    # Pagination helpers
    # ------------------------------------------------------------------

    async def _click_next(self, page: Page) -> bool:
        """Click the Next page button. Returns True if clicked."""
        selectors = [
            'button[data-cy="pagination-next"]:not([disabled])',
            '[aria-label="Next page"]:not([disabled])',
            'button:has(svg[data-icon="chevron-right"]):not([disabled])',
        ]
        for sel in selectors:
            try:
                btn = await page.query_selector(sel)
                if btn:
                    disabled = await btn.get_attribute("disabled")
                    aria_disabled = await btn.get_attribute("aria-disabled")
                    if not disabled and aria_disabled != "true":
                        await btn.click()
                        return True
            except Exception:
                continue
        return False

    async def _wait_for_page_load(self, page: Page, prev_count: int) -> None:
        """Wait until new records arrive or timeout."""
        for _ in range(20):
            await page.wait_for_timeout(500)
            if len(self._records) > prev_count:
                return
        # If still nothing after 10 s, continue anyway

    # ------------------------------------------------------------------
    # Main scrape loop
    # ------------------------------------------------------------------

    async def scrape(self, apollo_url: str) -> list[dict]:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            context: BrowserContext = await browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()
            page.on("response", self._on_response)

            await self._login(page)

            print(f"Navigating to search URL...")
            await page.goto(apollo_url, wait_until="domcontentloaded", timeout=60_000)

            # Initial page load — wait for first batch of records
            print("Page 1 — waiting for data...")
            await page.wait_for_timeout(PAGE_WAIT_MS)
            await self._wait_for_page_load(page, 0)

            page_num = 1
            while page_num < MAX_PAGES:
                prev = len(self._records)

                # Stop early if we already have everything
                if self._total_count and len(self._records) >= self._total_count:
                    print("All records captured.")
                    break

                clicked = await self._click_next(page)
                if not clicked:
                    print("No next page — done.")
                    break

                page_num += 1
                print(f"Page {page_num} — waiting for data...")
                await page.wait_for_timeout(PAGE_WAIT_MS)
                await self._wait_for_page_load(page, prev)

            await browser.close()

        print(f"\nScraping complete: {len(self._records)} records collected.")
        return self._records

    # ------------------------------------------------------------------
    # Export helpers
    # ------------------------------------------------------------------

    def _flatten(self, record: dict, fields: list[str]) -> dict:
        row = {}
        for field in fields:
            # Support nested keys like organization_name → organization.name
            val = record.get(field)
            if val is None:
                # Try splitting on underscore for one level of nesting
                parts = field.split("_", 1)
                nested = record.get(parts[0], {})
                if isinstance(nested, dict) and len(parts) > 1:
                    val = nested.get(parts[1])
            if isinstance(val, list):
                val = "; ".join(str(v) for v in val if v)
            row[field] = val if val is not None else ""
        return row

    def export_csv(self, path: str) -> None:
        fields = PEOPLE_FIELDS if self._data_type == "people" else COMPANY_FIELDS
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for record in self._records:
                writer.writerow(self._flatten(record, fields))
        print(f"Saved {len(self._records)} records → {path}")

    def export_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self._records, f, indent=2, default=str)
        print(f"Saved {len(self._records)} records → {path}")


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apollo.io scraper — export people or company search results to CSV/JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--url",
        required=True,
        help="Apollo.io search/list URL (copy from your browser address bar)",
    )
    parser.add_argument(
        "--email",
        default=os.environ.get("APOLLO_EMAIL"),
        help="Apollo.io account email (or set APOLLO_EMAIL env var)",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("APOLLO_PASSWORD"),
        help="Apollo.io account password (or set APOLLO_PASSWORD env var)",
    )
    parser.add_argument(
        "--output",
        default="apollo_export.csv",
        help="Output file (.csv or .json). Default: apollo_export.csv",
    )
    parser.add_argument(
        "--show-browser",
        action="store_true",
        help="Show the browser window (useful for debugging / 2FA)",
    )

    args = parser.parse_args()

    if not args.email or not args.password:
        parser.error(
            "Email and password are required.\n"
            "Use --email / --password flags or set APOLLO_EMAIL / APOLLO_PASSWORD env vars."
        )

    scraper = ApolloScraper(
        email=args.email,
        password=args.password,
        headless=not args.show_browser,
    )

    try:
        records = asyncio.run(scraper.scrape(args.url))
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)

    if not records:
        print("No records captured. Possible causes:")
        print("  • Wrong credentials")
        print("  • CAPTCHA / 2FA — try --show-browser")
        print("  • The URL is not a people/companies search page")
        sys.exit(1)

    if args.output.endswith(".json"):
        scraper.export_json(args.output)
    else:
        scraper.export_csv(args.output)


if __name__ == "__main__":
    main()
