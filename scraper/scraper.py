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

MAX_PAGES = int(os.environ.get("MAX_PAGES", "0"))               # 0 = sınırsız (güvenlik limiti yok)
TIME_BUDGET_SECONDS = int(os.environ.get("TIME_BUDGET_SECONDS", "2400"))  # 40 dk: tarama+indirme TOPLAM bütçesi, commit'e pay bırakır
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

# Puanlama, yorum, giriş, arama gibi aksiyon linkleri: bunlar HTML içerik
# sayfası değildir, taranmamalı (örn. /media/media-ratings/7193/rate)
ACTION_URL_RE = re.compile(
    r"/media-ratings/"
    r"|/(rate|vote|comment|comments|report|quote|reply|login|logout|register"
    r"|search|print|share|embed|edit|delete|watch|unwatch|favorite|favorites"
    r"|subscribe|unsubscribe|follow|unfollow|attachment|attachments)(?:/|$|\?)",
    re.IGNORECASE,
)

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


# Tarama, sitenin tamamı yerine /media/ altındaki marka/model/sayfalama
# hiyerarşisiyle sınırlandırılır (örn. /media/page-2, /media/samsung/...).
# Bu hem daha hızlıdır hem de forumun alakasız bölümlerine sapmayı önler.
SCOPE_PREFIX = urlparse(START_URL).path.rstrip("/") or "/media"
SITE_HOST = urlparse(SITE_BASE).netloc.lower()

# Href, şema veya "/" olmadan doğrudan bir domain adıyla başlıyorsa
# (örn. "device-forum.com/membership/") bu, sitenin HTML'inde eksik yazılmış
# bir linktir; urljoin bunu mevcut sayfanın ALTINA göreceli path gibi ekler
# ve anlamsız/sonsuz URL'ler üretir. Böyle hamlar baştan elenir.
BARE_DOMAIN_HREF_RE = re.compile(r"^[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(/|$)")


def is_in_scope(url):
    if not is_same_domain(url):
        return False
    parsed = urlparse(url)
    if ACTION_URL_RE.search(parsed.path) or ACTION_URL_RE.search(parsed.query or ""):
        return False
    # Sitenin kendi domain adı, path içinde İKİNCİ kez geçiyorsa bu URL
    # bozuktur: sitede "https://" veya "/" olmadan yazılmış bir href
    # (örn. "device-forum.com/membership/"), tarayıcı tarafından mevcut
    # sayfanın ALTINA göreceli link gibi eklenmiş demektir
    # (örn. ".../media/299/device-forum.com/membership/"). Böyle
    # bozuk linkler sonsuz/anlamsız URL üretimine yol açar, tamamen elenir.
    if SITE_HOST and SITE_HOST in parsed.path.lower():
        return False
    path = parsed.path.rstrip("/") or "/"
    return path == SCOPE_PREFIX or path.startswith(SCOPE_PREFIX + "/")


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
def crawl_site(known_ids, deadline):
    """Aynı domain içinde tüm sayfaları BFS ile dolaşır, medya id'lerini toplar.

    known_ids: zaten indirilmiş medya id'lerinin kümesi. Bu id'lerin DETAY
    SAYFASI bir daha asla ziyaret edilmez (görseli zaten elimizde, başlığı
    da elimizde) -> her saatlik taramanın süresi, "toplam medya sayısı"na
    değil, sadece "sayfalama/liste sayfası sayısı + yeni öğe sayısı"na
    bağlı kalır.

    deadline: time.monotonic() cinsinden MUTLAK bitiş zamanı. Tüm çalıştırma
    (tarama + indirme) tek bir ortak bütçeyi paylaşır, böylece iş GitHub
    Actions'ın zaman aşımıyla asla ortadan kesilip commit atamadan iptal
    olmaz.
    """
    visited = set()
    queue = [normalize_url(START_URL)]
    media_ids = {}  # id -> {"page_url": ..., "title": ...}
    skipped_known = 0

    while queue and (MAX_PAGES <= 0 or len(visited) < MAX_PAGES):
        if time.monotonic() > deadline:
            log.warning(
                "Zaman bütçesi doldu; tarama bu çalıştırma için durduruluyor "
                "(%s sayfa tarandı, %s yeni görsel adayı bulundu). Bulunanlar "
                "indirilip rss.xml üretilecek; kalan sayfalar bir sonraki "
                "çalıştırmada taranacak.",
                len(visited), len(media_ids),
            )
            break

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
            raw_href = a["href"].strip()
            if BARE_DOMAIN_HREF_RE.match(raw_href):
                # Bozuk/eksik yazılmış href (şema veya "/" yok) -> atla
                continue

            href = urljoin(url, raw_href)
            href = normalize_url(href)
            if not is_in_scope(href):
                continue

            id_match = MEDIA_ID_RE.search(href)
            if id_match:
                media_id = id_match.group(1)

                if media_id in known_ids:
                    # Zaten indirilmiş -> hiç ilgilenme
                    skipped_known += 1
                    continue

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

                # ÖNEMLİ: medya detay sayfası (/media/{id}) BİR DAHA ZİYARET
                # EDİLMEZ. Başlık zaten yukarıda listeleme/thumbnail linkinden
                # alındı; detay sayfasına gidip başlığı "bir kez daha, biraz
                # daha iyi" almaya çalışmak, ziyaret edilecek sayfa sayısını
                # neredeyse ikiye katlıyor ve tüm zaman bütçesini taramada
                # tüketip indirmeye hiç zaman bırakmıyordu.
                continue

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
STATE_SAVE_EVERY = int(os.environ.get("STATE_SAVE_EVERY", "25"))  # her N indirmede bir ara kayıt
# Tek bir çalıştırmada indirilecek MAX yeni görsel sayısı. Bunu sınırlamak,
# git commit/push'un küçük ve güvenilir kalmasını sağlar -- binlerce yeni
# binary dosyayı tek seferde push etmeye çalışmak GitHub tarafında
# "HTTP 500 / remote end hung up" hatasına yol açabiliyor.
MAX_NEW_DOWNLOADS_PER_RUN = int(os.environ.get("MAX_NEW_DOWNLOADS_PER_RUN", "300"))


