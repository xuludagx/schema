#!/usr/bin/env python3
"""
Device-Forum.com Medya Tarayıcı & RSS Üretici
------------------------------------------------
Tüm site genelinde (marka / model / tüm sayfalar) dolaşarak /media/
altındaki görselleri bulur, "full" (orijinal çözünürlük) versiyonlarını
indirir, GitHub Pages'te barındırılacak data/images/ klasörüne kaydeder
ve rss.xml üretir.

- Daha önce indirilmiş görseller state.json sayesinde tekrar indirilmez.
- Her çalıştırmada TÜM site yeniden taranır (yeni içerik kaçmasın diye),
  ama sadece state.json'da olmayan yeni id'ler indirilir.
- Ağ hataları, 404'ler, bozuk sayfalar programı durdurmaz; loglanıp
  bir sonraki adıma geçilir (hatasız / kesintisiz çalışma hedefi).
"""

import os
import re
import json
import time
import logging
import hashlib
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from xml.sax.saxutils import escape
from email.utils import format_datetime

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Ayarlar (ortam değişkenleri ile override edilebilir)
# ---------------------------------------------------------------------------
SITE_BASE = os.environ.get("SITE_BASE", "https://device-forum.com").rstrip("/")
START_URL = os.environ.get("START_URL", f"{SITE_BASE}/media/")

# ÖNEMLİ: Kendi GitHub Pages adresinizle değiştirin, örn:
# "https://kullaniciadi.github.io/device-forum-rss/"
GITHUB_PAGES_BASE = os.environ.get(
    "GITHUB_PAGES_BASE", "https://REPLACE_ME.github.io/REPO_NAME/"
).rstrip("/") + "/"

DATA_DIR = os.environ.get("DATA_DIR", "data")
IMAGES_DIR = os.path.join(DATA_DIR, "images")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
RSS_FILE = os.environ.get("RSS_FILE", "rss.xml")

MAX_PAGES = int(os.environ.get("MAX_PAGES", "5000"))            # güvenlik limiti (sayfa)
MAX_FEED_ITEMS = int(os.environ.get("MAX_FEED_ITEMS", "500"))   # rss.xml içine max öğe
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "0.4"))   # istekler arası bekleme (sn)
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "20"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (compatible; DeviceForumRSSBot/1.0; +https://github.com/)",
)

# /media/758  veya  /media/758/full  gibi linkleri yakalar
MEDIA_ID_RE = re.compile(r"/media/(\d+)(?:/full)?/?$")

# Başlıkların sonundaki dosya uzantısını temizlemek için
# örn: "SAMSUNG SM-G991 WIFI BT GPS.webp" -> "SAMSUNG SM-G991 WIFI BT GPS"
IMAGE_EXT_SUFFIX_RE = re.compile(
    r"\.(jpg|jpeg|png|webp|gif|bmp|avif|svg)\s*$", re.IGNORECASE
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("scraper")

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


# ---------------------------------------------------------------------------
# Yardımcı fonksiyonlar
# ---------------------------------------------------------------------------
def fetch(url, stream=False):
    """Yeniden denemeli, hataya dayanıklı GET isteği."""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT, stream=stream)
            if resp.status_code == 200:
                return resp
            if resp.status_code == 404:
                log.warning("404 (yok sayıldı): %s", url)
                return None
            log.warning(
                "HTTP %s (deneme %s/%s): %s", resp.status_code, attempt, MAX_RETRIES, url
            )
        except requests.RequestException as exc:
            last_exc = exc
            log.warning(
                "İstek hatası (deneme %s/%s) %s: %s", attempt, MAX_RETRIES, url, exc
            )
        time.sleep(REQUEST_DELAY * attempt)
    if last_exc:
        log.error("Vazgeçildi: %s (%s)", url, last_exc)
    return None


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.error("state.json okunamadı, sıfırdan başlanıyor: %s", exc)
    return {"downloaded": {}}


def save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp_path, STATE_FILE)


def guess_extension(resp, fallback_url):
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
        "image/avif": ".avif",
    }
    if ctype in mapping:
        return mapping[ctype]
    path_ext = os.path.splitext(urlparse(fallback_url).path)[1].lower()
    if path_ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"):
        return path_ext
    return ".jpg"


def is_same_domain(url):
    try:
        return urlparse(url).netloc == urlparse(SITE_BASE).netloc
    except ValueError:
        return False


def normalize_url(url):
    parsed = urlparse(url)
    parsed = parsed._replace(fragment="")
    return parsed.geturl()


