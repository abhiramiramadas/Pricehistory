# price_tracker.py
# Flipkart-API-enabled price tracker + Amazon scraping + alerts + CSV + graphs
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

# ---------- Flipkart API helpers ----------
# Try a few known Flipkart JSON endpoints (these are internal endpoints observed in practice).
FLIPKART_API_CANDIDATES = [
    "https://www.flipkart.com/api/3/page/dynamic/product?pid={pid}",
    "https://www.flipkart.com/api/3/product/{pid}",
    "https://www.flipkart.com/api/3/page/json/product?pid={pid}",
]

def fetch_flipkart_price_by_pid(pid):
    headers = get_headers()
    # add a generic Accept header for JSON
    headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
    for endpoint in FLIPKART_API_CANDIDATES:
        url = endpoint.format(pid=pid)
        try:
            r = session.get(url, headers=headers, timeout=10)
            if r.status_code != 200:
                # sometimes returns 403/429; try next
                print(f"[Flipkart API] {url} -> status {r.status_code}")
                continue
            data = None
            try:
                data = r.json()
            except Exception:
                # some endpoints wrap JSON inside HTML; try to extract
                txt = r.text
                m = re.search(r"({.+})", txt, re.S)
                if m:
                    try:
                        data = json.loads(m.group(1))
                    except:
                        data = None
            if data:
                # search for price keys recursively
                price = find_price_in_json(data)
                if price:
                    return price
        except Exception as e:
            print(f"[Flipkart API] error calling {url}: {e}")
            continue
    return None

def find_price_in_json(obj):
    # recursive search for keys likely containing final price
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                p = find_price_in_json(v)
                if p:
                    return p
            else:
                key = str(k).lower()
                if key in ("finalprice", "final_price", "finalPrice".lower(), "price", "sellingPrice".lower(), "sp"):
                    n = only_digits_int(str(v))
                    if n and MIN_REASONABLE_PRICE <= n <= MAX_REASONABLE_PRICE:
                        return n
                # sometimes value is dict-like in string
                if isinstance(v, str):
                    m = re.search(r"₹\s*([\d,]{3,7})", v)
                    if m:
                        n = only_digits_int(m.group(1))
                        if n and MIN_REASONABLE_PRICE <= n <= MAX_REASONABLE_PRICE:
                            return n
    elif isinstance(obj, list):
        for item in obj:
            p = find_price_in_json(item)
            if p:
                return p
    return None

# ---------- Generic extraction helpers (Amazon + fallback) ----------
def only_digits_int(s):
    s2 = re.sub(r"[^\d]", "", s or "")
    if not s2:
        return None
    try:
        return int(s2)
    except:
        return None

def filter_candidates(candidates):
    cleaned = []
    for v in candidates:
        if v is None: continue
        if v < MIN_REASONABLE_PRICE or v > MAX_REASONABLE_PRICE: continue
        if v not in cleaned: cleaned.append(v)
    cleaned.sort()
    return cleaned

# site-specific CSS selectors (kept minimal and pragmatic)
SITE_SELECTORS = {
    "amazon": [
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        ".a-price .a-offscreen",
        ".a-price-whole",
        "meta[itemprop='price']"
    ],
    "default": [
        ".price",
        ".product-price",
        "meta[itemprop='price']",
        ".offer-price"
    ]
}

def extract_candidates_by_selectors(soup, selectors):
    candidates = []
    for sel in selectors:
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

def extract_price_from_html(soup, text, url):
    url_l = (url or "").lower()
    if "amazon" in url_l:
        selectors = SITE_SELECTORS["amazon"]
    else:
        selectors = SITE_SELECTORS["default"]
    candidates = extract_candidates_by_selectors(soup, selectors)
    filtered = filter_candidates(candidates)
    if filtered:
        print(f"[DEBUG] selector candidates={candidates} chosen={filtered[0]}")
        return filtered[0]
    # broader fallback
    candidates2 = extract_candidates_by_selectors(soup, SITE_SELECTORS["default"])
    candidates += candidates2
    filtered = filter_candidates(candidates)
    if filtered:
        print(f"[DEBUG] fallback candidates={candidates} chosen={filtered[0]}")
        return filtered[0]
    # regex fallback
    candidates3 = fallback_regex_candidates(text)
    filtered = filter_candidates(candidates3)
    if filtered:
        print(f"[DEBUG] regex fallback candidates={candidates3} chosen={filtered[0]}")
        return filtered[0]
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
    t = soup.find("title")
    if t: return t.get_text().strip()
    h1 = soup.find("h1")
    if h1: return h1.get_text().strip()
    return url

