import os
import json
import time
import logging
import cloudscraper
import html
import re
import tempfile
import trafilatura
import concurrent.futures
import feedparser
from urllib.parse import quote, unquote, urlparse, urlunparse
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from gnews import GNews
from ddgs import DDGS
from dateutil import parser
import hashlib

# --- CONFIGURATION ---
CONFIG = {
    'SEARCH_QUERY': 'technology (AI OR gadget OR software OR startup OR gaming)',
    'SEARCH_QUERIES': [
        '(artificial intelligence OR "AI model" OR "generative AI") (launch OR update OR release OR breakthrough)',
        '(smartphone OR laptop OR wearable OR gadget) (review OR launch OR unveiled OR release)',
        '(gaming OR "video game" OR PlayStation OR Xbox OR Nintendo OR Steam) (release OR update OR announcement)',
        '(startup OR "tech company") (funding OR acquisition OR valuation OR launch)',
        '(software OR app OR "operating system") (update OR release OR new feature)',
        '(chip OR semiconductor OR Nvidia OR AMD OR Intel OR "graphics card") (news OR launch OR announcement)',
        '(Apple OR Google OR Microsoft OR Samsung OR Meta OR Amazon) (announces OR unveils OR launches)',
    ],
    'GOOGLE_NEWS_TOPIC_FEEDS': [
        # Google News - Sci/Tech topic feed
        'https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGRqTVhZU0FtVnVHZ0pWVXlnQVAB?hl=en-US&gl=US&ceid=US:en',
    ],
    'TARGET_SOURCES': [
        'theverge.com', 'techcrunch.com', 'engadget.com', 'arstechnica.com',
        'wired.com', 'cnet.com', 'gizmodo.com', 'tomshardware.com',
        '9to5mac.com', '9to5google.com', 'androidauthority.com',
        'ign.com', 'polygon.com', 'kotaku.com', 'zdnet.com', 'venturebeat.com'
    ],
    'SOURCE_PRIORITY': {
        'theverge.com': 9, 'techcrunch.com': 9, 'arstechnica.com': 9,
        'wired.com': 8, 'engadget.com': 8, 'cnet.com': 7, 'gizmodo.com': 7,
        'tomshardware.com': 7, '9to5mac.com': 7, '9to5google.com': 7,
        'androidauthority.com': 6, 'ign.com': 7, 'polygon.com': 7,
        'kotaku.com': 6, 'zdnet.com': 6, 'venturebeat.com': 7,
        'reuters.com': 8, 'apnews.com': 7, 'bloomberg.com': 8,
        'bbc.com': 7, 'theguardian.com': 6,
    },
    'FILES': {
        'NEWS': 'news.json',
    },
    'TELEGRAM': {
        'BOT_TOKEN': os.environ.get('TG_BOT_TOKEN'),
        'CHANNEL_ID': os.environ.get('TG_CHANNEL_ID')
    },
    'SITE_URL': 'https://wirtec.github.io/WirTech',
    'TELEGRAM_CHANNEL_URL': 'https://t.me/wirtech',
    # Exact footer requested to be appended to Telegram posts
    'TELEGRAM_FOOTER_HTML': '<br><aside><a href="https://t.me/wirtech">WirTech</a><cite>Technology News</cite></aside>',
    'TIMEOUT': 12,
    'AI_TIMEOUT': 45,
    'MAX_WORKERS': 3,
    'MAX_CANDIDATES': 15,
    'MAX_TEXT_CHARS': 1800,
    'MIN_TEXT_LEN': 100,
    'MIN_AI_URGENCY_HINT': 5,
    'GEMINI_KEY': os.environ.get('GEMINI_API_KEY'),
    'GEMINI_MODEL': 'gemini-3.6-flash',
    'AI_RETRIES': 3,
    'MIN_TELEGRAM_URGENCY': 6,
    'MAX_NEWS_AGE_HOURS': 24,
    'HISTORY_SIZE': 300,
    'RESOLVE_GOOGLE_URLS': True,
    'MAX_IMAGES_PER_ARTICLE': 5,
}

BAD_IMAGE_HOSTS = (
    'lh3.googleusercontent.com',
    'lh4.googleusercontent.com',
    'lh5.googleusercontent.com',
    'lh6.googleusercontent.com',
    'encrypted-tbn0.gstatic.com',
    'encrypted-tbn1.gstatic.com',
    'encrypted-tbn2.gstatic.com',
    'encrypted-tbn3.gstatic.com',
    'news.google.com',
    'www.google.com',
    'google.com',
)

