import os
import re
import time
import json
import sqlite3
import threading
from datetime import datetime, time as dt_time
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, redirect, url_for, abort

# ----------------------
# PADEN & LOCKS
# ----------------------
CONFIG_FILE = os.environ.get("WATCHER_CONFIG_PATH", "/config/settings.json")
KEYWORDS_FILE = os.environ.get("WATCHER_KEYWORDS_PATH", "/config/keywords.json")
RESULTS_DB = os.environ.get("WATCHER_RESULTS_DB_PATH", "/config/results.db")

CONFIG_LOCK = threading.Lock()
KEYWORDS_LOCK = threading.Lock()


# ----------------------
# STORAGE INIT
# ----------------------
def ensure_storage():
    base_dir = "/config"
    os.makedirs(base_dir, exist_ok=True)

    # settings.json
    if not os.path.exists(CONFIG_FILE):
        default_cfg = {
            "default_interval_minutes": "15",
            "default_limit_per_run": "5",
            "sleep_mode": "nee",
            "sleep_start": "23:00",
            "sleep_end": "07:00",
            "postcode": "",
            "radius_km": "alle",
            "telegram_bot_id": "",
            "telegram_chat_id": "",
            "manual_telegram": "nee",
        }
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(default_cfg, f, indent=2)

    # keywords.json
    if not os.path.exists(KEYWORDS_FILE):
        with open(KEYWORDS_FILE, "w", encoding="utf-8") as f:
            json.dump({"keywords": []}, f, indent=2)

    ensure_results_db()


def ensure_results_db():
    conn = sqlite3.connect(RESULTS_DB)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS search_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            found_at REAL NOT NULL,
            price TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def get_results_db_connection():
    conn = sqlite3.connect(RESULTS_DB)
    conn.row_factory = sqlite3.Row
    return conn


# ----------------------
# CONFIG FUNCTIES (settings.json)
# ----------------------
def get_config():
    with CONFIG_LOCK:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)


def get_config_value(key: str, default=None):
    cfg = get_config()
    return cfg.get(key, default)


def update_config(updates: dict):
    with CONFIG_LOCK:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.update(updates)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_FILE)


# ----------------------
# KEYWORD FUNCTIES (keywords.json)
# ----------------------
def _read_keywords_raw():
    with open(KEYWORDS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("keywords", [])


def _write_keywords_raw(keywords):
    tmp = KEYWORDS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"keywords": keywords}, f, indent=2)
    os.replace(tmp, KEYWORDS_FILE)


def load_keywords():
    with KEYWORDS_LOCK:
        return _read_keywords_raw()


def save_keywords(keywords):
    with KEYWORDS_LOCK:
        _write_keywords_raw(keywords)


def find_keyword_by_id(keyword_id: int):
    keywords = load_keywords()
    for kw in keywords:
        if kw["id"] == keyword_id:
            return kw
    return None


# ----------------------
# RESULTATEN DB HULP
# ----------------------
def get_existing_urls_for_keyword(keyword_id: int):
    conn = get_results_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT url FROM search_results WHERE keyword_id = ?",
        (keyword_id,),
    )
    rows = c.fetchall()
    conn.close()
    return {row["url"] for row in rows}


def replace_results_for_keyword(keyword_id: int, ads, timestamp: float):
    conn = get_results_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM search_results WHERE keyword_id = ?", (keyword_id,))
    for ad in ads:
        c.execute(
            """
            INSERT INTO search_results (keyword_id, title, url, found_at, price)
            VALUES (?, ?, ?, ?, ?)
            """,
            (keyword_id, ad["title"], ad["url"], timestamp, ad.get("price")),
        )
    conn.commit()
    conn.close()


def delete_results_for_keyword(keyword_id: int):
    conn = get_results_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM search_results WHERE keyword_id = ?", (keyword_id,))
    conn.commit()
    conn.close()


def get_results_for_keyword(keyword_id: int):
    conn = get_results_db_connection()
    c = conn.cursor()
    c.execute(
        """
        SELECT * FROM search_results
        WHERE keyword_id = ?
        ORDER BY found_at DESC, id DESC
        """,
        (keyword_id,),
    )
    rows = c.fetchall()
    conn.close()
    return rows


