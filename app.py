import os
import json
import sqlite3
import threading
import time as time_module
from datetime import datetime, time, timedelta
from pathlib import Path

import requests
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
)

# ------------------------------------------------------------------------------
# Basisconfiguratie
# ------------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "mpwatchter-dev")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.environ.get("MPWATCHTER_CONFIG_DIR", "/config"))
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

SETTINGS_FILE = CONFIG_DIR / "settings.json"
KEYWORDS_FILE = CONFIG_DIR / "keywords.json"
DB_FILE = CONFIG_DIR / "results.db"

# ------------------------------------------------------------------------------
# Default settings & helpers
# ------------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "default_interval_minutes": 15,
    "default_limit_per_run": 5,
    "sleep_mode": "nee",       # "ja" / "nee"
    "sleep_start": "23:00",    # HH:MM
    "sleep_end": "07:00",      # HH:MM
    "postcode": "",
    "radius_km": "alle",       # wordt vertaald naar distanceMeters voor de API
    "telegram_bot_id": "",
    "telegram_chat_id": "",
    "manual_telegram": "nee",  # "ja" / "nee"
}


def load_settings() -> dict:
    if not SETTINGS_FILE.exists():
        save_settings(DEFAULT_SETTINGS)
        return DEFAULT_SETTINGS.copy()
    try:
        with SETTINGS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    merged = DEFAULT_SETTINGS.copy()
    merged.update(data or {})
    # normaliseer types
    merged["default_interval_minutes"] = int(merged.get("default_interval_minutes", 15) or 15)
    merged["default_limit_per_run"] = int(merged.get("default_limit_per_run", 5) or 5)
    return merged


