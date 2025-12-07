# ---------- Patched Price Tracker (Tara Edition) ----------
# Fixes false price detections like ₹1996 on Amazon

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
import matplotlib.pyplot as plt

# ---------- Config ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

DB_PATH = "prices.db"
PRODUCTS_FILE = "products.json"
CSV_PATH = "prices.csv"
GRAPHS_DIR = "graphs"

# REALISTIC PRICE FILTERING
MIN_REAL_IPHONE_PRICE = 40000
MAX_REAL_IPHONE_PRICE = 200000

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

session = requests.Session()

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
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        r = requests.post(api, json=payload, timeout=10)
        if r.status_code != 200:
            print("Telegram failed:", r.status_code, r.text)
    except Exception as e:
        print("Telegram exception:", e)

# ---------- HTTP helpers ----------
def get_headers():
    h = COMMON_HEADERS.copy()
    h["User-Agent"] = random.choice(USER_AGENTS)
    return h

def get_page(url, max_retries=2):
    headers = get_headers()
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

# ---------- Flipkart API ----------
def fetch_flipkart_price_by_pid(pid):
    API_ENDPOINTS = [
        "https://www.flipkart.com/api/3/page/dynamic/product?pid={pid}",
        "https://www.flipkart.com/api/3/product/{pid}",
        "https://www.flipkart.com/api/3/page/json/product?pid={pid}",
    ]
    for endpoint in API_ENDPOINTS:
        url = endpoint.format(pid=pid)
        try:
            r = session.get(url, headers=get_headers(), timeout=10)
            if r.status_code != 200:
                continue
            data = parse_json_safely(r)
            if data:
                price = find_price_in_json(data)
                if price and MIN_REAL_IPHONE_PRICE <= price <= MAX_REAL_IPHONE_PRICE:
                    return price
        except:
            continue
    return None

def parse_json_safely(resp):
    try:
        return resp.json()
    except:
        txt = resp.text
        m = re.search(r"({.+})", txt, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except:
                return None
    return None

def find_price_in_json(obj):
    if isinstance(obj, dict):
        for k,v in obj.items():
            if isinstance(v, (dict,list)):
                r = find_price_in_json(v)
                if r: return r
            else:
                if "price" in str(k).lower():
                    n = only_digits(v)
                    if n: return n
    elif isinstance(obj, list):
        for item in obj:
            r = find_price_in_json(item)
            if r: return r
    return None

# ---------- Helpers ----------
def only_digits(s):
    s = str(s)
    num = re.sub(r"[^\d]", "", s)
    if not num: return None
    try:
        return int(num)
    except:
        return None

def is_valid_price(n):
    if not n: return False
    return MIN_REAL_IPHONE_PRICE <= n <= MAX_REAL_IPHONE_PRICE

# ---------- Amazon / HTML scraping ----------
AMAZON_SELECTORS = [
    ".a-price .a-offscreen",
    "#priceblock_ourprice",
    "#priceblock_dealprice"
]

def extract_price_html(soup, url, full_text):
    url_l = url.lower()

    selectors = AMAZON_SELECTORS if "amazon" in url_l else [".price", ".offer-price"]

    # structured extraction first
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            n = only_digits(el.get_text())
            if is_valid_price(n):
                return n

    # reject obvious EMI values
    emi_vals = re.findall(r"₹\s*([\d,]+)\s*/\s*month", full_text, re.I)
    if emi_vals:
        pass  # ignore

    # fallback regex, but ONLY allow valid range
    for m in re.finditer(r"₹\s*([\d,]{4,7})", full_text):
        n = only_digits(m.group(1))
        if is_valid_price(n):
            return n

    return None

# ---------- Main Check ----------
def check_item(item):
    url = item.get("url")
    pid = item.get("flipkart_pid")
    name = item.get("name") or url

    price = None

    # Try Flipkart API
    if pid:
        price = fetch_flipkart_price_by_pid(pid)
        if price: site = "flipkart"
        else: site = "unknown"

    # Fallback: HTML scraping
    if price is None and url:
        html = get_page(url)
        if not html:
            print("Failed to fetch", url)
            return
        soup = BeautifulSoup(html, "html.parser")
        price = extract_price_html(soup, url, html)
        site = "amazon" if "amazon" in url.lower() else "unknown"

    if price is None:
        print("No valid price found for", url)
        return

    # Compare to last
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT price FROM price_history WHERE url = ? ORDER BY id DESC LIMIT 1", (url,))
    row = cur.fetchone()
    conn.close()

    if row:
        old = row[0]
        if price < old:
            pct = (old - price) / old * 100
            send_telegram_message(
                f"📉 *Price Dropped!*\n{name}\nOld: ₹{old}\nNew: ₹{price}\nDrop: {pct:.2f}%\n30-day low: ₹{last_30d_low(url) or price}\n{url}"
            )
    else:
        send_telegram_message(
            f"📊 Started tracking:\n{name}\nCurrent price: ₹{price}\n{url}"
        )

    save_price(url, site, name, price)
    export_csv()
    generate_graph(url)

    print(f"[INFO] Saved {name} → ₹{price}")

# ---------- CSV & Graph ----------
def export_csv():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT url, site, product_name, price, checked_at FROM price_history ORDER BY checked_at ASC")
    rows = cur.fetchall()
    conn.close()
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["url", "site", "product_name", "price", "checked_at"])
        writer.writerows(rows)

def generate_graph(url):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT price, checked_at FROM price_history WHERE url = ? ORDER BY checked_at ASC", (url,))
    rows = cur.fetchall()
    conn.close()
    if not rows: return
    prices = [r[0] for r in rows]
    times = [datetime.fromisoformat(r[1]) for r in rows]
    os.makedirs(GRAPHS_DIR, exist_ok=True)
    fname = os.path.join(GRAPHS_DIR, re.sub(r"[^a-zA-Z0-9]+", "_", url) + ".png")
    plt.figure(figsize=(6,3))
    plt.plot(times, prices, marker="o")
    plt.savefig(fname)
    plt.close()

def last_30d_low(url):
    cutoff = datetime.utcnow() - timedelta(days=30)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT MIN(price) FROM price_history WHERE url = ? AND checked_at >= ?", (url, cutoff.isoformat()))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

# ---------- Main ----------
def main():
    init_db()
    try:
        with open(PRODUCTS_FILE, "r") as f:
            products = json.load(f)
    except:
        print("Cannot read products.json")
        return

    for item in products:
        try:
            check_item(item)
            time.sleep(3)
        except Exception as e:
            print("Error:", e)

if __name__ == "__main__":
    main()
