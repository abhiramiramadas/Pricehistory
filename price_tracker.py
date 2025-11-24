# improved price_tracker.py
# Upgraded price extraction: better headers, site-specific selectors, validation, and logging.
import requests
from bs4 import BeautifulSoup
import re
import sqlite3
import time
import json
from datetime import datetime
import os
import random

# ---------- Config ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

DB_PATH = "prices.db"
PRODUCTS_FILE = "products.json"

# Price sanity bounds (phones). Adjust if you want other ranges.
MIN_REASONABLE_PRICE = 1000
MAX_REASONABLE_PRICE = 200000

# A small list of desktop user agents to reduce bot detection
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
    " Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/537.36 (KHTML, like Gecko)"
    " Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko)"
    " Chrome/120.0.0.0 Safari/537.36",
]

COMMON_HEADERS = {
    # Note: User-Agent will be set per-request from USER_AGENTS
    "Accept-Language": "en-US,en;q=0.9,en-IN;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Connection": "keep-alive",
    "Referer": "https://www.google.com/",
}

# ---------- DB ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS price_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT,
        site TEXT,
        product_name TEXT,
        price INTEGER,
        checked_at TEXT
    )
    """)
    conn.commit()
    conn.close()

def save_price(url, site, name, price):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO price_history (url, site, product_name, price, checked_at) VALUES (?,?,?,?,?)",
        (url, site, name, price, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

# ---------- Telegram ----------
def send_telegram_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram not configured")
        return
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    try:
        r = requests.post(api, json=payload, timeout=10)
        if r.status_code != 200:
            print("Telegram failed:", r.status_code, r.text)
    except Exception as e:
        print("Telegram exception:", e)

# ---------- HTTP fetch ----------
session = requests.Session()

def get_page(url, max_retries=2):
    # rotate UA
    headers = COMMON_HEADERS.copy()
    headers["User-Agent"] = random.choice(USER_AGENTS)
    # set referer to domain if possible (helps some sites)
    try:
        domain = re.match(r"https?://([^/]+)", url).group(0)
        headers["Referer"] = domain
    except:
        pass

    for attempt in range(max_retries + 1):
        try:
            r = session.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            return r.text
        except Exception as e:
            print(f"Fetch attempt {attempt+1} failed for {url}: {e}")
            time.sleep(2 + attempt)
    return None

# ---------- Price extraction helpers ----------
def only_digits_int(s):
    s2 = re.sub(r"[^\d]", "", s or "")
    if not s2:
        return None
    try:
        return int(s2)
    except:
        return None

def filter_candidates(candidates):
    # Remove None, duplicates, and obviously wrong prices
    cleaned = []
    for v in candidates:
        if v is None:
            continue
        # ignore tiny values (likely EMI or month amounts) - but keep room above MIN_REASONABLE_PRICE
        if v < MIN_REASONABLE_PRICE or v > MAX_REASONABLE_PRICE:
            continue
        if v not in cleaned:
            cleaned.append(v)
    cleaned.sort()
    return cleaned

# site-specific selectors (tuple: (selector, is_text))
SITE_SELECTORS = {
    "flipkart": [
        ("._30jeq3._16Jk6d", True),
        ("._30jeq3", True),
        (".B_NuCI", True),
        (".rgWa7D", True),
    ],
    "amazon": [
        ("#priceblock_ourprice", True),
        ("#priceblock_dealprice", True),
        (".a-price .a-offscreen", True),
        (".a-price-whole", True),
        ("meta[itemprop='price']", False),
    ],
    "myg": [
        (".price", True),
        (".prod-price", True),
        ("meta[itemprop='price']", False),
    ],
    "croma": [
        (".pdp_price", True),
        (".price", True),
        ("meta[itemprop='price']", False),
    ],
    "default": [
        (".price", True),
        (".product-price", True),
        ("meta[itemprop='price']", False),
        (".offer-price", True),
    ]
}

def extract_candidates_by_selectors(soup, selectors):
    candidates = []
    for sel, is_text in selectors:
        try:
            if sel.startswith("meta"):
                el = soup.select_one(sel)
                if el:
                    val = el.get("content") or el.get("value") or ""
                else:
                    val = ""
            else:
                el = soup.select_one(sel)
                val = el.get_text() if el else ""
            if val:
                # remove currency symbols but keep numbers
                # ignore patterns like "₹ 6,901/mo" that contain "/" or "mo"
                if re.search(r"/|per month|mo\b", val, re.I):
                    # skip EMI/per month values
                    continue
                n = only_digits_int(val)
                if n:
                    candidates.append(n)
        except Exception:
            continue
    return candidates

def fallback_regex_candidates(text):
    candidates = []
    # look for explicit rupee patterns
    for m in re.finditer(r"₹\s*([\d,]{3,})", text):
        raw = m.group(1)
        n = only_digits_int(raw)
        if n:
            candidates.append(n)
    # also any long number groups (4+ digits)
    for m in re.finditer(r"([\d,]{4,7})", text):
        n = only_digits_int(m.group(1))
        if n:
            candidates.append(n)
    return candidates

def extract_price(soup, text, url):
    # decide site
    url_l = (url or "").lower()
    if "flipkart" in url_l:
        site = "flipkart"
    elif "amazon" in url_l:
        site = "amazon"
    elif "myg.in" in url_l or "myg." in url_l:
        site = "myg"
    elif "croma" in url_l:
        site = "croma"
    else:
        site = "default"

    selectors = SITE_SELECTORS.get(site, SITE_SELECTORS["default"])

    # 1) try site-specific selectors
    candidates = extract_candidates_by_selectors(soup, selectors)
    if candidates:
        filtered = filter_candidates(candidates)
        if filtered:
            # pick smallest reasonable price (sometimes list -> lowest)
            chosen = filtered[0]
            print(f"[DEBUG] site={site} selector-match candidates={candidates} chosen={chosen}")
            return chosen

    # 2) try broader selector lists (default)
    candidates2 = extract_candidates_by_selectors(soup, SITE_SELECTORS["default"])
    candidates += candidates2
    filtered = filter_candidates(candidates)
    if filtered:
        chosen = filtered[0]
        print(f"[DEBUG] fallback-default candidates={candidates} chosen={chosen}")
        return chosen

    # 3) regex fallback
    candidates3 = fallback_regex_candidates(text)
    filtered = filter_candidates(candidates3)
    if filtered:
        chosen = filtered[0]
        print(f"[DEBUG] regex-fallback candidates={candidates3} chosen={chosen}")
        return chosen

    # 4) try scanning price-like spans but avoid "EMI" and "per month"
    # as last resort attempt to find any numeric token near currency symbol
    m = re.search(r"(?:₹|INR)\s*([\d,]{3,7})", text)
    if m:
        n = only_digits_int(m.group(1))
        if n and MIN_REASONABLE_PRICE <= n <= MAX_REASONABLE_PRICE:
            print(f"[DEBUG] last-resort regex matched {n}")
            return n

    return None

def guess_site(url):
    if not url:
        return "unknown"
    u = url.lower()
    if "amazon." in u: return "amazon"
    if "flipkart." in u: return "flipkart"
    if "myg." in u or "myg.in" in u: return "myg"
    if "croma." in u: return "croma"
    return "unknown"

def get_product_name(soup, url):
    # prefer og:title or title
    og = soup.select_one("meta[property='og:title']")
    if og and og.get("content"):
        return og.get("content").strip()
    title = soup.find("title")
    if title:
        return title.get_text().strip()
    h1 = soup.find("h1")
    if h1:
        return h1.get_text().strip()
    return url

# ---------- Core check function ----------
def check_item(item):
    url = item.get("url")
    if not url:
        print("Missing url for item:", item)
        return

    html = get_page(url)
    if not html:
        print("Failed to fetch page:", url)
        return

    soup = BeautifulSoup(html, "html.parser")
    price = extract_price(soup, html, url)
    name = get_product_name(soup, url)

    if not price:
        print("Price not found for", url)
        # save nothing (avoid saving useless values)
        return

    site = guess_site(url)

    # Check previous price
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT price FROM price_history WHERE url = ? ORDER BY id DESC LIMIT 1", (url,))
    row = cur.fetchone()
    conn.close()

    if row:
        last_price = row[0]
        if price < last_price:
            send_telegram_message(f"📉 Price Dropped!\n\n{name}\nOld: ₹{last_price}\nNew: ₹{price}\n{url}")
    else:
        send_telegram_message(f"📊 Started tracking:\n{name}\nCurrent price: ₹{price}\n{url}")

    save_price(url, site, name, price)
    print(f"[INFO] {name} → ₹{price}")

# ---------- Main ----------
def main():
    init_db()

    try:
        with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
            products = json.load(f)
    except Exception as e:
        print("Failed to read products.json:", e)
        return

    for item in products:
        try:
            check_item(item)
            time.sleep(4)  # polite pause
        except Exception as e:
            print("Error checking item:", e)

if __name__ == "__main__":
    main()