# Fallback imagery themed around technology/gadgets/AI/gaming
FALLBACK_IMAGES = {
    'ai': 'https://images.unsplash.com/photo-1677442136019-21780ecad995?auto=format&fit=crop&w=1200&q=80',
    'gadget': 'https://images.unsplash.com/photo-1518770660439-4636190af475?auto=format&fit=crop&w=1200&q=80',
    'gaming': 'https://images.unsplash.com/photo-1493711662062-fa541adb3fc8?auto=format&fit=crop&w=1200&q=80',
    'chip': 'https://images.unsplash.com/photo-1591238372338-22dae4b5c85e?auto=format&fit=crop&w=1200&q=80',
    'software': 'https://images.unsplash.com/photo-1461749280684-dccba630e2f6?auto=format&fit=crop&w=1200&q=80',
    'default': 'https://images.unsplash.com/photo-1519389950473-47ba0277781c?auto=format&fit=crop&w=1200&q=80',
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()


class WirTechRadar:
    def __init__(self):
        self.scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
        self.scraper.headers.update({
            'Accept-Language': 'en-US,en;q=0.9,fa;q=0.8',
            'Cache-Control': 'no-cache',
        })
        self.existing_news = self._load_existing_news()

        self.seen_urls = set()
        self.seen_titles = set()
        self.recent_title_hashes = set()
        self.failed_hosts = set()

        for item in self.existing_news:
            if item.get('url'):
                self.seen_urls.add(self._clean_url(item['url']))
            for key in ('title_en', 'title_fa'):
                if item.get(key):
                    self.seen_titles.add(self._normalize_text(item[key]))
                    self.recent_title_hashes.add(self._title_hash(item[key]))

        if len(self.recent_title_hashes) > 200:
            self.recent_title_hashes = set(list(self.recent_title_hashes)[-150:])

        self.gnews_en = GNews(language='en', country='US', period='6h', max_results=6)

    # ───────────────────────── helpers ─────────────────────────

    def _get_tehran_time(self):
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo("Asia/Tehran"))
        except ImportError:
            return datetime.now(timezone(timedelta(hours=3, minutes=30)))

    def _clean_url(self, url):
        if not url:
            return ""
        try:
            parsed = urlparse(url)
            clean = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))
            return clean.rstrip('/')
        except Exception:
            return url

    def _normalize_text(self, text):
        if not text:
            return ""
        text = text.replace('ي', 'ی').replace('ك', 'ک').replace('\u200c', ' ')
        clean = re.sub(r'[^\w\s]', '', text.lower())
        return re.sub(r'\s+', '', clean)

    def _title_hash(self, title):
        return hashlib.md5(self._normalize_text(title).encode('utf-8')).hexdigest()

    def _get_tokens(self, text):
        if not text:
            return set()
        stop_words = {
            'a', 'an', 'the', 'and', 'or', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by',
            'is', 'are', 'was', 'were', 'be', 'been', 'news', 'report', 'reports', 'breaking',
            'live', 'updates', 'latest', 'says', 'new', 'how', 'what', 'why', 'best',
            'از', 'به', 'در', 'که', 'و', 'این', 'آن', 'را', 'برای', 'با', 'است', 'شد',
            'شده', 'می', 'بر', 'یک', 'خود', 'تا', 'کرد', 'نیز', 'خبر', 'جدید'
        }
        text = text.replace('ي', 'ی').replace('ك', 'ک').replace('\u200c', ' ')
        clean = re.sub(r'[^\w\s]', '', text.lower())
        tokens = set()
        for word in clean.split():
            if word not in stop_words and len(word) > 2:
                if word.startswith(('un', 're', 'dis')):
                    word = word[2:]
                tokens.add(word)
        return tokens

    def _is_duplicate_fuzzy(self, new_title, comparison_pool):
        norm_title = self._normalize_text(new_title)
        if norm_title in self.seen_titles:
            return True

        new_tokens = self._get_tokens(new_title)
        if len(new_tokens) < 2:
            return False

        pool = comparison_pool[:120] if len(comparison_pool) > 120 else comparison_pool
        for item in pool:
            existing_title = item.get('title_en') or item.get('title_fa') or item.get('title', '')
            existing_tokens = self._get_tokens(existing_title)
            if not existing_tokens:
                continue

            inter = new_tokens.intersection(existing_tokens)
            union = new_tokens.union(existing_tokens)

            # Similar headlines describing the same underlying story
            if union and (len(inter) / len(union)) >= 0.4:
                return True

            if len(inter) >= 3 and len(inter) / min(len(new_tokens), len(existing_tokens)) >= 0.6:
                return True

        return False

    def _load_existing_news(self):
        if not os.path.exists(CONFIG['FILES']['NEWS']):
            return []
        try:
            with open(CONFIG['FILES']['NEWS'], 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []

    def _domain_score(self, url, publisher=""):
        try:
            host = urlparse(url or '').netloc.lower().replace('www.', '')
            for domain, score in CONFIG['SOURCE_PRIORITY'].items():
                if domain in host:
                    return score
        except Exception:
            pass
        pub = (publisher or '').lower()
        for domain, score in CONFIG['SOURCE_PRIORITY'].items():
            if domain.split('.')[0] in pub:
                return score
        return 3

    def _cheap_urgency_hint(self, title, publisher=""):
        t = (title or '').lower()
        score = 3
        high = [
            'launch', 'unveil', 'announce', 'breakthrough', 'release', 'exclusive',
            'first look', 'hands-on', 'official', 'confirmed'
        ]
        mid = ['update', 'rumor', 'leak', 'review', 'preview', 'beta']
        if any(w in t for w in high):
            score += 3
        if any(w in t for w in mid):
            score += 1
        if self._domain_score('', publisher) >= 8:
            score += 1
        return min(score, 9)

    def _generate_news_id(self, clean_url):
        return hashlib.md5((clean_url or str(time.time())).encode('utf-8')).hexdigest()[:10]

    def _is_valid_image_url(self, url):
        if not url or not isinstance(url, str):
            return False
        u = url.strip()
        if not u.startswith(('http://', 'https://')):
            return False
        if u.startswith('data:'):
            return False
        try:
            host = urlparse(u).netloc.lower().replace('www.', '')
            if any(bad in host for bad in BAD_IMAGE_HOSTS):
                return False
            if 'googleusercontent.com' in host and ('=s0' in u or 'w300' in u or '-rw' in u):
                return False
        except Exception:
            return False
        return True

    def _get_fallback_image(self, text_or_tag):
        t = str(text_or_tag).lower()
        if any(w in t for w in ['ai', 'artificial intelligence', 'هوش مصنوعی', 'مدل زبانی', 'chatgpt', 'gemini']):
            return FALLBACK_IMAGES['ai']
        if any(w in t for w in ['game', 'gaming', 'بازی', 'playstation', 'xbox', 'nintendo']):
            return FALLBACK_IMAGES['gaming']
        if any(w in t for w in ['chip', 'processor', 'nvidia', 'amd', 'intel', 'تراشه']):
            return FALLBACK_IMAGES['chip']
        if any(w in t for w in ['app', 'software', 'os', 'نرم‌افزار', 'سیستم‌عامل']):
            return FALLBACK_IMAGES['software']
        if any(w in t for w in ['phone', 'laptop', 'gadget', 'wearable', 'گجت', 'موبایل', 'لپ‌تاپ']):
            return FALLBACK_IMAGES['gadget']
        return FALLBACK_IMAGES['default']

    def _pick_image(self, *candidates, fallback_text=''):
        for c in candidates:
            if self._is_valid_image_url(c):
                return c
        return self._get_fallback_image(fallback_text)

    def _pick_images(self, candidates, fallback_text='', max_images=None):
        """Return a deduplicated list of valid image URLs (main image first)."""
        max_images = max_images or CONFIG.get('MAX_IMAGES_PER_ARTICLE', 5)
        out = []
        for c in candidates:
            if self._is_valid_image_url(c) and c not in out:
                out.append(c)
            if len(out) >= max_images:
                break
        if not out:
            out = [self._get_fallback_image(fallback_text)]
        return out

    # ───────────────────────── news search ─────────────────────────

    def fetch_gnews(self):
        results = []
        try:
            results = self.gnews_en.get_news(CONFIG['SEARCH_QUERY']) or []
        except Exception as e:
            logger.error(f"GNews Error: {e}")
        return results

    def fetch_google_news_topic(self, feed_url):
        """Pull items directly from a Google News topic RSS feed (e.g. Sci/Tech)."""
        results = []
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                title = entry.get('title', '')
                source = "Google News"
                if hasattr(entry, 'source') and getattr(entry.source, 'title', None):
                    source = entry.source.title
                image = None
                if hasattr(entry, 'media_content') and entry.media_content:
                    image = entry.media_content[0].get('url')
                results.append({
                    'title': title,
                    'url': entry.get('link'),
                    'publisher': {'title': source},
                    'published date': entry.get('published'),
                    'description': entry.get('summary', title),
                    'image': image
                })
        except Exception as e:
            logger.error(f"Google News Topic Feed Error: {e}")
        return results

    def fetch_duckduckgo(self, query, region='wt-wt', max_results=8):
        results = []
        try:
            ddgs = DDGS()
            ddg_gen = ddgs.news(
                query=query, region=region, safesearch="off",
                timelimit="d", max_results=max_results
            )
            for r in ddg_gen:
                results.append({
                    'title': r.get('title'),
                    'url': r.get('url'),
                    'publisher': {'title': r.get('source')},
                    'published date': r.get('date'),
                    'description': r.get('body'),
                    'image': r.get('image')
                })
        except Exception as e:
            logger.warning(f"DDG blocked/failed ({query[:30]}), falling back to Bing RSS: {e}")
            return self.fetch_bing_rss(query)

        return results

    def fetch_bing_rss(self, query):
        results = []
        try:
            encoded_query = quote(query)
            url = f"https://www.bing.com/news/search?q={encoded_query}&format=rss"
            feed = feedparser.parse(url)
            for entry in feed.entries:
                publisher = "Bing News"
                if hasattr(entry, 'news_source'):
                    publisher = entry.news_source
                elif hasattr(entry, 'source') and hasattr(entry.source, 'title'):
                    publisher = entry.source.title

                final_link = entry.link
                if "apiclick.aspx" in final_link:
                    match = re.search(r'[?&]url=([^&]+)', final_link)
                    if match:
                        final_link = unquote(match.group(1))

                image_url = None
                try:
                    if hasattr(entry, 'news_image'):
                        raw_url = entry.news_image
                        image_url = (
                            raw_url.replace('{0}', '700').replace('{1}', '400')
                            if '{0}' in raw_url else raw_url
                        )
                except Exception:
                    pass

                results.append({
                    'title': entry.title,
                    'url': final_link,
                    'publisher': {'title': publisher},
                    'published date': entry.published,
                    'description': entry.summary if hasattr(entry, 'summary') else entry.title,
                    'image': image_url
                })
        except Exception as e:
            logger.error(f"Bing RSS Error: {e}")
        return results

    def fetch_manual_url(self, url):
        try:
            resp = self.scraper.get(url, timeout=15)
            soup = BeautifulSoup(resp.text, 'lxml')
            title = soup.title.string if soup.title else "Unknown Title"
            og_title = soup.find("meta", property="og:title")
            if og_title:
                title = og_title.get("content")
            publisher = "Manual Source"
            og_site = soup.find("meta", property="og:site_name")
            if og_site:
                publisher = og_site.get("content")
            image = None
            og_image = soup.find("meta", property="og:image")
            if og_image:
                image = og_image.get("content")
            return [{
                'title': title,
                'url': url,
                'publisher': {'title': publisher},
                'published date': datetime.now(timezone.utc).isoformat(),
                'description': "Manual Submission",
                'image': image
            }]
        except Exception as e:
            logger.error(f"Manual Fetch Error: {e}")
            return []

    def get_combined_news(self):
        all_entries = []
        futs = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futs.append(ex.submit(self.fetch_gnews))
            futs.append(ex.submit(self.fetch_bing_rss, CONFIG['SEARCH_QUERY']))

            for feed_url in CONFIG.get('GOOGLE_NEWS_TOPIC_FEEDS', []):
                futs.append(ex.submit(self.fetch_google_news_topic, feed_url))

            for q in CONFIG.get('SEARCH_QUERIES', []):
                futs.append(ex.submit(self.fetch_duckduckgo, q, 'wt-wt', 6))

            for fut in concurrent.futures.as_completed(futs):
                try:
                    batch = fut.result() or []
                    all_entries.extend(batch)
                except Exception as e:
                    logger.warning(f"Search worker failed: {e}")

        logger.info(f"Raw search hits: {len(all_entries)}")
        return all_entries

    # ───────────────────────── URL resolve ─────────────────────────

    def _resolve_final_url(self, url, raw_title=None):
        if not url:
            return None
        if "news.google.com" not in url:
            return url

        try:
            match = re.search(r'articles/([^?&]+)', url)
            if match:
                encoded = match.group(1)
                padded = encoded + '=' * (-len(encoded) % 4)
                import base64
                decoded_bytes = base64.urlsafe_b64decode(padded.encode('ascii'))
                urls_found = re.findall(rb'https?://[a-zA-Z0-9.\-_~:/?#[\]@!$&\'()*+,;=%]+', decoded_bytes)
                for u in urls_found:
                    u_str = u.decode('utf-8', errors='ignore')
                    if "google.com" not in u_str:
                        return u_str
        except Exception:
            pass

        try:
            resp = self.scraper.get(url, allow_redirects=True, timeout=8)
            if resp.status_code == 200 and "news.google.com" not in resp.url:
                return resp.url
        except Exception as e:
            logger.warning(f"Failed to resolve Google URL {url}: {e}")

        return url

    # ───────────────────────── content grab ─────────────────────────

    def scrape_article_data(self, final_url, fallback_snippet, raw_image=None):
        """Returns (extracted_text, image_list) for an article."""
        if not final_url or final_url.lower().endswith('.pdf'):
            return fallback_snippet, self._pick_images([raw_image], fallback_text=fallback_snippet)

        host = urlparse(final_url).netloc.lower()
        if host in self.failed_hosts:
            return fallback_snippet, self._pick_images([raw_image], fallback_text=fallback_snippet)

        extracted_text = fallback_snippet
        found_images = []
        if self._is_valid_image_url(raw_image):
            found_images.append(raw_image)
        max_chars = CONFIG.get('MAX_TEXT_CHARS', 1800)

        try:
            downloaded = trafilatura.fetch_url(final_url)
            if downloaded:
                text = trafilatura.extract(
                    downloaded,
                    include_comments=False,
                    include_tables=False,
                    favor_precision=True,
                )
                if text and len(text.strip()) > CONFIG.get('MIN_TEXT_LEN', 100):
                    extracted_text = re.sub(r'\s+', ' ', text).strip()[:max_chars]
                try:
                    meta = trafilatura.extract_metadata(downloaded)
                    if meta and getattr(meta, 'image', None) and self._is_valid_image_url(meta.image):
                        if meta.image not in found_images:
                            found_images.insert(0, meta.image)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"trafilatura failed {final_url}: {e}")
            self.failed_hosts.add(host)

        need_soup = (
            len(found_images) < CONFIG.get('MAX_IMAGES_PER_ARTICLE', 5)
            or extracted_text == fallback_snippet
            or len(extracted_text) < CONFIG.get('MIN_TEXT_LEN', 100)
        )
        if need_soup:
            try:
                resp = self.scraper.get(final_url, timeout=CONFIG['TIMEOUT'])
                soup = BeautifulSoup(resp.text, 'lxml')

                if extracted_text == fallback_snippet or len(extracted_text) < CONFIG.get('MIN_TEXT_LEN', 100):
                    for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe']):
                        tag.decompose()
                    paras = [
                        p.get_text(strip=True)
                        for p in soup.find_all('p')
                        if len(p.get_text(strip=True)) > 40
                    ]
                    clean = ' '.join(paras[:12])
                    if len(clean) > CONFIG.get('MIN_TEXT_LEN', 100):
                        extracted_text = clean[:max_chars]

                # Meta images (og/twitter) first
                for prop in (
                    ('property', 'og:image'),
                    ('property', 'og:image:secure_url'),
                    ('name', 'twitter:image'),
                    ('name', 'twitter:image:src'),
                    ('itemprop', 'image'),
                ):
                    tag = soup.find('meta', attrs={prop[0]: prop[1]})
                    if tag and tag.get('content') and self._is_valid_image_url(tag['content']):
                        val = tag['content'].strip()
                        if val not in found_images:
                            found_images.append(val)

                # Additional in-article images (for the gallery)
                if len(found_images) < CONFIG.get('MAX_IMAGES_PER_ARTICLE', 5):
                    for img in soup.find_all('img', src=True):
                        src = img.get('src') or ''
                        if src.startswith('//'):
                            src = 'https:' + src
                        if not src.startswith('http'):
                            continue
                        if not self._is_valid_image_url(src):
                            continue
                        w = img.get('width') or img.get('data-width') or ''
                        h = img.get('height') or img.get('data-height') or ''
                        try:
                            if w and int(str(w).replace('px', '')) < 150:
                                continue
                            if h and int(str(h).replace('px', '')) < 100:
                                continue
                        except Exception:
                            pass
                        if src not in found_images:
                            found_images.append(src)
                        if len(found_images) >= CONFIG.get('MAX_IMAGES_PER_ARTICLE', 5):
                            break
            except Exception as e:
                logger.warning(f"Soup fallback failed {final_url}: {e}")
                self.failed_hosts.add(host)

        images = self._pick_images(
            found_images + [raw_image],
            fallback_text=extracted_text or fallback_snippet
        )
        return extracted_text, images

    # ───────────────────────── AI analysis ─────────────────────────

    def _call_gemini(self, system_prompt, user_prompt, temperature=0.2):
        if not CONFIG.get('GEMINI_KEY'):
            logger.error("GEMINI_API_KEY is not set.")
            return None

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{CONFIG['GEMINI_MODEL']}:generateContent?key={CONFIG['GEMINI_KEY']}"
        payload = {
            "system_instruction": {
                "parts": [{"text": system_prompt}]
            },
            "contents": [{
                "parts": [{"text": user_prompt}]
            }],
            "generationConfig": {
                "response_mime_type": "application/json",
                "temperature": temperature
            }
        }

        for attempt in range(CONFIG['AI_RETRIES']):
            try:
                resp = self.scraper.post(url, json=payload, timeout=CONFIG.get('AI_TIMEOUT', 45))
                if resp.status_code == 200:
                    result = resp.json()
                    raw_text = result['candidates'][0]['content']['parts'][0]['text']
                    clean = re.sub(r'```json\s*|```', '', raw_text).strip()
                    return json.loads(clean)
                else:
                    logger.error(f"Gemini API error {resp.status_code}: {resp.text[:200]}")
                time.sleep(1)
            except Exception as e:
                logger.error(f"Gemini Attempt {attempt + 1} failed: {e}")
                time.sleep(2)
        return None

    def batch_analyze_with_gemini(self, candidates_data):
        """
        Analyzes multiple candidate tech news articles in a SINGLE Gemini API request.
        candidates_data format: list of dicts with {'index', 'source', 'headline', 'text'}
        """
        if not candidates_data or not CONFIG.get('GEMINI_KEY'):
            return {}

        system_prompt = (
            "تو سردبیر ارشد یک خبرنامه تکنولوژی به نام «WirTech» هستی که اخبار روز دنیای فناوری، "
            "گجت، هوش مصنوعی، نرم‌افزار، سخت‌افزار و بازی‌های ویدیویی را برای مخاطب فارسی‌زبان روایت می‌کند.\n\n"
            "🎯 وظیفه تو تبدیل هر خبر خام انگلیسی به یک تیتر جذاب فارسی و و خبر خلاصه ساده و روان است. "
            "کامل و درست ؛ فقط باید بگویی «چه اتفاقی افتاده».\n\n"
            "🔴 قانون حیاتی حذف اخبار تکراری و هم‌پوشان:\n"
            "- اگر چند خبر به یک رویداد واحد پرداخته‌اند، فقط یک مورد (کامل‌ترین منبع) را در خروجی بیاور.\n\n"
            "🔴 قوانین نگارش:\n"
            "۱. زبان ساده، روان و امروزی؛ از ترجمه تحت‌اللفظی اصطلاحات فنی خودداری کن و معادل رایج فارسی/فنگلیسی رایج در جامعه تکنولوژی ایران را به کار ببر.\n"
            "۲. از عبارات کلیشه‌ای و رباتیک خودداری کن ('به نظر می‌رسد'، 'شایان ذکر است' و مشابه آن).\n"
            "۳. بخش summary باید ۲ تا ۳ نکته کوتاه نباشه، خبری و مشخص باشد (نه تحلیلی)؛ فقط واقعیت خبر را بگو.\n"
            "۴. تیتر (title_fa) باید کوتاه (حداکثر ۱۵ کلمه)، جذاب و غیرتکراری باشد.\n\n"
            "تو فهرستی از آیتم‌های خبری با شناسه index دریافت می‌کنی. خروجی باید یک لیست JSON معتبر شامل تحلیل تک تک این آیتم‌ها با ساختار زیر باشد:\n"
            "[\n"
            "  {\n"
            '    "index": 0,\n'
            '    "title_fa": "تیتر جذاب و کوتاه فارسی",\n'
            '    "summary": ["نکته خبری ۱", "نکته خبری ۲", "نکته خبری ۳ (اختیاری)"],\n'
            '    "tag": "یکی از این مقادیر: هوش مصنوعی, گجت, بازی, نرم‌افزار, سخت‌افزار, استارتاپ, عمومی",\n'
            '    "urgency": عدد بین 1 تا 10 (میزان اهمیت خبر برای قرارگیری در خبرنامه بیشتر رو به بالا)\n'
            "  }\n"
            "]"
        )

        items_input = []
        for item in candidates_data:
            items_input.append(
                f"--- ITEM INDEX: {item['index']} ---\n"
                f"SOURCE: {item['source']}\n"
                f"HEADLINE: {item['headline']}\n"
                f"TEXT: {item['text'][:1000]}\n"
            )

        user_prompt = "لطفاً تمامی آیتم‌های زیر را تحلیل و در قالب JSON مشخص‌شده برگردان:\n\n" + "\n".join(items_input)

        data = self._call_gemini(system_prompt, user_prompt, temperature=0.3)
        if isinstance(data, list):
            return {item.get('index'): item for item in data if 'index' in item}
        return {}

    def analyze_with_ai(self, headline, text, source):
        result = self.batch_analyze_with_gemini([{'index': 0, 'source': source, 'headline': headline, 'text': text}])
        return result.get(0)

    # ───────────────────────── process item ─────────────────────────

    def process_item(self, entry):
        raw_title = entry.get('title', '').rsplit(' - ', 1)[0].strip()
        publisher = entry.get('publisher', {}).get('title', 'Unknown')

        final_url = self._resolve_final_url(entry.get('url'), raw_title)
        if not final_url:
            return None

        clean_final_url = self._clean_url(final_url)

        if not os.environ.get('MANUAL_URL'):
            if clean_final_url in self.seen_urls:
                return None
            th = self._title_hash(raw_title)
            if th in self.recent_title_hashes or self._normalize_text(raw_title) in self.seen_titles:
                return None
            if self._is_duplicate_fuzzy(raw_title, self.existing_news):
                return None

        hint = self._cheap_urgency_hint(raw_title, publisher)
        logger.info(
            f"Processing (hint={hint}, score={self._domain_score(final_url, publisher)}): "
            f"{publisher} | {raw_title[:40]}..."
        )

        snippet = entry.get('description', raw_title)
        text, images = self.scrape_article_data(
            final_url, snippet, raw_image=entry.get('image')
        )

        if hint < 3 and len(text) < 80:
            logger.info(f"Skip AI (very low hint/thin text): {raw_title[:40]}")
            return None

        ai = self.analyze_with_ai(raw_title, text, publisher)
        if not ai:
            return None

        try:
            urgency_val = int(ai.get('urgency', 3))
        except Exception:
            urgency_val = 3
        try:
            ts = parser.parse(entry.get('published date')).timestamp()
        except Exception:
            ts = time.time()

        news_id = self._generate_news_id(clean_final_url)

        return {
            "id": news_id,
            "title_fa": ai.get('title_fa', raw_title),
            "title_en": raw_title,
            "summary": ai.get('summary', [snippet]),
            "tag": ai.get('tag', 'عمومی'),
            "urgency": urgency_val,
            "source": publisher,
            "url": final_url,
            "clean_url": clean_final_url,
            "image": images[0] if images else self._get_fallback_image(raw_title),
            "images": images,
            "timestamp": ts
        }

    # ───────────────────────── telegram sender ─────────────────────────

    def send_digest_to_telegram(self, items):
        """Send a compact newsletter-style digest (title + short summary + link) to Telegram."""
        token = CONFIG['TELEGRAM']['BOT_TOKEN']
        chat_id = CONFIG['TELEGRAM']['CHANNEL_ID']
        if not token or not chat_id or not items:
            return

        items.sort(key=lambda x: x.get('urgency', 3), reverse=True)

        def esc(s):
            return html.escape(str(s or ''), quote=False)

        now_ir = self._get_tehran_time()
        time_str = now_ir.strftime("%H:%M")
        date_str = now_ir.strftime("%Y/%m/%d")
        site = CONFIG['SITE_URL']
        footer = CONFIG['TELEGRAM_FOOTER_HTML']

        lines = [
            "🚀 <b>خبرنامه فناوری WirTech</b>",
            f"⏱ {time_str} — {date_str}",
            "",
        ]

        for item in items[:8]:
            title = esc(item.get('title_fa') or item.get('title_en'))
            source = esc(item.get('source', ''))
            summary_raw = item.get('summary', [])
            if isinstance(summary_raw, str):
                summary_raw = [summary_raw]
            summary_text = esc(' '.join(summary_raw[:2]))
            url = item.get('url') or site

            lines.append(f"🔹 <b>{title}</b>")
            if summary_text:
                lines.append(summary_text)
            lines.append(f"<a href=\"{esc(url)}\">مشاهده کامل خبر</a> | <i>{source}</i>")
            lines.append("")

        lines.append(f"📊 <a href=\"{esc(site)}\">مشاهده همه اخبار در وب‌سایت WirTech</a>")
        lines.append(footer)

        full_text = "\n".join(lines)
        if len(full_text) > 4000:
            full_text = full_text[:3900] + f"\n\n📊 <a href=\"{esc(site)}\">ادامه در وب‌سایت WirTech</a>" + footer

        # Prefer sending with the top item's photo when available
        photo_url = None
        for item in items[:8]:
            img = item.get('image')
            if self._is_valid_image_url(img):
                photo_url = img
                break

        if photo_url and len(full_text) <= 1024:
            photo_api = f"https://api.telegram.org/bot{token}/sendPhoto"
            try:
                resp = self.scraper.post(photo_api, json={
                    "chat_id": chat_id,
                    "photo": photo_url,
                    "caption": full_text,
                    "parse_mode": "HTML",
                }, timeout=30)
                if resp.status_code == 200:
                    logger.info(">>> Digest sent to Telegram as photo message.")
                    return
                logger.warning(f"sendPhoto failed ({resp.status_code}), falling back to text message.")
            except Exception as e:
                logger.warning(f"sendPhoto exception: {e}, falling back to text message.")

        # Standard text message (also used when the digest is longer than a caption allows)
        api_url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            resp = self.scraper.post(api_url, json={
                "chat_id": chat_id,
                "text": full_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=30)
            if resp.status_code == 200:
                logger.info(">>> Digest sent to Telegram as text message.")
            else:
                logger.error(f"sendMessage failed: {resp.status_code} | {resp.text[:300]}")
        except Exception as e:
            logger.error(f"TG send error: {e}")

    # ───────────────────────── save ─────────────────────────

    def _atomic_json_dump(self, file_path, data):
        dir_name = os.path.dirname(file_path) or '.'
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile('w', dir=dir_name, delete=False, encoding='utf-8') as tf:
                json.dump(data, tf, indent=4, ensure_ascii=False)
                temp_name = tf.name
            os.replace(temp_name, file_path)
        except Exception as e:
            logger.error(f"Atomic dump failed for {file_path}: {e}")
            if temp_name and os.path.exists(temp_name):
                os.remove(temp_name)

    def save_news(self, new_items):
        try:
            all_news = new_items + self.existing_news
            seen_u = set()
            unique_news = []
            for item in all_news:
                u = self._clean_url(item.get('url'))
                if u and u not in seen_u:
                    seen_u.add(u)
                    if not item.get('images'):
                        item['images'] = self._pick_images(
                            [item.get('image')],
                            fallback_text=item.get('title_en') or item.get('title_fa') or ''
                        )
                    item['image'] = item['images'][0]
                    unique_news.append(item)
            unique_news.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
            final_list = unique_news[:CONFIG['HISTORY_SIZE']]
            self._atomic_json_dump(CONFIG['FILES']['NEWS'], final_list)
            logger.info(">>> news.json updated successfully.")
            return final_list
        except Exception as e:
            logger.error(f"Save Failed: {e}")
            return self.existing_news

    # ───────────────────────── main run ─────────────────────────

    def run(self):
        logger.info(">>> WirTech Radar Started (search + extract + photos)...")

        manual_url = os.environ.get('MANUAL_URL')

        if manual_url and manual_url.strip():
            logger.info(f"!!! MANUAL MODE: {manual_url} !!!")
            results = self.fetch_manual_url(manual_url)
            candidates = results
        else:
            results = self.get_combined_news()
            candidates = []
            seen_batch_titles = set()
            cutoff_date = datetime.now(timezone.utc) - timedelta(hours=CONFIG['MAX_NEWS_AGE_HOURS'])

            for item in results:
                try:
                    p_date = item.get('published date')
                    if p_date:
                        dt = parser.parse(p_date)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        if dt < cutoff_date:
                            continue
                except Exception:
                    pass

                raw_url = item.get('url', '')
                clean_u = self._clean_url(raw_url)
                if clean_u in self.seen_urls:
                    continue

                t = item.get('title', '').rsplit(' - ', 1)[0].strip()
                norm_t = self._normalize_text(t)
                th = self._title_hash(t)

                if norm_t in self.seen_titles or norm_t in seen_batch_titles:
                    continue
                if th in self.recent_title_hashes:
                    continue

                seen_batch_titles.add(norm_t)
                candidates.append(item)

            candidates.sort(
                key=lambda x: self._domain_score(
                    x.get('url'),
                    x.get('publisher', {}).get('title', '')
                ),
                reverse=True
            )

            accepted_candidates = []
            for item in candidates:
                raw_t = item.get('title', '').rsplit(' - ', 1)[0].strip()
                if self._is_duplicate_fuzzy(raw_t, self.existing_news) or self._is_duplicate_fuzzy(raw_t, accepted_candidates):
                    continue
                accepted_candidates.append(item)

            candidates = accepted_candidates[:CONFIG.get('MAX_CANDIDATES', 15)]

        logger.info(
            f"Total Fetched: {len(results)} | Candidates (new/recent/capped): {len(candidates)}"
        )

        new_processed_items = []
        if candidates:
            scraped_items = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=CONFIG['MAX_WORKERS']) as exc:
                future_to_cand = {}
                for idx, cand in enumerate(candidates):
                    raw_title = cand.get('title', '').rsplit(' - ', 1)[0].strip()
                    publisher = cand.get('publisher', {}).get('title', 'Unknown')
                    final_url = self._resolve_final_url(cand.get('url'), raw_title)
                    if not final_url:
                        continue
                    clean_u = self._clean_url(final_url)
                    snippet = cand.get('description', raw_title)
                    f = exc.submit(self.scrape_article_data, final_url, snippet, cand.get('image'))
                    future_to_cand[f] = (idx, cand, raw_title, publisher, final_url, clean_u, snippet)

                for fut in concurrent.futures.as_completed(future_to_cand):
                    idx, cand, raw_title, publisher, final_url, clean_u, snippet = future_to_cand[fut]
                    try:
                        text, images = fut.result()
                        scraped_items.append({
                            'index': idx,
                            'cand': cand,
                            'headline': raw_title,
                            'source': publisher,
                            'url': final_url,
                            'clean_url': clean_u,
                            'snippet': snippet,
                            'text': text,
                            'images': images
                        })
                    except Exception as e:
                        logger.error(f"Scrape worker error: {e}")

            if scraped_items:
                ai_batch_results = self.batch_analyze_with_gemini(scraped_items)

                for item in scraped_items:
                    ai = ai_batch_results.get(item['index'])
                    if not ai:
                        continue
                    try:
                        urgency_val = int(ai.get('urgency', 3))
                    except Exception:
                        urgency_val = 3
                    try:
                        ts = parser.parse(item['cand'].get('published date')).timestamp()
                    except Exception:
                        ts = time.time()

                    images = self._pick_images(
                        item['images'] + [item['cand'].get('image')],
                        fallback_text=item['headline']
                    )
                    news_id = self._generate_news_id(item['clean_url'])

                    res = {
                        "id": news_id,
                        "title_fa": ai.get('title_fa', item['headline']),
                        "title_en": item['headline'],
                        "summary": ai.get('summary', [item['snippet']]),
                        "tag": ai.get('tag', 'عمومی'),
                        "urgency": urgency_val,
                        "source": item['source'],
                        "url": item['url'],
                        "clean_url": item['clean_url'],
                        "image": images[0],
                        "images": images,
                        "timestamp": ts
                    }
                    new_processed_items.append(res)
                    self.seen_urls.add(res['clean_url'])
                    self.recent_title_hashes.add(self._title_hash(res.get('title_en', '')))

        if new_processed_items:
            self.existing_news = self.save_news(new_processed_items)

            telegram_items = [
                item for item in new_processed_items
                if item.get('urgency', 0) >= CONFIG['MIN_TELEGRAM_URGENCY']
            ]

            if telegram_items:
                logger.info(f"Sending {len(telegram_items)} items in the WirTech digest.")
                self.send_digest_to_telegram(telegram_items)
            else:
                logger.info("New items saved, but urgency too low for the digest.")
        else:
            logger.info(">>> No valid new items found.")

        logger.info(
            f">>> Done. New={len(new_processed_items)} | "
            f"Failed hosts this run={len(self.failed_hosts)}"
        )


if __name__ == "__main__":
    WirTechRadar().run()
