#!/usr/bin/env python3
"""Scrape CBS, ABC and NBC News live pages with Playwright and build eye.m3u8 + eye.xml.gz.

For each channel page it loads the page in headless Chromium, then finds the
HLS (.m3u8) URL from the page's og:video meta tag and, failing that, from the
network requests the player makes. Output is sorted alphabetically by tvg-name.
"""

import gzip
import re
import sys
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape, quoteattr

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

M3U_FILE = "eye.m3u8"
EPG_FILE = "eye.xml.gz"
GROUP = "EN - News"
EPG_HOURS = 6  # matches the workflow schedule

CHANNELS = [
    ("CBS News 24/7", "https://www.cbsnews.com/live/"),
    ("CBS News New York", "https://www.cbsnews.com/newyork/live/"),
    ("CBS News Los Angeles", "https://www.cbsnews.com/losangeles/live/"),
    ("CBS News Chicago", "https://www.cbsnews.com/chicago/live/"),
    ("CBS News Texas", "https://www.cbsnews.com/texas/live/"),
    ("CBS News Philadelphia", "https://www.cbsnews.com/philadelphia/live/"),
    ("CBS News Bay Area", "https://www.cbsnews.com/sanfrancisco/live/"),
    ("CBS News Boston", "https://www.cbsnews.com/boston/live/"),
    ("CBS News Detroit", "https://www.cbsnews.com/detroit/live/"),
    ("CBS News Minnesota", "https://www.cbsnews.com/minnesota/live/"),
    ("CBS News Colorado", "https://www.cbsnews.com/colorado/live/"),
    ("CBS News Miami", "https://www.cbsnews.com/miami/live/"),
    ("CBS News Sacramento", "https://www.cbsnews.com/sacramento/live/"),
    ("CBS News Pittsburgh", "https://www.cbsnews.com/pittsburgh/live/"),
    ("CBS News Baltimore", "https://www.cbsnews.com/baltimore/live/"),
    ("ABC News Live", "https://abcnews.com/live/"),
    ("NBC New York", "https://www.nbcnews.com/new-york"),
    ("NBC Los Angeles", "https://www.nbcnews.com/los-angeles"),
    ("NBC Chicago", "https://www.nbcnews.com/chicago"),
    ("NBC Dallas-Fort Worth", "https://www.nbcnews.com/dallas-fort-worth"),
    ("NBC Philadelphia", "https://www.nbcnews.com/philadelphia"),
    ("NBC Washington", "https://www.nbcnews.com/washington"),
    ("NBC Boston", "https://www.nbcnews.com/boston"),
    ("NBC Bay Area", "https://www.nbcnews.com/bay-area"),
    ("NBC Miami", "https://www.nbcnews.com/miami"),
    ("NBC San Diego", "https://www.nbcnews.com/san-diego"),
    ("NBC Connecticut", "https://www.nbcnews.com/connecticut"),
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
M3U8_RE = re.compile(r"https?:(?:\\?/){2}[^\"'\s<>\\]+?\.m3u8[^\"'\s<>\\]*", re.I)
AD_HINT = re.compile(r"doubleclick|imasdk|adsystem|googlesyndication|/ads?/|freewheel|moatads|adnxs", re.I)
VARIANT_HINT = re.compile(r"chunk|media_|variant|\d{3,4}p|_\d+k", re.I)


def slugify(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", text) or "Channel"


def meta(page, prop: str) -> str:
    for attr in ("property", "name"):
        el = page.query_selector(f'meta[{attr}="{prop}"]')
        if el:
            val = (el.get_attribute("content") or "").strip()
            if val:
                return val
    return ""


def pick_stream(meta_url: str, seen: list, html: str) -> str:
    if ".m3u8" in meta_url:
        return meta_url
    # prefer a master playlist over a variant/chunk playlist
    masters = [u for u in seen if not VARIANT_HINT.search(u.rsplit("/", 1)[-1])]
    if masters:
        return masters[0]
    if seen:
        return seen[0]
    m = M3U8_RE.search(html)
    return m.group(0).replace("\\/", "/").replace("\\u002F", "/") if m else ""


def try_play(page):
    """Best-effort click on common play buttons / start muted video."""
    for sel in (
        'button[aria-label*="Play" i]',
        'button[title*="Play" i]',
        ".vjs-big-play-button",
        '[class*="play-button" i]',
        '[class*="PlayButton" i]',
    ):
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click(timeout=2000)
                return
        except Exception:
            pass
    try:
        page.evaluate("document.querySelectorAll('video').forEach(v => {v.muted = true; v.play().catch(()=>{})})")
    except Exception:
        pass


def scrape_channel(context, name: str, url: str):
    page = context.new_page()
    seen = []

    def on_request(req):
        if ".m3u8" in req.url and req.url not in seen and not AD_HINT.search(req.url):
            seen.append(req.url)

    page.on("request", on_request)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass
        # give the player a moment to request its playlist; nudge it if idle
        for i in range(15):
            if seen:
                break
            if i in (3, 8):
                try_play(page)
            page.wait_for_timeout(1000)

        html = page.content()
        stream = pick_stream(meta(page, "og:video:secure_url") or meta(page, "og:video:url"), seen, html)
        if not stream:
            print(f"skip {name}: no .m3u8 found", file=sys.stderr)
            return None

        h1 = page.query_selector("h1")
        program = (h1.inner_text().strip() if h1 else "").replace("\n", " ")
        if not program or len(program) > 80:
            program = name
        return {
            "name": name,
            "id": f"{slugify(name)}.us",
            "logo": meta(page, "og:image"),
            "stream": stream,
            "program": program,
        }
    except Exception as e:  # keep going if one channel fails
        print(f"skip {name}: {e}", file=sys.stderr)
        return None
    finally:
        page.close()


def build():
    entries = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"])
        context = browser.new_context(
            user_agent=UA,
            locale="en-US",
            viewport={"width": 1366, "height": 768},
        )
        for name, url in CHANNELS:
            print(f"Scraping {name} ...")
            item = scrape_channel(context, name, url)
            if item:
                entries.append(item)
        browser.close()

    if not entries:
        sys.exit("No streams extracted.")
    entries.sort(key=lambda e: e["name"].casefold())
    return entries


def write_m3u(entries):
    lines = ["#EXTM3U"]
    for e in entries:
        lines.append(
            f'#EXTINF:-1 tvg-id="{e["id"]}" tvg-name="{e["name"]}" '
            f'tvg-logo="{e["logo"]}" group-title="{GROUP}",{e["name"]}'
        )
        lines.append(e["stream"])
    with open(M3U_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_epg(entries):
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    fmt = "%Y%m%d%H%M%S +0000"
    start = now.strftime(fmt)
    stop = (now + timedelta(hours=EPG_HOURS)).strftime(fmt)

    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="eye">']
    for e in entries:
        xml.append(f"  <channel id={quoteattr(e['id'])}>")
        xml.append(f"    <display-name>{escape(e['name'])}</display-name>")
        if e["logo"]:
            xml.append(f"    <icon src={quoteattr(e['logo'])}/>")
        xml.append("  </channel>")
    for e in entries:
        xml.append(f'  <programme start="{start}" stop="{stop}" channel={quoteattr(e["id"])}>')
        xml.append(f'    <title lang="en">{escape(e["program"])}</title>')
        xml.append("  </programme>")
    xml.append("</tv>")

    with gzip.open(EPG_FILE, "wt", encoding="utf-8") as f:
        f.write("\n".join(xml) + "\n")


if __name__ == "__main__":
    items = build()
    write_m3u(items)
    write_epg(items)
    print(f"Wrote {len(items)} channels to {M3U_FILE} and {EPG_FILE}")