# ----------------------
# TELEGRAM
# ----------------------
def send_telegram_message(text: str):
    cfg = get_config()
    bot_token = (cfg.get("telegram_bot_id") or "").strip()
    chat_id = (cfg.get("telegram_chat_id") or "").strip()

    if not bot_token or not chat_id:
        print("[Telegram] Niet geconfigureerd, geen bericht verstuurd.")
        return

    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        resp = requests.post(url, data=payload, timeout=15)
        resp.raise_for_status()
        print("[Telegram] Bericht (text) verzonden")
    except Exception as e:
        print(f"[Telegram] FOUT bij versturen text-bericht: {e}")


def send_telegram_ad(ad: dict):
    cfg = get_config()
    bot_token = (cfg.get("telegram_bot_id") or "").strip()
    chat_id = (cfg.get("telegram_chat_id") or "").strip()

    if not bot_token or not chat_id:
        print("[Telegram] Niet geconfigureerd, geen advertentie-bericht verstuurd.")
        return

    title = ad.get("title") or "Onbekende titel"
    price = ad.get("price") or "Onbekend"
    url = ad.get("url") or ""

    caption = f"Titel: {title}\nPrijs: {price}"

    reply_markup = {
        "inline_keyboard": [
            [
                {
                    "text": "Bekijk advertentie",
                    "url": url,
                }
            ]
        ]
    }

    image_url = ad.get("image_url")

    try:
        if image_url:
            api_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
            payload = {
                "chat_id": chat_id,
                "photo": image_url,
                "caption": caption,
                "reply_markup": json.dumps(reply_markup),
            }
            resp = requests.post(api_url, data=payload, timeout=15)
            resp.raise_for_status()
            print("[Telegram] Advertentie met foto verzonden")
        else:
            api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": caption,
                "reply_markup": json.dumps(reply_markup),
            }
            resp = requests.post(api_url, data=payload, timeout=15)
            resp.raise_for_status()
            print("[Telegram] Advertentie zonder foto verzonden")
    except Exception as e:
        print(f"[Telegram] FOUT bij versturen advertentie: {e}")


# ----------------------
# SLAAPSTAND
# ----------------------
def parse_time_str(s: str, default: dt_time) -> dt_time:
    try:
        parts = s.split(":")
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        return dt_time(hour=h, minute=m)
    except Exception:
        return default


def is_in_sleep_window(now_dt: datetime, start_str: str, end_str: str) -> bool:
    t = now_dt.time()
    start = parse_time_str(start_str, dt_time(23, 0))
    end = parse_time_str(end_str, dt_time(7, 0))

    if start < end:
        return start <= t < end
    else:
        return t >= start or t < end


# ----------------------
# HULP: PRIJS & LIMIET
# ----------------------
def parse_price_to_euros(price_text: str):
    if not price_text:
        return None

    cleaned = re.sub(r"[^\d\.,]", "", price_text)
    if not cleaned:
        return None

    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        value = float(cleaned)
        return int(round(value))
    except Exception:
        return None


def _parse_int_or_none(value: str):
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def normalize_limit(value):
    if value is None:
        return 5
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 5
    if v < 1:
        return 1
    if v > 20:
        return 20
    return v


