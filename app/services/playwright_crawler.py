"""
Playwright-based crawler for JavaScript-rendered pages
Optional enhancement - only used when explicitly requested
"""
from __future__ import annotations
import typing as t
import urllib.parse as up

# Optional Playwright support
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    _HAS_PLAYWRIGHT = True
except Exception:
    _HAS_PLAYWRIGHT = False


def crawl_with_playwright(
    url: str,
    wait_time: float = 3.0,
    timeout: int = 60000,
    max_pdfs: int | None = None
) -> dict:
    """
    Crawl a JavaScript-rendered page using Playwright.

    Args:
        url: The URL to crawl
        wait_time: Seconds to wait for JavaScript to render (default: 3)
        timeout: Page load timeout in milliseconds (default: 60000 = 60s)
        max_pdfs: Maximum number of PDFs to find (None = unlimited)

    Returns:
        dict with keys: found (int), urls (list[str]), source (str), ms (int), error (str|None)
    """
    if not _HAS_PLAYWRIGHT:
        return {
            "found": 0,
            "urls": [],
            "source": "playwright",
            "ms": 0,
            "error": "Playwright not installed. Run: pip install playwright && playwright install chromium"
        }

    import time
    import re

    started = time.time()
    pdfs: list[str] = []
    error: str | None = None

    try:
        with sync_playwright() as p:
            # Launch browser in headless mode
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            # Navigate and wait for network to be idle
            try:
                page.goto(url, wait_until="networkidle", timeout=timeout)
            except PlaywrightTimeout:
                # Continue anyway - some pages never fully idle
                pass

            # Wait for JavaScript to render
            time.sleep(wait_time)

            # Extract PDF links using multiple strategies
            pdf_pattern = re.compile(r"\.pdf($|[?#])", re.I)

            # Strategy 1: Direct PDF links
            pdf_links = page.locator('a[href*=".pdf"]').all()
            for link in pdf_links:
                try:
                    href = link.get_attribute("href")
                    if href:
                        absolute_url = up.urljoin(url, href)
                        if pdf_pattern.search(absolute_url):
                            pdfs.append(absolute_url)
                            if max_pdfs and len(pdfs) >= max_pdfs:
                                break
                except Exception:
                    continue

            # Strategy 2: Links with "form", "application", or "download" text
            if not max_pdfs or len(pdfs) < max_pdfs:
                form_links = page.locator('a:has-text("form"), a:has-text("application"), a[download]').all()
                for link in form_links:
                    try:
                        href = link.get_attribute("href")
                        if href and pdf_pattern.search(href):
                            absolute_url = up.urljoin(url, href)
                            if absolute_url not in pdfs:
                                pdfs.append(absolute_url)
                                if max_pdfs and len(pdfs) >= max_pdfs:
                                    break
                    except Exception:
                        continue

            # Strategy 3: iframe/embed sources
            if not max_pdfs or len(pdfs) < max_pdfs:
                for selector in ['iframe[src*=".pdf"]', 'embed[src*=".pdf"]', 'object[data*=".pdf"]']:
                    elements = page.locator(selector).all()
                    for elem in elements:
                        try:
                            src = elem.get_attribute("src") or elem.get_attribute("data")
                            if src and pdf_pattern.search(src):
                                absolute_url = up.urljoin(url, src)
                                if absolute_url not in pdfs:
                                    pdfs.append(absolute_url)
                                    if max_pdfs and len(pdfs) >= max_pdfs:
                                        break
                        except Exception:
                            continue

            # Deduplicate while preserving order
            seen = set()
            unique_pdfs = []
            for pdf in pdfs:
                if pdf not in seen:
                    seen.add(pdf)
                    unique_pdfs.append(pdf)

            browser.close()

    except Exception as e:
        error = str(e)

    elapsed_ms = int((time.time() - started) * 1000)

    return {
        "found": len(unique_pdfs),
        "urls": unique_pdfs,
        "source": "playwright",
        "ms": elapsed_ms,
        "error": error
    }


def is_playwright_available() -> bool:
    """Check if Playwright is installed and ready"""
    return _HAS_PLAYWRIGHT