def save_settings(settings: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with SETTINGS_FILE.open("w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def load_keywords() -> list[dict]:
    """
    Laad keywords uit keywords.json en normaliseer naar een lijst van dicts.
    Kan overweg met:
    - nieuwe structuur: [ { ... }, { ... } ]
    - fallback: { "keywords": [ ... ] }
    - oude structuren / plain strings: "lego", "switch", ...
    """
    if not KEYWORDS_FILE.exists():
        return []

    try:
        with KEYWORDS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []

    if isinstance(data, list):
        raw_list = data
    elif isinstance(data, dict):
        if "keywords" in data and isinstance(data["keywords"], list):
            raw_list = data["keywords"]
        else:
            raw_list = [data]
    else:
        return []

    normed: list[dict] = []
    for item in raw_list:
        if not isinstance(item, dict):
            item = {"term": str(item)}

        item.setdefault("id", None)
        item.setdefault("term", "")
        item.setdefault("interval_minutes", None)
        item.setdefault("min_price", None)
        item.setdefault("max_price", None)
        item.setdefault("limit_per_run", None)
        item.setdefault("last_run_at", "Nooit")

        normed.append(item)

    return normed


def save_keywords(keywords: list[dict]) -> None:
    for idx, kw in enumerate(keywords, start=1):
        if kw.get("id") is None:
            kw["id"] = idx
    with KEYWORDS_FILE.open("w", encoding="utf-8") as f:
        json.dump(keywords, f, ensure_ascii=False, indent=2)


def init_db() -> None:
    conn = sqlite3.connect(DB_FILE)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword_id INTEGER NOT NULL,
                ad_id TEXT NOT NULL,
                title TEXT,
                price TEXT,
                url TEXT,
                image_url TEXT,
                first_seen_at TEXT,
                posted_at TEXT,
                UNIQUE(keyword_id, ad_id)
            )
            """
        )
        conn.commit()

        # Zorg dat oudere DB's de kolom posted_at ook krijgen
        cur.execute("PRAGMA table_info(results)")
        cols = [row[1] for row in cur.fetchall()]
        if "posted_at" not in cols:
            cur.execute("ALTER TABLE results ADD COLUMN posted_at TEXT")
            conn.commit()
    finally:
        conn.close()


def parse_time_str(value: str) -> time:
    try:
        hh, mm = value.strip().split(":")
        return time(int(hh), int(mm))
    except Exception:
        return time(23, 0)


def is_in_sleep_window(now_t: time, start: time, end: time) -> bool:
    if start < end:
        return start <= now_t < end
    return now_t >= start or now_t < end


# ------------------------------------------------------------------------------
# Marktplaats URL-bouwer – voor GUI-link
# ------------------------------------------------------------------------------

def build_marktplaats_search_url(
    term: str,
    postcode: str | None = None,
    radius_km: str | None = None,
) -> str:
    """
    Bouw Marktplaats-zoek-URL voor de GUI:

    https://www.marktplaats.nl/q/lego+21026/
    #offeredSince:Altijd|sortBy:SORT_INDEX|sortOrder:DECREASING
    |distanceMeters:75000
    |postcode:3208BJ
    """
    query = term.strip().replace(" ", "+")
    base = f"https://www.marktplaats.nl/q/{query}/#offeredSince:Altijd|sortBy:SORT_INDEX|sortOrder:DECREASING"

    if radius_km and radius_km != "alle":
        try:
            meters = int(radius_km) * 1000
            base += f"|distanceMeters:{meters}"
        except ValueError:
            pass

    if postcode:
        base += f"|postcode:{postcode}"

    return base


# ------------------------------------------------------------------------------
# Helpers: plaatsingsdatum uit JSON trekken / parsen
# ------------------------------------------------------------------------------

def _format_epoch_to_str(v) -> str:
    try:
        ts = float(v)
    except Exception:
        return ""
    if ts > 10**12:
        ts = ts / 1000.0
    try:
        dt = datetime.fromtimestamp(ts)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _extract_posted_at(item: dict) -> str:
    """
    Probeer uit de Marktplaats JSON een plaatsingsdatum/tijd te halen.
    """
    for key in ("date", "dateTime", "startTime", "startDateTime", "startDate", "postedAt"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            s = _format_epoch_to_str(v)
            if s:
                return s
        if isinstance(v, dict):
            for sub in ("value", "date", "iso", "iso8601"):
                sv = v.get(sub)
                if isinstance(sv, str) and sv.strip():
                    return sv.strip()
                if isinstance(sv, (int, float)):
                    s = _format_epoch_to_str(sv)
                    if s:
                        return s

    nested_keys = ("dateInfo", "metadata", "timing")
    for nk in nested_keys:
        nv = item.get(nk)
        if isinstance(nv, dict):
            for sub in ("date", "dateTime", "start", "value"):
                sv = nv.get(sub)
                if isinstance(sv, str) and sv.strip():
                    return sv.strip()
                if isinstance(sv, (int, float)):
                    s = _format_epoch_to_str(sv)
                    if s:
                        return s

    return ""


def parse_posted_at_to_dt(posted_at: str | None, fallback_first_seen: str | None) -> datetime:
    """
    Zet 'posted_at' om naar datetime zodat we op nieuw -> oud kunnen sorteren.

    Ondersteunt o.a.:
    - ISO strings (2025-11-23 12:34, 2025-11-23T12:34:00)
    - '8 sep 25', '23 nov 25'
    Valt terug op first_seen_at als parsing niet lukt.
    """
    if posted_at:
        s = posted_at.strip()
        if s:
            for sep in [",", "|"]:
                if sep in s:
                    s = s.split(sep, 1)[0].strip()

            try:
                return datetime.fromisoformat(s)
            except Exception:
                pass

            month_map = {
                "jan": 1, "feb": 2, "mrt": 3, "apr": 4, "mei": 5, "jun": 6,
                "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12,
            }
            try:
                parts = s.replace(".", "").split()
                if len(parts) >= 3:
                    day = int(parts[0])
                    mon_abbr = parts[1].lower()
                    year = int(parts[2])
                    if year < 100:
                        year += 2000
                    month = month_map.get(mon_abbr)
                    if month:
                        return datetime(year, month, day)
            except Exception:
                pass

    if fallback_first_seen:
        try:
            return datetime.fromisoformat(fallback_first_seen)
        except Exception:
            pass

    return datetime.min


# ------------------------------------------------------------------------------
# Scrapen via JSON API
# ------------------------------------------------------------------------------

def fetch_marktplaats_results(term: str, settings: dict, limit: int) -> list[dict]:
    """
    Haal advertenties op voor een zoekterm, gesorteerd op nieuw → oud
    via https://www.marktplaats.nl/lrp/api/search
    """
    postcode = settings.get("postcode") or None
    radius_km = settings.get("radius_km", "alle")

    distance_meters = None
    if radius_km and radius_km != "alle":
        try:
            distance_meters = int(radius_km) * 1000
        except Exception:
            distance_meters = None

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        )
    }

    api_url = "https://www.marktplaats.nl/lrp/api/search"
    params = {
        "query": term,
        "sortBy": "SORT_INDEX",
        "sortOrder": "DECREASING",
        "viewOptions": "list-view",
        "limit": limit,
        "offset": 0,
    }
    if postcode:
        params["postcode"] = postcode
    if distance_meters is not None:
        params["distanceMeters"] = distance_meters

    results: list[dict] = []

    try:
        resp = requests.get(api_url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        def find_listings(obj):
            if isinstance(obj, list):
                if obj and isinstance(obj[0], dict) and ("itemId" in obj[0] or "id" in obj[0]) and "title" in obj[0]:
                    return obj
                for item in obj:
                    res = find_listings(item)
                    if res is not None:
                        return res
            elif isinstance(obj, dict):
                for v in obj.values():
                    res = find_listings(v)
                    if res is not None:
                        return res
            return None

        listings = find_listings(data)

        if isinstance(listings, list):
            for item in listings[:limit]:
                ad_id = str(item.get("itemId") or item.get("id") or "")
                title = item.get("title") or ""
                price = ""
                url = ""
                image_url = ""
                posted_at = _extract_posted_at(item)

                price_info = item.get("priceInfo") or {}
                if "priceDisplay" in price_info:
                    price = price_info["priceDisplay"]
                elif "priceCents" in price_info:
                    cents = price_info["priceCents"]
                    if cents is not None:
                        price = f"€ {cents/100:.2f}".replace(".", ",")

                url_path = item.get("url") or item.get("vipUrl") or item.get("relativeUrl")
                if url_path:
                    if url_path.startswith("http"):
                        url = url_path
                    else:
                        url = f"https://www.marktplaats.nl{url_path}"

                media = item.get("media") or {}
                if isinstance(media, dict):
                    imgs = media.get("images")
                    if isinstance(imgs, list) and imgs:
                        image_url = imgs[0].get("url") or ""
                elif isinstance(media, list) and media:
                    image_url = media[0].get("url") or ""

                if not ad_id or not url:
                    continue

                results.append(
                    {
                        "ad_id": ad_id,
                        "title": title,
                        "price": price,
                        "url": url,
                        "image_url": image_url,
                        "posted_at": posted_at,
                    }
                )

    except Exception:
        return []

    return results[:limit]


# ------------------------------------------------------------------------------
# DB helpers
# ------------------------------------------------------------------------------

def store_new_results(keyword_id: int, ads: list[dict]) -> list[dict]:
    if not ads:
        return []

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    new_ads: list[dict] = []
    try:
        cur = conn.cursor()
        for ad in ads:
            try:
                cur.execute(
                    """
                    INSERT INTO results (keyword_id, ad_id, title, price, url, image_url, first_seen_at, posted_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        keyword_id,
                        ad["ad_id"],
                        ad.get("title", ""),
                        ad.get("price", ""),
                        ad.get("url", ""),
                        ad.get("image_url", ""),
                        datetime.now().isoformat(timespec="seconds"),
                        ad.get("posted_at", ""),
                    ),
                )
                new_ads.append(ad)
            except sqlite3.IntegrityError:
                continue
        conn.commit()
    finally:
        conn.close()
    return new_ads