# ----------------------
# MARKTPLAATS SCRAPER
# ----------------------
def search_marktplaats(term: str, limit: int):
    query = quote_plus(term)
    base_url = f"https://www.marktplaats.nl/q/{query}/"

    cfg = get_config()
    postcode = (cfg.get("postcode") or "").replace(" ", "").upper()
    radius = (cfg.get("radius_km") or "").strip()

    distance_meters = None
    if radius and radius.lower() not in ("alle", "all", "0"):
        try:
            km = int(radius)
            distance_meters = km * 1000
        except ValueError:
            distance_meters = None

    fragment_parts = ["offeredSince:Altijd"]
    if distance_meters:
        fragment_parts.append(f"distanceMeters:{distance_meters}")
    if postcode:
        fragment_parts.append(f"postcode:{postcode}")

    if distance_meters or postcode:
        url = base_url + "#" + "|".join(fragment_parts)
    else:
        url = base_url

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36 MarktplaatsWatcher/1.0"
        )
    }

    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()

    html = resp.text
    soup = BeautifulSoup(html, "html.parser")

    results = []

    for a in soup.select('a[href*="/v/"]'):
        href = a.get("href")
        if not href:
            continue

        raw_title = a.get("title") or a.get_text(strip=True)
        if not raw_title:
            continue

        title = raw_title
        if "€" in title:
            title = title.split("€", 1)[0]
        if "details" in title:
            title = title.split("details", 1)[0]
        title = " ".join(title.split()).strip()
        if not title:
            title = raw_title.strip()

        if not href.startswith("http"):
            href = f"https://www.marktplaats.nl{href}"

        container = a.find_parent(["article", "li", "div", "section"])

        price_text = None
        if container:
            snippet = container.get_text(" ", strip=True)
            m = re.search(r"€\s?[0-9][0-9\.\,]*", snippet)
            if m:
                price_text = m.group(0).strip()

        price = price_text or "Onbekend"
        price_value = parse_price_to_euros(price_text)

        image_url = None
        if container:
            img = container.find("img")
            if img:
                image_url = (
                    img.get("src")
                    or img.get("data-src")
                    or img.get("data-img")
                    or None
                )

        results.append(
            {
                "title": title,
                "url": href,
                "price": price,
                "price_value": price_value,
                "image_url": image_url,
            }
        )

        if len(results) >= limit:
            break

    print(
        f"[search_marktplaats] term='{term}', url='{url}', gevonden links: {len(results)}"
    )

    return results


# ----------------------
# FILTEREN OP MIN/MAX PRIJS
# ----------------------
def filter_ads_by_price(ads, keyword_row):
    min_price = keyword_row.get("min_price")
    max_price = keyword_row.get("max_price")

    if min_price is None and max_price is None:
        return ads

    filtered = []
    for ad in ads:
        pv = ad.get("price_value")
        if pv is None:
            filtered.append(ad)
            continue

        if min_price is not None and pv < min_price:
            continue
        if max_price is not None and pv > max_price:
            continue

        filtered.append(ad)

    return filtered


# ----------------------
# AUTOMATISCHE RUN
# ----------------------
def run_search_for_keyword(keyword_id: int):
    kw = find_keyword_by_id(keyword_id)
    if kw is None:
        print(f"[run_search_for_keyword] Keyword {keyword_id} niet gevonden")
        return

    term = kw["term"]
    limit_per_run = normalize_limit(kw.get("limit_per_run"))
    now_ts = time.time()
    print(f"[{datetime.now().isoformat()}] Auto-zoekactie voor: '{term}'")

    old_urls = get_existing_urls_for_keyword(keyword_id)

    try:
        ads = search_marktplaats(term, limit_per_run)
        ads = filter_ads_by_price(ads, kw)
        print(f"  Na prijsfilter: {len(ads)} advertenties voor '{term}'")

        new_ads = [ad for ad in ads if ad["url"] not in old_urls]
        print(f"  Nieuwe advertenties sinds vorige run: {len(new_ads)}")

        # Resultaten in DB
        replace_results_for_keyword(keyword_id, ads, now_ts)

        # last_run_at updaten in keywords.json
        keywords = load_keywords()
        for item in keywords:
            if item["id"] == keyword_id:
                item["last_run_at"] = now_ts
                break
        save_keywords(keywords)

        if not new_ads:
            print("[Telegram] Geen nieuwe advertenties bij automatische run.")
            return

        for ad in new_ads[:limit_per_run]:
            send_telegram_ad(ad)

    except Exception as e:
        print(f"FOUT bij automatische zoekactie voor '{term}': {e}")


