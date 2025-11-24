import requests
from bs4 import BeautifulSoup
import re
import sqlite3
import time
import json
from datetime import datetime
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

DB_PATH = "prices.db"
PRODUCTS_FILE = "products.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"

headers = {"User-Agent": USER_AGENT}


# ------------------------------
# Database Setup
# ------------------------------
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


# ------------------------------
# Telegram
# ------------------------------
def send_telegram_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram not configured")
        return
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    requests.post(api, json=payload)


# ------------------------------
# Fetch & Parse
# ------------------------------
def get_page(url):
    try:
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        return r.text
    except:
        return None


def extract_price(soup, text):
    selectors = [
        ('#priceblock_ourprice', True),           # Amazon old
        ('#priceblock_dealprice', True),          # Amazon deal
        ('.a-price .a-offscreen', True),          # Amazon new
        ('._30jeq3._16Jk6d', True),               # Flipkart
        ('._30jeq3', True),
        (".price", True)                          # Generic
    ]

    for sel, is_text in selectors:
        el = soup.select_one(sel)
        if el:
            val = el.get_text() if is_text else el.get("content")
            p = clean_price(val)
            if p:
                return p

    # Fallback regex ₹xxxxx
    m = re.search(r'₹\s?([\d,]+)', text)
    if m:
        return int(m.group(1).replace(",", ""))

    return None


def clean_price(val):
    if not val:
        return None
    val = re.sub(r"[^\d]", "", val)
    if val.isdigit():
        return int(val)
    return None


def guess_site(url):
    if "amazon" in url: return "amazon"
    if "flipkart" in url: return "flipkart"
    if "croma" in url: return "croma"
    if "myg.in" in url: return "myg"
    return "unknown"


def get_product_name(soup, url):
    t = soup.find("title")
    if t:
        return t.get_text().strip()
    return url


# ------------------------------
# MAIN PRICE CHECK FUNCTION
# ------------------------------
def check_item(item):
    url = item["url"]

    html = get_page(url)
    if not html:
        print("Failed to fetch:", url)
        return

    soup = BeautifulSoup(html, "html.parser")
    price = extract_price(soup, html)
    name = get_product_name(soup, url)

    if not price:
        print("Price not found for", url)
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
            send_telegram_message(
                f"📉 Price Dropped!\n\n{name}\nOld: ₹{last_price}\nNew: ₹{price}\n{url}"
            )
    else:
        # First time tracking
        send_telegram_message(
            f"📊 Started tracking:\n{name}\nCurrent price: ₹{price}\n{url}"
        )

    # Save the new price
    save_price(url, site, name, price)
    print(name, "→ ₹", price)


# ------------------------------
# MAIN
# ------------------------------
def main():
    init_db()

    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        products = json.load(f)

    for item in products:
        check_item(item)
        time.sleep(5)  # small delay to avoid blocks


if __name__ == "__main__":
    main()