def get_results_for_keyword(keyword_id: int, limit: int = 50) -> list[dict]:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT title, price, url, image_url, first_seen_at, posted_at
            FROM results
            WHERE keyword_id = ?
            """,
            (keyword_id,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    ads: list[dict] = []
    for r in rows:
        d = dict(r)
        sort_dt = parse_posted_at_to_dt(
            d.get("posted_at"),
            d.get("first_seen_at"),
        )
        d["_sort_dt"] = sort_dt
        ads.append(d)

    ads.sort(key=lambda x: x["_sort_dt"], reverse=True)

    for d in ads:
        d.pop("_sort_dt", None)

    return ads[:limit]


def reset_results_for_keyword(keyword_id: int) -> None:
    conn = sqlite3.connect(DB_FILE)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM results WHERE keyword_id = ?", (keyword_id,))
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------------------
# Telegram helpers
# ------------------------------------------------------------------------------

def send_telegram_message(text: str, settings: dict) -> None:
    bot_id = settings.get("telegram_bot_id") or ""
    chat_id = settings.get("telegram_chat_id") or ""
    if not bot_id or not chat_id:
        return

    url = f"https://api.telegram.org/bot{bot_id}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass


def send_telegram_ad(ad: dict, settings: dict) -> None:
    bot_id = settings.get("telegram_bot_id") or ""
    chat_id = settings.get("telegram_chat_id") or ""
    if not bot_id or not chat_id:
        return

    title = ad.get("title", "").strip()
    price = ad.get("price", "").strip()
    url = ad.get("url", "").strip()
    image_url = ad.get("image_url", "").strip()
    posted_at = (ad.get("posted_at") or "").strip()

    if posted_at:
        caption = f"Titel = {title}\nPrijs = {price}\nDatum = {posted_at}"
    else:
        caption = f"Titel = {title}\nPrijs = {price}"

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "Bekijk advertentie", "url": url}
            ]
        ]
    }

    try:
        if image_url:
            api_url = f"https://api.telegram.org/bot{bot_id}/sendPhoto"
            payload = {
                "chat_id": chat_id,
                "photo": image_url,
                "caption": caption,
                "parse_mode": "Markdown",
                "reply_markup": reply_markup,
            }
            requests.post(api_url, json=payload, timeout=10)
        else:
            api_url = f"https://api.telegram.org/bot{bot_id}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": caption,
                "parse_mode": "Markdown",
                "reply_markup": reply_markup,
            }
            requests.post(api_url, json=payload, timeout=10)
    except Exception:
        pass


# ------------------------------------------------------------------------------
# Kern zoeken
# ------------------------------------------------------------------------------

def run_search_for_keyword(keyword: dict, settings: dict, manual: bool = False) -> tuple[int, int]:
    term = keyword["term"]
    interval = int(keyword.get("interval_minutes") or settings["default_interval_minutes"])
    limit_per_run = int(keyword.get("limit_per_run") or settings["default_limit_per_run"])
    min_price = keyword.get("min_price")
    max_price = keyword.get("max_price")

    limit_per_run = max(1, min(20, limit_per_run))

    raw_ads = fetch_marktplaats_results(term, settings, limit_per_run)

    def parse_price_to_int(p: str) -> int | None:
        if not p:
            return None
        digits = "".join(ch for ch in p if ch.isdigit())
        if not digits:
            return None
        try:
            return int(digits)
        except ValueError:
            return None

    filtered_ads: list[dict] = []
    for ad in raw_ads:
        p_int = parse_price_to_int(ad.get("price", ""))
        if min_price not in (None, ""):
            try:
                if p_int is None or p_int < int(min_price) * 100:
                    continue
            except ValueError:
                pass
        if max_price not in (None, ""):
            try:
                if p_int is None or p_int > int(max_price) * 100:
                    continue
            except ValueError:
                pass
        filtered_ads.append(ad)

    new_ads = store_new_results(keyword_id=keyword["id"], ads=filtered_ads)

    manual_telegram_on = (settings.get("manual_telegram", "nee").lower() == "ja")

    if not manual:
        for ad in new_ads:
            send_telegram_ad(ad, settings)
    else:
        if manual_telegram_on and new_ads:
            for ad in new_ads:
                send_telegram_ad(ad, settings)

    return len(filtered_ads), len(new_ads)


# ------------------------------------------------------------------------------
# Background worker
# ------------------------------------------------------------------------------

_worker_thread_started = False
_worker_lock = threading.Lock()


def scheduler_loop():
    while True:
        try:
            settings = load_settings()
            keywords = load_keywords()
            if not keywords:
                time_module.sleep(30)
                continue

            sleep_mode = settings.get("sleep_mode", "nee").lower() == "ja"
            sleep_start = parse_time_str(settings.get("sleep_start", "23:00"))
            sleep_end = parse_time_str(settings.get("sleep_end", "07:00"))

            now = datetime.now()
            now_t = now.time()

            in_sleep = sleep_mode and is_in_sleep_window(now_t, sleep_start, sleep_end)

            for kw in keywords:
                if not kw.get("term"):
                    continue

                interval_minutes = int(kw.get("interval_minutes") or settings["default_interval_minutes"])
                interval = timedelta(minutes=interval_minutes)

                if in_sleep and interval < timedelta(hours=1):
                    eff_interval = timedelta(hours=1)
                else:
                    eff_interval = interval

                last_run_at_str = kw.get("last_run_at") or "Nooit"
                last_dt = None
                if last_run_at_str != "Nooit":
                    try:
                        last_dt = datetime.fromisoformat(last_run_at_str)
                    except Exception:
                        last_dt = None

                if (last_dt is None) or (now - last_dt >= eff_interval):
                    try:
                        total, new_count = run_search_for_keyword(kw, settings, manual=False)
                        kw["last_run_at"] = datetime.now().isoformat(timespec="seconds")
                        save_keywords(keywords)
                    except Exception:
                        continue

            time_module.sleep(60)
        except Exception:
            time_module.sleep(60)


def start_background_worker():
    global _worker_thread_started
    with _worker_lock:
        if not _worker_thread_started:
            init_db()
            t = threading.Thread(target=scheduler_loop, daemon=True)
            t.start()
            _worker_thread_started = True


# ------------------------------------------------------------------------------
# Routes – UI
# ------------------------------------------------------------------------------

@app.route("/")
def index():
    settings = load_settings()
    keywords = load_keywords()

    postcode = (settings.get("postcode") or "").strip()
    radius_km = (settings.get("radius_km") or "alle").strip()

    for kw in keywords:
        kw["mp_url"] = build_marktplaats_search_url(
            kw["term"],
            postcode=postcode if postcode else None,
            radius_km=radius_km,
        )

    return render_template(
        "index.html",
        keywords=keywords,
        default_interval=settings["default_interval_minutes"],
        default_limit_per_run=settings["default_limit_per_run"],
        postcode=postcode,
    )


@app.route("/keyword/add", methods=["POST"])
def add_keyword():
    settings = load_settings()
    keywords = load_keywords()

    term = (request.form.get("term") or "").strip()
    if not term:
        flash("Zoekwoord mag niet leeg zijn.", "error")
        return redirect(url_for("index"))

    min_price = request.form.get("min_price")
    max_price = request.form.get("max_price")

    next_id = 1
    if keywords:
        next_id = max((int(k.get("id") or 0) for k in keywords), default=0) + 1

    kw = {
        "id": next_id,
        "term": term,
        "interval_minutes": settings["default_interval_minutes"],
        "min_price": min_price if min_price else None,
        "max_price": max_price if max_price else None,
        "limit_per_run": settings["default_limit_per_run"],
        "last_run_at": "Nooit",
    }
    keywords.append(kw)
    save_keywords(keywords)
    flash(f"Zoekwoord '{term}' toegevoegd.", "success")
    return redirect(url_for("index"))


@app.route("/keyword/<int:keyword_id>/edit", methods=["POST"])
def edit_keyword(keyword_id: int):
    settings = load_settings()
    keywords = load_keywords()
    kw = next((k for k in keywords if int(k.get("id")) == keyword_id), None)
    if not kw:
        flash("Zoekwoord niet gevonden.", "error")
        return redirect(url_for("index"))

    term = (request.form.get("term") or kw["term"]).strip()
    interval = request.form.get("interval")
    min_price = request.form.get("min_price")
    max_price = request.form.get("max_price")
    limit_per_run = request.form.get("limit_per_run")

    kw["term"] = term
    if interval:
        try:
            kw["interval_minutes"] = max(1, int(interval))
        except ValueError:
            pass

    kw["min_price"] = min_price if min_price not in ("", None) else None
    kw["max_price"] = max_price if max_price not in ("", None) else None

    if limit_per_run:
        try:
            l = int(limit_per_run)
            l = max(1, min(20, l))
            kw["limit_per_run"] = l
        except ValueError:
            pass

    save_keywords(keywords)
    flash(f"Zoekwoord '{term}' bijgewerkt.", "success")
    return redirect(url_for("index"))


@app.route("/keyword/<int:keyword_id>/manual", methods=["POST"])
def manual_search(keyword_id: int):
    settings = load_settings()
    keywords = load_keywords()
    kw = next((k for k in keywords if int(k.get("id") or 0) == keyword_id), None)
    if not kw:
        flash("Zoekwoord niet gevonden.", "error")
        return redirect(url_for("index"))

    total, new_count = run_search_for_keyword(kw, settings, manual=True)
    kw["last_run_at"] = datetime.now().isoformat(timespec="seconds")
    save_keywords(keywords)

    flash(
        f"Handmatige zoekactie voor '{kw['term']}' uitgevoerd "
        f"({total} resultaten, {new_count} nieuw).",
        "success",
    )
    return redirect(url_for("index"))


@app.route("/keyword/<int:keyword_id>/reset", methods=["POST"])
def reset_keyword(keyword_id: int):
    keywords = load_keywords()
    kw = next((k for k in keywords if int(k.get("id") or 0) == keyword_id), None)
    if not kw:
        flash("Zoekwoord niet gevonden.", "error")
        return redirect(url_for("index"))

    reset_results_for_keyword(keyword_id)
    kw["last_run_at"] = "Nooit"
    save_keywords(keywords)
    flash(f"Resultaten voor '{kw['term']}' zijn gereset.", "success")
    return redirect(url_for("index"))


@app.route("/keyword/<int:keyword_id>/delete", methods=["POST"])
def delete_keyword(keyword_id: int):
    keywords = load_keywords()
    remaining = [k for k in keywords if int(k.get("id") or 0) != keyword_id]
    if len(remaining) == len(keywords):
        flash("Zoekwoord niet gevonden.", "error")
        return redirect(url_for("index"))

    save_keywords(remaining)
    reset_results_for_keyword(keyword_id)
    flash("Zoekwoord verwijderd.", "success")
    return redirect(url_for("index"))


@app.route("/keyword/<int:keyword_id>/results")
def results(keyword_id: int):
    keywords = load_keywords()
    kw = next((k for k in keywords if int(k.get("id") or 0) == keyword_id), None)
    if not kw:
        flash("Zoekwoord niet gevonden.", "error")
        return redirect(url_for("index"))

    ads = get_results_for_keyword(keyword_id, limit=100)
    return render_template(
        "results.html",
        keyword=kw,
        ads=ads,
    )


# ------------------------------------------------------------------------------
# Configuratie UI
# ------------------------------------------------------------------------------

@app.route("/config", methods=["GET"])
def config_view():
    settings = load_settings()
    return render_template("config.html", settings=settings)


@app.route("/config/timer", methods=["POST"])
def config_save_timer():
    settings = load_settings()

    default_interval = request.form.get("default_interval_minutes")
    default_limit = request.form.get("default_limit_per_run")
    sleep_mode = request.form.get("sleep_mode", "nee")
    sleep_start = request.form.get("sleep_start", "23:00")
    sleep_end = request.form.get("sleep_end", "07:00")
    postcode = request.form.get("postcode", "").strip()
    radius_km = request.form.get("radius_km", "alle")

    try:
        settings["default_interval_minutes"] = max(1, int(default_interval))
    except Exception:
        pass

    try:
        l = int(default_limit)
        settings["default_limit_per_run"] = max(1, min(20, l))
    except Exception:
        pass

    settings["sleep_mode"] = "ja" if sleep_mode.lower() == "ja" else "nee"
    settings["sleep_start"] = sleep_start or "23:00"
    settings["sleep_end"] = sleep_end or "07:00"
    settings["postcode"] = postcode
    settings["radius_km"] = radius_km or "alle"

    save_settings(settings)
    flash("Timer-, slaap- en locatie-instellingen opgeslagen.", "success")
    return redirect(url_for("config_view"))


@app.route("/config/telegram", methods=["POST"])
def config_save_telegram():
    settings = load_settings()

    bot_id = request.form.get("telegram_bot_id", "").strip()
    chat_id = request.form.get("telegram_chat_id", "").strip()
    manual_telegram = request.form.get("manual_telegram", "nee")

    settings["telegram_bot_id"] = bot_id
    settings["telegram_chat_id"] = chat_id
    settings["manual_telegram"] = "ja" if manual_telegram.lower() == "ja" else "nee"

    save_settings(settings)
    flash("Telegram-instellingen opgeslagen.", "success")
    return redirect(url_for("config_view"))


@app.route("/config/telegram/test", methods=["POST"])
def config_test_telegram():
    settings = load_settings()
    send_telegram_message("✅ Testbericht van Marktplaats Watcher (instellingen ook opgeslagen)", settings)
    flash("Testbericht naar Telegram verstuurd (indien juist geconfigureerd).", "success")
    return redirect(url_for("config_view"))


# ------------------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------------------

start_background_worker()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)