# ----------------------
# BACKGROUND WORKER
# ----------------------
def worker_loop():
    while True:
        try:
            cfg = get_config()
            sleep_mode = (cfg.get("sleep_mode") or "nee").strip().lower()
            sleep_start = cfg.get("sleep_start", "23:00")
            sleep_end = cfg.get("sleep_end", "07:00")

            now_dt = datetime.now()
            in_sleep = sleep_mode == "ja" and is_in_sleep_window(
                now_dt, sleep_start, sleep_end
            )
            now_epoch = time.time()

            keywords = load_keywords()
            for kw in keywords:
                interval = kw.get("interval_minutes", 15)
                last_run_at = kw.get("last_run_at") or 0

                effective_interval = interval
                if in_sleep:
                    effective_interval = max(effective_interval, 60)

                if now_epoch - last_run_at >= effective_interval * 60:
                    run_search_for_keyword(kw["id"])

        except Exception as e:
            print(f"[worker_loop] Algemene fout: {e}")

        time.sleep(60)


# ----------------------
# FLASK APP
# ----------------------
app = Flask(__name__)


@app.route("/")
def index():
    keywords = load_keywords()
    formatted_keywords = []
    for kw in keywords:
        if kw.get("last_run_at"):
            dt = datetime.fromtimestamp(kw["last_run_at"])
            last_run = dt.strftime("%Y-%m-%d %H:%M")
        else:
            last_run = "Nooit"
        formatted_keywords.append(
            {
                "id": kw["id"],
                "term": kw["term"],
                "interval_minutes": kw.get("interval_minutes", 15),
                "last_run_at": last_run,
                "min_price": kw.get("min_price"),
                "max_price": kw.get("max_price"),
                "limit_per_run": kw.get("limit_per_run"),
            }
        )

    default_interval = get_config_value("default_interval_minutes", "15")
    default_limit_per_run = get_config_value("default_limit_per_run", "5")

    return render_template(
        "index.html",
        keywords=formatted_keywords,
        default_interval=default_interval,
        default_limit_per_run=default_limit_per_run,
    )


@app.route("/add", methods=["POST"])
def add_keyword():
    term = request.form.get("term", "").strip()
    min_price_str = request.form.get("min_price")
    max_price_str = request.form.get("max_price")

    if not term:
        return redirect(url_for("index"))

    cfg = get_config()
    interval_str = cfg.get("default_interval_minutes", "15")
    try:
        interval = int(interval_str)
    except ValueError:
        interval = 15
    if interval < 1:
        interval = 1

    default_limit_str = cfg.get("default_limit_per_run", "5")
    limit_per_run = normalize_limit(default_limit_str)

    min_price = _parse_int_or_none(min_price_str)
    max_price = _parse_int_or_none(max_price_str)

    keywords = load_keywords()
    new_id = max((kw["id"] for kw in keywords), default=0) + 1

    new_kw = {
        "id": new_id,
        "term": term,
        "interval_minutes": interval,
        "last_run_at": None,
        "min_price": min_price,
        "max_price": max_price,
        "limit_per_run": limit_per_run,
    }
    keywords.append(new_kw)
    save_keywords(keywords)

    return redirect(url_for("index"))


@app.route("/edit/<int:keyword_id>", methods=["POST"])
def edit_keyword(keyword_id):
    term = request.form.get("term", "").strip()
    interval_str = request.form.get("interval", "").strip()
    min_price_str = request.form.get("min_price")
    max_price_str = request.form.get("max_price")
    limit_str = request.form.get("limit_per_run")

    try:
        interval = int(interval_str)
    except ValueError:
        interval = 15
    if interval < 1:
        interval = 1

    min_price = _parse_int_or_none(min_price_str)
    max_price = _parse_int_or_none(max_price_str)
    limit_per_run = normalize_limit(limit_str)

    keywords = load_keywords()
    found = False
    for kw in keywords:
        if kw["id"] == keyword_id:
            kw["term"] = term
            kw["interval_minutes"] = interval
            kw["min_price"] = min_price
            kw["max_price"] = max_price
            kw["limit_per_run"] = limit_per_run
            found = True
            break

    if not found:
        abort(404)

    save_keywords(keywords)
    return redirect(url_for("index"))


@app.route("/delete/<int:keyword_id>", methods=["POST"])
def delete_keyword(keyword_id):
    keywords = load_keywords()
    keywords = [kw for kw in keywords if kw["id"] != keyword_id]
    save_keywords(keywords)
    delete_results_for_keyword(keyword_id)
    return redirect(url_for("index"))