def clean_title(text, fallback):
    """Sondaki dosya uzantısını ve fazla boşlukları temizler.

    "SAMSUNG SM-G991 WIFI BT GPS.webp" -> "SAMSUNG SM-G991 WIFI BT GPS"
    """
    if not text:
        return fallback
    text = " ".join(text.split())  # fazla boşluk/satır sonlarını sadeleştir
    text = IMAGE_EXT_SUFFIX_RE.sub("", text).strip()
    return text or fallback


# ---------------------------------------------------------------------------
# Site geneli tarama (marka / model / tüm sayfalar, BFS)
# ---------------------------------------------------------------------------
def crawl_site():
    """Aynı domain içinde tüm sayfaları BFS ile dolaşır, medya id'lerini toplar."""
    visited = set()
    queue = [normalize_url(START_URL)]
    media_ids = {}  # id -> {"page_url": ..., "title": ...}

    while queue and len(visited) < MAX_PAGES:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        resp = fetch(url)
        time.sleep(REQUEST_DELAY)
        if resp is None:
            continue

        content_type = resp.headers.get("Content-Type", "")
        if "text/html" not in content_type:
            continue

        try:
            soup = BeautifulSoup(resp.text, "lxml")
        except Exception as exc:  # bozuk HTML vs.
            log.warning("HTML parse edilemedi %s: %s", url, exc)
            continue

        # Bu sayfa doğrudan bir medya id'sine karşılık geliyor mu?
        m = MEDIA_ID_RE.search(url)
        if m:
            media_id = m.group(1)
            fallback = f"Media {media_id}"

            # Öncelik sırası: <img alt="...">/title -> og:image:alt -> h1 -> <title>
            title_source = None
            img_tag = soup.find("img", src=re.compile(r"/media/\d+/full"))
            if img_tag:
                title_source = img_tag.get("alt") or img_tag.get("title")
            if not title_source:
                og_alt = soup.find("meta", attrs={"property": "og:image:alt"})
                if og_alt and og_alt.get("content"):
                    title_source = og_alt["content"]
            if not title_source:
                h1_tag = soup.find("h1")
                if h1_tag:
                    title_source = h1_tag.get_text(strip=True)
            if not title_source:
                title_tag = soup.find("title")
                if title_tag:
                    title_source = title_tag.get_text(strip=True)

            title = clean_title(title_source, fallback)
            media_ids.setdefault(media_id, {"page_url": url, "title": title})

        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"])
            href = normalize_url(href)
            if not is_same_domain(href):
                continue

            id_match = MEDIA_ID_RE.search(href)
            if id_match:
                media_id = id_match.group(1)
                if media_id not in media_ids:
                    fallback = f"Media {media_id}"
                    # Link içindeki <img alt="..."> önceliklidir (genellikle dosya adı orada olur)
                    inner_img = a.find("img")
                    title_source = None
                    if inner_img:
                        title_source = inner_img.get("alt") or inner_img.get("title")
                    if not title_source:
                        title_source = a.get_text(strip=True) or a.get("title")
                    title = clean_title(title_source, fallback)
                    media_ids[media_id] = {"page_url": href, "title": title}

            if href not in visited and href not in queue:
                queue.append(href)

        if len(visited) % 50 == 0:
            log.info(
                "Taranan sayfa: %s | Bulunan görsel adayı: %s | Kuyrukta: %s",
                len(visited), len(media_ids), len(queue),
            )

    log.info(
        "Tarama tamamlandı. Toplam sayfa: %s, toplam görsel adayı: %s",
        len(visited), len(media_ids),
    )
    return media_ids


# ---------------------------------------------------------------------------
# İndirme (sadece yeni olanlar)
# ---------------------------------------------------------------------------
def download_new_images(media_ids, state):
    os.makedirs(IMAGES_DIR, exist_ok=True)
    downloaded = state.setdefault("downloaded", {})
    new_count = 0

    for media_id, meta in media_ids.items():
        if media_id in downloaded:
            continue  # zaten indirilmiş -> tekrar indirilmiyor

        full_url = f"{SITE_BASE}/media/{media_id}/full"
        resp = fetch(full_url, stream=True)
        time.sleep(REQUEST_DELAY)
        if resp is None:
            log.warning("Görsel indirilemedi, atlanıyor: %s", full_url)
            continue

        ext = guess_extension(resp, full_url)
        filename = f"{media_id}{ext}"
        filepath = os.path.join(IMAGES_DIR, filename)

        try:
            sha256 = hashlib.sha256()
            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        sha256.update(chunk)
        except OSError as exc:
            log.error("Dosya yazılamadı %s: %s", filepath, exc)
            continue

        downloaded[media_id] = {
            "filename": filename,
            "source_url": full_url,
            "page_url": meta.get("page_url", full_url),
            "title": meta.get("title", f"Media {media_id}"),
            "sha256": sha256.hexdigest(),
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
        }
        new_count += 1
        log.info("Yeni görsel indirildi: %s (%s)", filename, meta.get("title"))

    log.info("Yeni indirilen görsel sayısı: %s", new_count)
    return new_count