def download_new_images(media_ids, state, deadline):
    os.makedirs(IMAGES_DIR, exist_ok=True)
    downloaded = state.setdefault("downloaded", {})
    new_count = 0
    time_budget_hit = False

    for media_id, meta in media_ids.items():
        if media_id in downloaded:
            continue  # zaten indirilmiş -> tekrar indirilmiyor

        if MAX_NEW_DOWNLOADS_PER_RUN > 0 and new_count >= MAX_NEW_DOWNLOADS_PER_RUN:
            log.warning(
                "Bu çalıştırma için indirme limiti doldu (MAX_NEW_DOWNLOADS_PER_RUN=%s). "
                "Kalan yeni öğeler bir sonraki çalıştırmada indirilecek.",
                MAX_NEW_DOWNLOADS_PER_RUN,
            )
            break

        if time.monotonic() > deadline:
            time_budget_hit = True
            log.warning(
                "Zaman bütçesi doldu; indirme bu çalıştırma için durduruluyor "
                "(%s yeni görsel indirildi). Kalan yeni öğeler bir sonraki "
                "çalıştırmada indirilecek.",
                new_count,
            )
            break

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

        if new_count % STATE_SAVE_EVERY == 0:
            # Uzun indirme sırasında ara ara kaydet: iş sert şekilde
            # durdurulursa bile diskte en güncel ilerleme kalsın.
            try:
                save_state(state)
            except OSError as exc:
                log.error("Ara state kaydı başarısız: %s", exc)

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
        "SITE_BASE=%s START_URL=%s GITHUB_PAGES_BASE=%s SCOPE_PREFIX=%s TIME_BUDGET_SECONDS=%s",
        SITE_BASE, START_URL, GITHUB_PAGES_BASE, SCOPE_PREFIX, TIME_BUDGET_SECONDS,
    )

    # Tarama + indirme TEK bir ortak zaman bütçesini paylaşır. Bu sayede
    # süreç, GitHub Actions'ın job zaman aşımına yakalanıp rss.xml'i hiç
    # üretemeden/commit atamadan iptal edilemez.
    deadline = time.monotonic() + TIME_BUDGET_SECONDS if TIME_BUDGET_SECONDS > 0 else float("inf")

    state = load_state()
    known_ids = set(state.get("downloaded", {}).keys())
    log.info("Zaten indirilmiş (atlanacak) medya sayısı: %s", len(known_ids))

    try:
        media_ids = crawl_site(known_ids, deadline)
    except Exception as exc:
        log.exception("Tarama sırasında beklenmeyen hata: %s", exc)
        media_ids = {}

    if media_ids:
        try:
            download_new_images(media_ids, state, deadline)
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
