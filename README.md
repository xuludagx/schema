# Device Forum RSS (device-forum.com görsel akışı)

`device-forum.com/media/` altındaki tüm marka/model sayfalarını tarar,
her görselin **full** (orijinal çözünürlük) versiyonunu indirir
(`https://device-forum.com/media/{id}/full`), bunları bu reponun
`data/images/` klasöründe saklar ve GitHub Pages linkleriyle bir
`rss.xml` üretir. GitHub Actions ile **her saat başı** otomatik çalışır.

Daha önce indirilen görseller `data/state.json` sayesinde tekrar
indirilmez.

## Kurulum (5 adım)

1. **Bu klasörü bir GitHub reposuna gönderin**
   ```bash
   cd device-forum-rss
   git init
   git add .
   git commit -m "İlk kurulum"
   git branch -M main
   git remote add origin https://github.com/KULLANICI_ADIN/REPO_ADIN.git
   git push -u origin main
   ```

2. **GitHub Pages'i açın**
   Repo > Settings > Pages > "Deploy from a branch" > Branch: `main`, klasör: `/ (root)`.
   Birkaç dakika sonra siteniz şu adreste yayınlanır:
   `https://KULLANICI_ADIN.github.io/REPO_ADIN/`

3. **PAGES_BASE_URL değişkenini tanımlayın**
   Repo > Settings > Secrets and variables > Actions > **Variables** sekmesi >
   "New repository variable":
   - Name: `PAGES_BASE_URL`  (⚠️ isim `GITHUB_` ile başlayamaz, GitHub buna izin vermiyor)
   - Value: `https://KULLANICI_ADIN.github.io/REPO_ADIN/` (sonunda `/` olsun)

   Örneğin repo `xuludagx/schema` ise değer:
   `https://xuludagx.github.io/schema/`

4. **Actions'ın çalışmasına izin verin**
   Repo > Settings > Actions > General > "Workflow permissions" >
   **Read and write permissions** seçili olmalı (workflow'un commit/push
   yapabilmesi için).

5. **İlk çalıştırmayı manuel tetikleyin**
   Repo > Actions > "Device Forum RSS - Saatlik Güncelleme" > "Run workflow".
   Bundan sonra her saat başı (UTC) otomatik çalışır; ayrıca istediğiniz an
   manuel de tetikleyebilirsiniz.

## RSS linkiniz

Kurulum tamamlandıktan ve ilk çalıştırma bittikten sonra RSS beslemeniz:

```
https://KULLANICI_ADIN.github.io/REPO_ADIN/rss.xml
```

## Yerelde test etmek isterseniz

```bash
cd scraper
pip install -r requirements.txt
export GITHUB_PAGES_BASE="https://KULLANICI_ADIN.github.io/REPO_ADIN/"
python scraper.py
```

Çalıştıktan sonra proje kökünde `rss.xml`, ve `data/images/` klasöründe
indirilen görselleri, `data/state.json` içinde de hangi id'lerin
indirildiğini görürsünüz.

## Ayarlanabilir ortam değişkenleri

| Değişken           | Varsayılan                         | Açıklama                                      |
|--------------------|-------------------------------------|------------------------------------------------|
| `SITE_BASE`        | `https://device-forum.com`          | Taranacak sitenin kök adresi                   |
| `START_URL`        | `SITE_BASE/media/`                  | Taramanın başlayacağı sayfa                    |
| `GITHUB_PAGES_BASE`| —                                    | GitHub Pages adresiniz (sonunda `/` ile)       |
| `MAX_PAGES`        | `0` (sınırsız)                       | Bir çalıştırmada en fazla taranacak sayfa sayısı. `0` = limit yok, site ne kadar büyürse büyüsün (141, 500, 5000... hepsi) tamamı taranır. İsterseniz güvenlik amaçlı bir sayı verebilirsiniz. |
| `MAX_FEED_ITEMS`   | `500`                               | rss.xml içine en fazla kaç görsel konulacağı   |
| `REQUEST_DELAY`    | `0.4`                               | İstekler arası bekleme (saniye), siteye nazik davranmak için |
| `MAX_RETRIES`      | `3`                                  | Hata durumunda yeniden deneme sayısı           |

## Önemli notlar

- **Site yapısı değişirse**: Bu scraper `/media/{id}` ve
  `/media/{id}/full` kalıbına göre çalışır (verdiğiniz örnek:
  `https://device-forum.com/media/758/full`). Site farklı bir sayfalama
  veya URL yapısı kullanıyorsa `scraper/scraper.py` içindeki
  `MEDIA_ID_RE` düzenli ifadesini ve `crawl_site()` fonksiyonunu
  güncellemek gerekebilir.
- **Büyük siteler için**: `MAX_PAGES=0` (sınırsız) olduğu için site
  büyüdükçe (141 sayfa, ileride daha da fazlası) her saatlik tam tarama
  daha uzun sürebilir. Workflow'un çalışma süresi GitHub Actions'ın izin
  verdiği üst sınıra (360 dakika) göre ayarlandı. Site çok büyürse ve
  tek çalıştırma bu süreyi de aşarsa, `REQUEST_DELAY` değerini
  düşürerek (dikkatli şekilde) veya taramayı daha sık/az sıklıkta
  çalıştırarak dengeleyebilirsiniz. Not: public (herkese açık)
  repolarda GitHub Actions dakikaları ücretsizdir; private repoda
  aylık dakika kotanız olabilir.
- **robots.txt / hız**: Script varsayılan olarak istekler arasında
  0.4 saniye bekler; siteye aşırı yük bindirmemek için bu süreyi
  düşürmemenizi öneririm.