@app.route("/reset/<int:keyword_id>", methods=["POST"])
def reset_keyword(keyword_id):
    keywords = load_keywords()
    found = False
    for kw in keywords:
        if kw["id"] == keyword_id:
            kw["last_run_at"] = None
            found = True
            break
    if not found:
        abort(404)
    save_keywords(keywords)
    delete_results_for_keyword(keyword_id)
    return redirect(url_for("index"))


@app.route("/search/<int:keyword_id>", methods=["POST"])
def manual_search(keyword_id):
    kw = find_keyword_by_id(keyword_id)
    if kw is None:
        abort(404)

    term = kw["term"]
    limit_per_run = normalize_limit(kw.get("limit_per_run"))
    print(f"[{datetime.now().isoformat()}] HANDMATIGE zoekactie voor '{term}'")

    try:
        ads = search_marktplaats(term, limit_per_run)
        ads = filter_ads_by_price(ads, kw)
        count = len(ads)
        print(f"  Handmatig: {count} resultaten na prijsfilter")

        now_ts = time.time()
        replace_results_for_keyword(keyword_id, ads, now_ts)

        # last_run_at updaten
        keywords = load_keywords()
        for item in keywords:
            if item["id"] == keyword_id:
                item["last_run_at"] = now_ts
                break
        save_keywords(keywords)

        cfg = get_config()
        manual_flag = (cfg.get("manual_telegram") or "nee").strip().lower()

        if ads and manual_flag == "ja":
            for ad in ads[:limit_per_run]:
                send_telegram_ad(ad)
        elif not ads and manual_flag == "ja":
            send_telegram_message(
                f"Geen resultaten gevonden voor '{term}' bij handmatige zoekactie."
            )

    except Exception as e:
        print(f"FOUT bij handmatige zoekactie voor '{term}': {e}")
        send_telegram_message(f"❌ Fout bij handmatige zoekactie voor '{term}': {e}")

    return redirect(url_for("results", keyword_id=keyword_id))


@app.route("/results/<int:keyword_id>")
def results(keyword_id):
    kw = find_keyword_by_id(keyword_id)
    if kw is None:
        abort(404)

    if kw.get("last_run_at"):
        last_run_dt = datetime.fromtimestamp(kw["last_run_at"])
        last_run = last_run_dt.strftime("%Y-%m-%d %H:%M")
    else:
        last_run = "Nooit"

    rows = get_results_for_keyword(keyword_id)
    results_formatted = []
    for r in rows:
        found_dt = datetime.fromtimestamp(r["found_at"])
        found_at_str = found_dt.strftime("%Y-%m-%d %H:%M")
        results_formatted.append(
            {
                "title": r["title"],
                "url": r["url"],
                "found_at": found_at_str,
                "price": r["price"] or "Onbekend",
            }
        )

    return render_template(
        "results.html",
        keyword_id=keyword_id,
        term=kw["term"],
        last_run=last_run,
        results=results_formatted,
    )


