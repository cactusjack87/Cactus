
import os
import re
import json
import time
import html
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# =========================
# CONFIGURATION
# =========================

SHOP_URL = "https://shop.travisscott.com/"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
NOTIFY_ON_START = os.getenv("NOTIFY_ON_START", "false").lower() == "true"

STATE_FILE = Path("bot_state.json")
MAX_PRODUCT_PAGES = 25

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json,*/*",
    "Accept-Language": "en-US,en;q=0.9"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()
session.headers.update(HEADERS)


# =========================
# STATE / DUPLICATE CONTROL
# =========================

def load_state():
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        logging.exception("State konnte nicht geladen werden")

    return {"notified": [], "previous_status": {}}


def save_state(state):
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2))
    temporary.replace(STATE_FILE)


state = load_state()


# =========================
# TELEGRAM
# =========================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.error("TELEGRAM_TOKEN oder CHAT_ID fehlt!")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": False
            },
            timeout=15
        )

        response.raise_for_status()

        result = response.json()

        if not result.get("ok"):
            logging.error("Telegram API Fehler: %s", result)
            return False

        return True

    except requests.RequestException:
        logging.exception("Telegram Nachricht konnte nicht gesendet werden")
        return False


# =========================
# SHOP REQUESTS
# =========================

def get_page(url):
    try:
        response = session.get(
            url,
            timeout=20,
            allow_redirects=True
        )
        response.raise_for_status()

        if "text/html" not in response.headers.get(
            "Content-Type", ""
        ).lower():
            return None

        return response.text

    except requests.RequestException:
        logging.exception("Shop-Abfrage fehlgeschlagen: %s", url)
        return None


def normalize_url(url):
    parsed = urlparse(url)

    if parsed.netloc and parsed.netloc.lower() != urlparse(SHOP_URL).netloc.lower():
        return None

    clean = parsed._replace(query="", fragment="").geturl()
    return clean.rstrip("/")


# =========================
# PRODUCT DISCOVERY
# =========================

def discover_products(soup):
    products = set()

    for link in soup.find_all("a", href=True):
        href = link["href"].strip()

        if "/products/" not in href:
            continue

        full_url = normalize_url(urljoin(SHOP_URL, href))

        if full_url:
            products.add(full_url)

    return sorted(products)[:MAX_PRODUCT_PAGES]


# =========================
# AIR FORCE 1 DETECTION
# =========================

def is_air_force_product(title):
    title = title.upper()

    return (
        "AIR FORCE 1" in title
        or bool(re.search(r"\bAF[\s-]?1\b", title))
    )


def get_shopify_product(url):
    """
    Shopify product JSON endpoint.
    Returns None if the shop does not expose this endpoint.
    """

    if not url.startswith(SHOP_URL):
        return None

    try:
        response = session.get(
            url + ".js",
            timeout=15
        )

        if response.status_code != 200:
            return None

        if "json" not in response.headers.get("Content-Type", "").lower():
            return None

        data = response.json()

        if not isinstance(data, dict) or "variants" not in data:
            return None

        return data

    except (requests.RequestException, ValueError):
        return None


def inspect_product(url):
    page = get_page(url)

    if not page:
        return None

    soup = BeautifulSoup(page, "html.parser")

    title = ""

    if soup.title:
        title = soup.title.get_text(" ", strip=True)

    # Prefer structured product title if available
    for tag in soup.find_all("meta", property="og:title"):
        if tag.get("content"):
            title = tag["content"]
            break

    # Shopify product JSON
    product = get_shopify_product(url)

    if product:
        title = product.get("title") or title

    if not is_air_force_product(title):
        return None

    # Check explicit sold-out indicators
    text = soup.get_text(" ", strip=True).upper()

    sold_out = any(
        phrase in text
        for phrase in [
            "SOLD OUT",
            "OUT OF STOCK",
            "UNAVAILABLE"
        ]
    )

    # Check Shopify variant availability
    if product:
        variants = product.get("variants", [])

        available = any(
            variant.get("available") is True
            for variant in variants
        )

        if available:
            return {
                "title": title,
                "url": url,
                "status": "AVAILABLE"
            }

        if sold_out or variants:
            return {
                "title": title,
                "url": url,
                "status": "SOLD_OUT"
            }

    # Check raffle indicators
    raffle_words = [
        "ENTER RAFFLE",
        "JOIN RAFFLE",
        "ENTER DRAW",
        "JOIN DRAW",
        "RAFFLE ENTRY"
    ]

    raffle = any(word in text for word in raffle_words)

    if raffle and not sold_out:
        return {
            "title": title,
            "url": url,
            "status": "RAFFLE"
        }

    # Check enabled add-to-cart buttons
    for button in soup.find_all(["button", "input"]):
        button_text = (
            button.get_text(" ", strip=True)
            + " "
            + str(button.get("value", ""))
        ).upper()

        disabled = (
            button.has_attr("disabled")
            or button.get("aria-disabled") == "true"
        )

        if disabled:
            continue

        if any(
            phrase in button_text
            for phrase in [
                "ADD TO CART",
                "ADD TO BAG",
                "BUY NOW"
            ]
        ):
            if not sold_out:
                return {
                    "title": title,
                    "url": url,
                    "status": "AVAILABLE"
                }

    return {
        "title": title,
        "url": url,
        "status": "SOLD_OUT" if sold_out else "UNKNOWN"
    }


# =========================
# SHOP MONITOR
# =========================

def check_shop():
    page = get_page(SHOP_URL)

    if not page:
        return []

    soup = BeautifulSoup(page, "html.parser")

    product_urls = discover_products(soup)

    logging.info(
        "Shop erreichbar | %s Produktseiten gefunden",
        len(product_urls)
    )

    results = []

    for url in product_urls:
        result = inspect_product(url)

        if result:
            results.append(result)

        # Small pause between product requests
        time.sleep(1)

    return results


# =========================
# NOTIFICATIONS
# =========================

def process_results(results, first_check=False):

    notified = set(state.get("notified", []))
    previous = state.get("previous_status", {})

    for product in results:
        url = product["url"]
        title = product["title"]
        status = product["status"]

        logging.info("%s | %s", status, title)

        if status not in ("AVAILABLE", "RAFFLE"):
            previous[url] = status
            continue

        notification_key = f"{url}|{status}"

        if notification_key in notified:
            previous[url] = status
            continue

        # Avoid sending alerts for products already live
        # when the bot first starts, unless explicitly enabled.
        if first_check and not NOTIFY_ON_START:
            notified.add(notification_key)
            previous[url] = status
            continue

        emoji = "🎟️" if status == "RAFFLE" else "🚨"

        message = (
            f"{emoji} <b>TRAVIS SCOTT AF1 LIVE!</b>\n\n"
            f"<b>Produkt:</b> {html.escape(title)}\n"
            f"<b>Status:</b> {status}\n"
            f"<b>Entdeckt:</b> "
            f"{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n\n"
            f"🛒 <a href=\"{html.escape(url, quote=True)}\">"
            f"ZUM PRODUKT</a>\n\n"
            f"🌵 <a href=\"{SHOP_URL}\">TRAVIS SCOTT SHOP</a>"
        )

        if send_telegram(message):
            notified.add(notification_key)
            logging.info("Telegram Alert gesendet: %s", title)

        previous[url] = status

    state["notified"] = list(notified)
    state["previous_status"] = previous

    save_state(state)


# =========================
# MAIN
# =========================

def main():

    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Bitte TELEGRAM_TOKEN und CHAT_ID in Railway setzen!"
        )

    if CHECK_INTERVAL < 30:
        raise ValueError("CHECK_INTERVAL muss mindestens 30 Sekunden sein.")

    logging.info("================================")
    logging.info("TRAVIS SCOTT AF1 MONITOR STARTED")
    logging.info("Check interval: %s seconds", CHECK_INTERVAL)
    logging.info("================================")

    send_telegram(
        "🌵 <b>AF1 MONITOR GESTARTET</b>\n\n"
        f"Prüfintervall: {CHECK_INTERVAL} Sekunden\n"
        "Shop: shop.travisscott.com"
    )

    first_check = True

    while True:

        try:
            results = check_shop()

            process_results(
                results,
                first_check=first_check
            )

            first_check = False

        except Exception:
            logging.exception("Unerwarteter Fehler im Monitor")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