# ---------------------------------------------------------------------------
# RSS üretimi (standart kütüphane ile, dış bağımlılık yok -> daha güvenilir)
# ---------------------------------------------------------------------------
MIME_BY_EXT = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp",
    "avif": "image/avif",
}


def _rfc822(dt):
    return format_datetime(dt)


def generate_rss(state):
    feed_link = GITHUB_PAGES_BASE
    self_link = urljoin(GITHUB_PAGES_BASE, RSS_FILE)

    items = sorted(
        state.get("downloaded", {}).items(),
        key=lambda kv: kv[1].get("downloaded_at", ""),
        reverse=True,
    )[:MAX_FEED_ITEMS]

    now = datetime.now(timezone.utc)

    item_xml_parts = []
    for media_id, meta in items:
        image_url = urljoin(GITHUB_PAGES_BASE, f"{DATA_DIR}/images/{meta['filename']}")
        title = meta.get("title", f"Media {media_id}") or f"Media {media_id}"
        ext = os.path.splitext(meta["filename"])[1].lstrip(".").lower()
        mime = MIME_BY_EXT.get(ext, "image/jpeg")

        try:
            pub_dt = datetime.fromisoformat(meta["downloaded_at"])
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        except (KeyError, ValueError, TypeError):
            pub_dt = now

        # sha256 dosya boyutu yerine kullanılabilir bir gösterge olarak
        # enclosure "length" alanına 0 koyuyoruz; gerçek boyut isteğe
        # bağlı olarak os.path.getsize ile eklenebilir.
        img_path = os.path.join(IMAGES_DIR, meta["filename"])
        try:
            length = os.path.getsize(img_path)
        except OSError:
            length = 0

        description_html = f"<img src=\"{image_url}\" alt=\"{title}\"/>"

        item_xml_parts.append(
            "    <item>\n"
            f"      <title>{escape(title)}</title>\n"
            f"      <link>{escape(image_url)}</link>\n"
            f"      <guid isPermaLink=\"true\">{escape(image_url)}</guid>\n"
            f"      <pubDate>{escape(_rfc822(pub_dt))}</pubDate>\n"
            f"      <description>{escape(description_html)}</description>\n"
            f"      <enclosure url=\"{escape(image_url)}\" length=\"{length}\" type=\"{mime}\"/>\n"
            "    </item>\n"
        )

    rss_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        "    <title>Device Forum - Görsel Akışı</title>\n"
        f"    <link>{escape(feed_link)}</link>\n"
        f'    <atom:link href="{escape(self_link)}" rel="self" type="application/rss+xml"/>\n'
        "    <description>device-forum.com sitesinden otomatik çekilen görseller (full çözünürlük).</description>\n"
        "    <language>tr</language>\n"
        f"    <lastBuildDate>{escape(_rfc822(now))}</lastBuildDate>\n"
        f"{''.join(item_xml_parts)}"
        "  </channel>\n"
        "</rss>\n"
    )

    os.makedirs(os.path.dirname(RSS_FILE) or ".", exist_ok=True)
    tmp_path = RSS_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(rss_xml)
    os.replace(tmp_path, RSS_FILE)
    log.info("rss.xml oluşturuldu: %s (%s öğe)", RSS_FILE, len(items))


# ---------------------------------------------------------------------------
# Ana akış
# ---------------------------------------------------------------------------
def main():
    log.info("=== Device Forum Scraper başlıyor ===")
    log.info(
        "SITE_BASE=%s START_URL=%s GITHUB_PAGES_BASE=%s",
        SITE_BASE, START_URL, GITHUB_PAGES_BASE,
    )

    state = load_state()

    try:
        media_ids = crawl_site()
    except Exception as exc:
        log.exception("Tarama sırasında beklenmeyen hata: %s", exc)
        media_ids = {}

    if media_ids:
        try:
            download_new_images(media_ids, state)
        except Exception as exc:
            log.exception("İndirme sırasında beklenmeyen hata: %s", exc)
        finally:
            save_state(state)
    else:
        log.warning("Hiç medya bulunamadı; state güncellenmedi.")

    try:
        generate_rss(state)
    except Exception as exc:
        log.exception("RSS üretilirken hata: %s", exc)

    log.info("=== Tamamlandı ===")


if __name__ == "__main__":
    main()
