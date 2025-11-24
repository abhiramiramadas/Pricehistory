# price_tracker_full.py
# Full-featured price tracker:
# - accurate extraction (Flipkart/Amazon/myG/Croma)
# - sale/lightning detection heuristics
# - price-history (SQLite) + CSV export
# - daily summary (Telegram)
# - generate graphs (PNG) per product
# - compute 30-day low
# - safe headers, filtering, debug logs

import requests
from bs4 import BeautifulSoup
import re
import sqlite3
import time
import json
from datetime import datetime, timedelta
import os
import random
import csv
import math

# Optional: matplotlib for PNG graphs (already available in many runners)
# If not installed in workflow, workflow installs it.
import matplotlib.pyplot as plt

# ---------------- Config ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

DB_PATH = "prices.db"
PRODUCTS_FILE = "products.json"
CSV_PATH = "prices.csv"
GRAPHS_DIR = "graphs"

MIN_REASONABLE_PRICE = 1000
MAX_REASONABLE_PRICE = 200000

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

COMMON_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9,en-IN;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Connection": "keep-alive",
    "Referer": "https://www.google.com/",
}

# ---------------- DB helpers ----------------
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
    cur.execute("INSERT INTO price_history (url, site, product_name, price, checked_at) VALUES (?,?,?,?,?)",
                (url, site, name, price, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

# ---------------- Telegram ----------------
def send_telegram_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram not configured")
        return
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        r = requests.post(api, json=payload, timeout=10)
        if r.status_code != 200:
            print("Telegram failed:", r.status_code, r.text)
    except Exception as e:
        print("Telegram exception:", e)

# ---------------- HTTP ----------------
session = requests.Session()
def get_page(url, max_retries=2):
    headers = COMMON_HEADERS.copy()
    headers["User-Agent"] = random.choice(USER_AGENTS)
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

# ---------------- Extraction helpers ----------------
def only_digits_int(s):
    if not s:
        return None
    s2 = re.sub(r"[^\d]", "", s)
    if not s2:
        return None
    try:
        return int(s2)
    except:
        return None

def filter_candidates(candidates):
    cleaned = []
    for v in candidates:
        if v is None:
            continue
        if v < MIN_REASONABLE_PRICE or v > MAX_REASONABLE_PRICE:
            continue
        if v not in cleaned:
            cleaned.append(v)
    cleaned.sort()
    return cleaned

SITE_SELECTORS = {
    "flipkart": [("._30jeq3._16Jk6d", True), ("._30jeq3", True)],
    "amazon": [("#priceblock_ourprice", True), ("#priceblock_dealprice", True), (".a-price .a-offscreen", True), (".a-price-whole", True)],
    "default": [(".price", True), ("meta[itemprop='price']", False)]
}

def extract_candidates_by_selectors(soup, selectors):
    candidates = []
    for sel, is_text in selectors:
        try:
            if sel.startswith("meta"):
                el = soup.select_one(sel)
                val = el.get("content") if el else ""
            else:
                el = soup.select_one(sel)
                val = el.get_text() if el else ""
            if val:
                if re.search(r"/|per month|mo\b|EMI|emi", val, re.I):
                    continue
                n = only_digits_int(val)
                if n:
                    candidates.append(n)
        except:
            continue
    return candidates

def fallback_regex_candidates(text):
    candidates = []
    for m in re.finditer(r"₹\s*([\d,]{3,7})", text):
        n = only_digits_int(m.group(1))
        if n: candidates.append(n)
    for m in re.finditer(r"([\d,]{4,7})", text):
        n = only_digits_int(m.group(1))
        if n: candidates.append(n)
    return candidates

def extract_price(soup, text, url):
    url_l = (url or "").lower()
    if "flipkart" in url_l:
        site = "flipkart"
    elif "amazon" in url_l:
        site = "amazon"
    else:
        site = "default"
    selectors = SITE_SELECTORS.get(site, SITE_SELECTORS["default"])
    candidates = extract_candidates_by_selectors(soup, selectors)
    if candidates:
        filtered = filter_candidates(candidates)
        if filtered:
            chosen = filtered[0]
            print(f"[DEBUG] site={site} selector-match candidates={candidates} chosen={chosen}")
            return chosen
    candidates2 = extract_candidates_by_selectors(soup, SITE_SELECTORS["default"])
    candidates += candidates2
    filtered = filter_candidates(candidates)
    if filtered:
        chosen = filtered[0]
        print(f"[DEBUG] fallback-default candidates={candidates} chosen={chosen}")
        return chosen
    candidates3 = fallback_regex_candidates(text)
    filtered = filter_candidates(candidates3)
    if filtered:
        chosen = filtered[0]
        print(f"[DEBUG] regex-fallback candidates={candidates3} chosen={chosen}")
        return chosen
    m = re.search(r"(?:₹|INR)\s*([\d,]{3,7})", text)
    if m:
        n = only_digits_int(m.group(1))
        if n and MIN_REASONABLE_PRICE <= n <= MAX_REASONABLE_PRICE:
            print(f"[DEBUG] last-resort regex matched {n}")
            return n
    return None

def guess_site(url):
    if not url: return "unknown"
    u = url.lower()
    if "amazon." in u: return "amazon"
    if "flipkart." in u: return "flipkart"
    return "unknown"

def get_product_name(soup, url):
    og = soup.select_one("meta[property='og:title']")
    if og and og.get("content"): return og.get("content").strip()
    t = soup.find("title"); 
    if t: return t.get_text().strip()
    h1 = soup.find("h1")
    if h1: return h1.get_text().strip()
    return url

# ---------------- Features: CSV/graph/daily summary/30-day low ----------------
def export_to_csv():
    # Export full DB to CSV (overwrites)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT url, site, product_name, price, checked_at FROM price_history ORDER BY checked_at ASC")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return
    os.makedirs(os.path.dirname(CSV_PATH) or ".", exist_ok=True)
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["url", "site", "product_name", "price", "checked_at"])
        writer.writerows(rows)

def generate_graph_for_product(url):
    # Make a time series graph for the product and save as PNG
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT price, checked_at FROM price_history WHERE url = ? ORDER BY checked_at ASC", (url,))
    rows = cur.fetchall()
    conn.close()
    if not rows: return None
    prices = [r[0] for r in rows]
    times = [datetime.fromisoformat(r[1]) for r in rows]
    # plot
    os.makedirs(GRAPHS_DIR, exist_ok=True)
    fname = os.path.join(GRAPHS_DIR, re.sub(r'[^0-9a-zA-Z]+', '_', url)[:80] + ".png")
    plt.figure(figsize=(6,3))
    plt.plot(times, prices)
    plt.title("Price history")
    plt.xlabel("Time")
    plt.ylabel("Price (INR)")
    plt.tight_layout()
    plt.savefig(fname)
    plt.close()
    return fname

def last_n_days_low(url, days=30):
    cutoff = datetime.utcnow() - timedelta(days=days)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT MIN(price) FROM price_history WHERE url = ? AND checked_at >= ?", (url, cutoff.isoformat()))
    row = cur.fetchone()
    conn.close()
    if row and row[0]:
        return int(row[0])
    return None

def detect_sale_flags(soup, text):
    # simple heuristics: presence of "deal", "limited time", "save", "offer", "discount"
    flags = []
    lowtext = (text or "").lower()
    if re.search(r"limited time|deal of the day|lightning deal|limited edition|today's deal", lowtext):
        flags.append("lightning/deal")
    if re.search(r"save \u20b9?\s?[\d,]{2,}|% off|%discount|discount", lowtext):
        flags.append("discount")
    if re.search(r"special price|offer price|bank offer|exchange offer", lowtext):
        flags.append("offer")
    return flags

# ---------------- Core check ----------------
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
        return
    site = guess_site(url)
    flags = detect_sale_flags(soup, html)
    # previous price
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT price FROM price_history WHERE url = ? ORDER BY id DESC LIMIT 1", (url,))
    row = cur.fetchone()
    conn.close()
    if row:
        last_price = row[0]
        if price < last_price:
            pct = (last_price - price) / last_price * 100
            send_telegram_message(f"📉 *Price Dropped!* \n{name}\nOld: ₹{last_price}\nNew: ₹{price}\nDrop: {pct:.2f}%\n30-day low: ₹{last_n_days_low(url) or price}\nFlags: {', '.join(flags) if flags else '—'}\n{url}")
    else:
        send_telegram_message(f"📊 Started tracking:\n{name}\nCurrent price: ₹{price}\nFlags: {', '.join(flags) if flags else '—'}\n{url}")
    save_price(url, site, name, price)
    # after saving, export CSV & graph
    export_to_csv()
    g = generate_graph_for_product(url)
    print(f"[INFO] {name} → ₹{price} (saved). Graph: {g if g else 'none'}")

# ---------------- Daily summary ----------------
def send_daily_summary():
    # for last 24 hours: list items that changed (old->new) and lowest in 30d
    since = datetime.utcnow() - timedelta(days=1)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT url FROM price_history")
    urls = [r[0] for r in cur.fetchall()]
    messages = []
    for url in urls:
        # find last two prices in last 24-48 hours
        cur.execute("SELECT price, checked_at FROM price_history WHERE url = ? AND checked_at >= ? ORDER BY checked_at ASC", (url, since.isoformat()))
        rows = cur.fetchall()
        if not rows:
            continue
        # find earliest and latest in this window
        earliest = rows[0][0]
        latest = rows[-1][0]
        if latest != earliest:
            messages.append(f"{url}\nOld: ₹{earliest} -> New: ₹{latest} (30d low: ₹{last_n_days_low(url) or latest})")
    conn.close()
    if messages:
        body = "*Daily summary — price changes in last 24h*\n\n" + "\n\n".join(messages)
        send_telegram_message(body)

# ---------------- Main ----------------
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
            time.sleep(3)
        except Exception as e:
            print("Error checking item:", e)
    # if this run is at roughly 00:30 UTC (or you can schedule a separate daily job),
    # run daily summary. We'll check local time and run summary if hour==0 (UTC)
    now = datetime.utcnow()
    if now.hour == 0 and 0 <= now.minute < 10:
        send_daily_summary()

if __name__ == "__main__":
    main()