# ----------------------
# CONFIG + TEST TELEGRAM
# ----------------------
@app.route("/config", methods=["GET", "POST"])
def config_view():
    if request.method == "POST":
        cfg = get_config()

        default_interval = request.form.get("default_interval")
        if default_interval is None:
            default_interval = cfg.get("default_interval_minutes", "15")

        default_limit = request.form.get("default_limit_per_run")
        if default_limit is None:
            default_limit = cfg.get("default_limit_per_run", "5")

        sleep_mode = request.form.get("sleep_mode")
        if sleep_mode is None:
            sleep_mode = cfg.get("sleep_mode", "nee")

        sleep_start = request.form.get("sleep_start")
        if sleep_start is None:
            sleep_start = cfg.get("sleep_start", "23:00")

        sleep_end = request.form.get("sleep_end")
        if sleep_end is None:
            sleep_end = cfg.get("sleep_end", "07:00")

        postcode = request.form.get("postcode")
        if postcode is None:
            postcode = cfg.get("postcode", "")
        postcode = (postcode or "").strip().replace(" ", "").upper()

        radius = request.form.get("radius")
        if radius is None:
            radius = cfg.get("radius_km", "alle")
        radius = (radius or "alle").strip()

        telegram_bot_id = request.form.get("telegram_bot_id")
        if telegram_bot_id is None:
            telegram_bot_id = cfg.get("telegram_bot_id", "")

        telegram_chat_id = request.form.get("telegram_chat_id")
        if telegram_chat_id is None:
            telegram_chat_id = cfg.get("telegram_chat_id", "")

        manual_telegram = request.form.get("manual_telegram")
        if manual_telegram is None:
            manual_telegram = cfg.get("manual_telegram", "nee")

        try:
            di = int(default_interval)
        except ValueError:
            di = 15
        if di < 1:
            di = 1

        default_limit_norm = normalize_limit(default_limit)

        sleep_mode = (sleep_mode or "nee").strip().lower()
        manual_telegram = (manual_telegram or "nee").strip().lower()

        update_config(
            {
                "default_interval_minutes": str(di),
                "default_limit_per_run": str(default_limit_norm),
                "sleep_mode": "ja" if sleep_mode == "ja" else "nee",
                "sleep_start": sleep_start or "23:00",
                "sleep_end": sleep_end or "07:00",
                "postcode": postcode,
                "radius_km": radius or "alle",
                "telegram_bot_id": telegram_bot_id or "",
                "telegram_chat_id": telegram_chat_id or "",
                "manual_telegram": "ja" if manual_telegram == "ja" else "nee",
            }
        )

        return redirect(url_for("config_view"))

    cfg = get_config()
    default_interval = cfg.get("default_interval_minutes", "15")
    default_limit_per_run = cfg.get("default_limit_per_run", "5")
    telegram_bot_id = cfg.get("telegram_bot_id", "")
    telegram_chat_id = cfg.get("telegram_chat_id", "")
    postcode = cfg.get("postcode", "")
    radius = cfg.get("radius_km", "alle")
    manual_telegram = (cfg.get("manual_telegram") or "nee").strip().lower()
    sleep_mode = (cfg.get("sleep_mode") or "nee").strip().lower()
    sleep_start = cfg.get("sleep_start", "23:00")
    sleep_end = cfg.get("sleep_end", "07:00")

    radius_options = [
        ("3", "3 km"),
        ("5", "5 km"),
        ("10", "10 km"),
        ("15", "15 km"),
        ("25", "25 km"),
        ("50", "50 km"),
        ("75", "75 km"),
        ("alle", "Alle afstanden"),
    ]

    return render_template(
        "config.html",
        default_interval=default_interval,
        default_limit_per_run=default_limit_per_run,
        telegram_bot_id=telegram_bot_id,
        telegram_chat_id=telegram_chat_id,
        postcode=postcode,
        radius=radius,
        radius_options=radius_options,
        manual_telegram=manual_telegram,
        sleep_mode=sleep_mode,
        sleep_start=sleep_start,
        sleep_end=sleep_end,
    )


@app.route("/config/test-telegram", methods=["POST"])
def config_test_telegram():
    cfg = get_config()

    telegram_bot_id = (request.form.get("telegram_bot_id") or cfg.get("telegram_bot_id", "")).strip()
    telegram_chat_id = (request.form.get("telegram_chat_id") or cfg.get("telegram_chat_id", "")).strip()
    manual_telegram = (request.form.get("manual_telegram") or cfg.get("manual_telegram", "nee")).strip().lower()

    update_config(
        {
            "telegram_bot_id": telegram_bot_id,
            "telegram_chat_id": telegram_chat_id,
            "manual_telegram": "ja" if manual_telegram == "ja" else "nee",
        }
    )

    send_telegram_message("✅ Testbericht van Marktplaats Watcher (instellingen ook opgeslagen)")
    return redirect(url_for("config_view"))


# ----------------------
# START
# ----------------------
def start_worker():
    ensure_storage()
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()


if __name__ == "__main__":
    start_worker()
    app.run(host="0.0.0.0", port=8000)