# ---------- CSV, graph, summary helpers ----------
def export_to_csv():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT url, site, product_name, price, checked_at FROM price_history ORDER BY checked_at ASC")
    rows = cur.fetchall()
    conn.close()
    if not rows: return
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["url", "site", "product_name", "price", "checked_at"])
        writer.writerows(rows)

def generate_graph_for_product(url):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT price, checked_at FROM price_history WHERE url = ? ORDER BY checked_at ASC", (url,))
    rows = cur.fetchall()
    conn.close()
    if not rows: return None
    prices = [r[0] for r in rows]
    times = [datetime.fromisoformat(r[1]) for r in rows]
    os.makedirs(GRAPHS_DIR, exist_ok=True)
    fname = os.path.join(GRAPHS_DIR, re.sub(r'[^0-9a-zA-Z]+', '_', url)[:80] + ".png")
    plt.figure(figsize=(6,3))
    plt.plot(times, prices, marker='o')
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

# ---------- Core check ----------
def check_item(item):
    url = item.get("url")
    flipkart_pid = item.get("flipkart_pid")
    if not url and not flipkart_pid:
        print("Missing url and flipkart_pid for item:", item)
        return

    # 1) If flipkart_pid is provided, try Flipkart API first
    price = None
    name = None
    site = "unknown"

    if flipkart_pid:
        price = fetch_flipkart_price_by_pid(flipkart_pid)
        if price:
            site = "flipkart"
            # Build a friendly name if provided
            name = item.get("name") or f"Flipkart PID {flipkart_pid}"

    # 2) If API failed or not provided, fall back to HTML scraping using url
    if price is None and url:
        html = get_page(url)
        if not html:
            print("Failed to fetch page:", url)
            return
        soup = BeautifulSoup(html, "html.parser")
        price = extract_price_from_html(soup, html, url)
        name = name or get_product_name(soup, url)
        site = guess_site(url)

    # 3) If still None -> abort
    if not price:
        print("Price not found for", url or flipkart_pid)
        return

    # Compare with last saved price and send alerts
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT price FROM price_history WHERE url = ? ORDER BY id DESC LIMIT 1", (url or flipkart_pid,))
    row = cur.fetchone()
    conn.close()

    if row:
        last_price = row[0]
        if price < last_price:
            pct = (last_price - price) / last_price * 100
            send_telegram_message(f"📉 *Price Dropped!* \n{name}\nOld: ₹{last_price}\nNew: ₹{price}\nDrop: {pct:.2f}%\n30-day low: ₹{last_n_days_low(url or flipkart_pid) or price}\n{url or ''}")
    else:
        send_telegram_message(f"📊 Started tracking:\n{name}\nCurrent price: ₹{price}\n{url or ''}")

    save_price(url or flipkart_pid, site, name, price)
    export_to_csv()
    g = generate_graph_for_product(url or flipkart_pid)
    print(f"[INFO] {name} → ₹{price} (saved). Graph: {g if g else 'none'}")

# ---------- Daily summary ----------
def send_daily_summary():
    since = datetime.utcnow() - timedelta(days=1)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT url FROM price_history")
    urls = [r[0] for r in cur.fetchall()]
    messages = []
    for url in urls:
        cur.execute("SELECT price, checked_at FROM price_history WHERE url = ? AND checked_at >= ? ORDER BY checked_at ASC", (url, since.isoformat()))
        rows = cur.fetchall()
        if not rows:
            continue
        earliest = rows[0][0]
        latest = rows[-1][0]
        if latest != earliest:
            messages.append(f"{url}\nOld: ₹{earliest} -> New: ₹{latest} (30d low: ₹{last_n_days_low(url) or latest})")
    conn.close()
    if messages:
        body = "*Daily summary — price changes in last 24h*\n\n" + "\n\n".join(messages)
        send_telegram_message(body)

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
            time.sleep(3)
        except Exception as e:
            print("Error checking item:", e)

    now = datetime.utcnow()
    # daily summary at 00:00 UTC (approx)
    if now.hour == 0 and 0 <= now.minute < 10:
        send_daily_summary()

if __name__ == "__main__":
    main()
