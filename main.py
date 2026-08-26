import os
import json
import time
import logging
import cloudscraper
import html
import re
import random
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
    'SEARCH_QUERY': '(AI OR "artificial intelligence" OR technology) (launch OR release OR breakthrough)',
    'SEARCH_QUERIES': [
        '(OpenAI OR "Google DeepMind" OR Anthropic OR Meta OR Microsoft) (AI OR model) (launch OR release OR update)',
        '"artificial intelligence" (research OR breakthrough OR study OR paper)',
        'AI (startup OR funding OR "raises" OR investment OR acquisition)',
        'tech startup (funding round OR "Series A" OR "Series B" OR valuation OR acquisition)',
        '(smartphone OR laptop OR chip OR processor OR gadget) (launch OR unveiled OR announced)',
        '(Apple OR Google OR Samsung OR Nvidia OR Microsoft) (announces OR unveils OR launches)',
        'هوش مصنوعی (مدل OR استارتاپ OR محصول جدید OR راه‌اندازی)'
    ],
    'TARGET_SOURCES': [
        'techcrunch.com', 'theverge.com', 'arstechnica.com', 'wired.com',
        'engadget.com', 'technologyreview.com', 'venturebeat.com',
        'digiato.com'
    ],
    'PRIORITY_SITES': [
        'techcrunch.com', 'theverge.com', 'arstechnica.com', 'wired.com', 'digiato.com'
    ],
    'SOURCE_PRIORITY': {
        'techcrunch.com': 10, 'theverge.com': 10, 'arstechnica.com': 9,
        'wired.com': 9, 'engadget.com': 8, 'technologyreview.com': 8,
        'venturebeat.com': 7, 'reuters.com': 8, 'bloomberg.com': 8,
        'theinformation.com': 8, '9to5mac.com': 6, '9to5google.com': 6,
        'digiato.com': 7,
    },
    'FILES': {
        'NEWS': 'news.json',
        'DAILY_SUMMARY': 'daily_summary.json',
        'SCHEDULE_STATE': 'schedule_state.json'
    },
    'TELEGRAM': {
        'BOT_TOKEN': os.environ.get('TG_BOT_TOKEN'),
        'CHANNEL_ID': os.environ.get('TG_CHANNEL_ID')
    },
    'TIMEOUT': 12,
    'AI_TIMEOUT': 45,
    'MAX_WORKERS': 3,
    'MAX_CANDIDATES': 15,
    'MAX_TEXT_CHARS': 6000,
    'MIN_TEXT_LEN': 100,
    'MAX_IMAGES_PER_ITEM': 4,
    'MIN_AI_URGENCY_HINT': 5,
    'GEMINI_KEY': os.environ.get('GEMINI_API_KEY'),
    'GEMINI_MODEL': 'gemini-3.7-flash',
    'AI_RETRIES': 3,
    'MIN_TELEGRAM_URGENCY': 7,
    'MAX_NEWS_AGE_HOURS': 18,
    'HISTORY_SIZE': 300,
    'RESOLVE_GOOGLE_URLS': True,
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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()


class TechNewsRadar:
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

        self.gnews_en = GNews(language='en', country='US', period='4h', max_results=5)

    # ───────────────────────── helpers ─────────────────────────

    def _get_tehran_time(self):
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo("Asia/Tehran"))
        except ImportError:
            return datetime.now(timezone(timedelta(hours=3, minutes=30)))

    def _is_schedule_already_sent(self, slot_key):
        path = CONFIG['FILES']['SCHEDULE_STATE']
        if not os.path.exists(path):
            return False
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get(slot_key, False)
        except Exception:
            return False

    def _mark_schedule_as_sent(self, slot_key):
        path = CONFIG['FILES']['SCHEDULE_STATE']
        data = {}
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data[slot_key] = True
        self._atomic_json_dump(path, data)

    def _load_previous_daily_summary(self):
        path = CONFIG['FILES']['DAILY_SUMMARY']
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None

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
            'live', 'updates', 'latest', 'warns', 'warning', 'says', 'vows', 'issues', 'pushes',
            'threat', 'threats', 'vs', 'new', 'announces', 'announced', 'launch', 'launches',
            'از', 'به', 'در', 'که', 'و', 'این', 'آن', 'را', 'برای', 'با', 'است', 'شد',
            'شده', 'می', 'بر', 'یک', 'خود', 'تا', 'کرد', 'نیز', 'خبر', 'فوری'
        }
        text = text.replace('ي', 'ی').replace('ك', 'ک').replace('\u200c', ' ')
        clean = re.sub(r'[^\w\s]', '', text.lower())
        tokens = set()
        for word in clean.split():
            if word not in stop_words and len(word) > 2:
                # Normalize common prefixes/suffixes
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

        # Key entity sets for cross-story syndication detection
        key_entity_groups = [
            {'openai', 'chatgpt', 'gpt', 'model', 'release'},
            {'google', 'deepmind', 'gemini', 'ai', 'model'},
            {'anthropic', 'claude', 'model', 'release'},
            {'apple', 'iphone', 'launch', 'event'},
            {'nvidia', 'chip', 'gpu', 'ai'},
            {'startup', 'funding', 'round', 'valuation'}
        ]

        pool = comparison_pool[:120] if len(comparison_pool) > 120 else comparison_pool
        for item in pool:
            existing_title = item.get('title_en') or item.get('title_fa') or item.get('title', '')
            existing_tokens = self._get_tokens(existing_title)
            if not existing_tokens:
                continue

            inter = new_tokens.intersection(existing_tokens)
            union = new_tokens.union(existing_tokens)
            
            # Lower Jaccard threshold from 0.5 to 0.32 to catch rephrased syndicated headlines
            if union and (len(inter) / len(union)) >= 0.32:
                return True

            # If 2 or more distinct key topical tokens match, treat as duplicate story event
            if len(inter) >= 2 and len(inter) / min(len(new_tokens), len(existing_tokens)) >= 0.5:
                return True

            # Match against known entity-event cluster groups
            for group in key_entity_groups:
                if len(new_tokens.intersection(group)) >= 2 and len(existing_tokens.intersection(group)) >= 2:
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
            'launch', 'launches', 'unveils', 'unveiled', 'breakthrough', 'acquires',
            'acquisition', 'announces new model', 'raises', 'funding', 'ipo',
            'رونمایی', 'راه‌اندازی', 'عرضه', 'سرمایه‌گذاری'
        ]
        mid = [
            'update', 'release', 'partnership', 'beta', 'research', 'study',
            'به‌روزرسانی', 'همکاری', 'تحقیق'
        ]
        if any(w in t for w in high):
            score += 3
        if any(w in t for w in mid):
            score += 2
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
        if any(w in t for w in ['ai', 'artificial intelligence', 'model', 'chatgpt', 'gemini', 'claude', 'هوش مصنوعی', 'مدل']):
            return 'https://images.unsplash.com/photo-1677442136019-21780ecad995?auto=format&fit=crop&w=1200&q=80'
        if any(w in t for w in ['startup', 'funding', 'investment', 'venture', 'استارتاپ', 'سرمایه‌گذاری']):
            return 'https://images.unsplash.com/photo-1553877522-43269d4ea984?auto=format&fit=crop&w=1200&q=80'
        if any(w in t for w in ['chip', 'gpu', 'processor', 'hardware', 'پردازنده', 'تراشه']):
            return 'https://images.unsplash.com/photo-1591405351990-4726e331f141?auto=format&fit=crop&w=1200&q=80'
        if any(w in t for w in ['phone', 'smartphone', 'laptop', 'gadget', 'device', 'گوشی', 'لپ‌تاپ']):
            return 'https://images.unsplash.com/photo-1518770660439-4636190af475?auto=format&fit=crop&w=1200&q=80'
        return 'https://images.unsplash.com/photo-1519389950473-47ba0277781c?auto=format&fit=crop&w=1200&q=80'

    def _pick_image(self, *candidates, fallback_text=''):
        for c in candidates:
            if self._is_valid_image_url(c):
                return c
        return self._get_fallback_image(fallback_text)

    def _dedupe_images(self, urls, limit=None):
        limit = limit or CONFIG.get('MAX_IMAGES_PER_ITEM', 4)
        result = []
        for u in urls:
            if u and self._is_valid_image_url(u) and u not in result:
                result.append(u)
            if len(result) >= limit:
                break
        return result

    def _extract_gallery_images(self, soup, limit=None):
        """Collect ALL usable images from an article page (not just the first one)."""
        limit = limit or CONFIG.get('MAX_IMAGES_PER_ITEM', 4)
        found = []

        for prop in (
            ('property', 'og:image'),
            ('property', 'og:image:secure_url'),
            ('name', 'twitter:image'),
            ('name', 'twitter:image:src'),
            ('itemprop', 'image'),
        ):
            for tag in soup.find_all('meta', attrs={prop[0]: prop[1]}):
                content = tag.get('content')
                if content:
                    found.append(content.strip())

        for img in soup.find_all('img', src=True):
            if len(found) >= limit * 3:
                break
            src = img.get('src') or ''
            if src.startswith('//'):
                src = 'https:' + src
            if not src.startswith('http'):
                continue
            w = img.get('width') or img.get('data-width') or ''
            h = img.get('height') or img.get('data-height') or ''
            try:
                if w and int(str(w).replace('px', '')) < 120:
                    continue
                if h and int(str(h).replace('px', '')) < 80:
                    continue
            except Exception:
                pass
            found.append(src)

        return self._dedupe_images(found, limit=limit)

    # ───────────────────────── news search ─────────────────────────

    def fetch_gnews(self):
        results = []
        try:
            results = self.gnews_en.get_news(CONFIG['SEARCH_QUERY']) or []
        except Exception as e:
            logger.error(f"GNews Error: {e}")
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
            # Fallback to Bing RSS when DuckDuckGo fails (403 on GitHub Actions)
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
            # 1. Main News Queries
            futs.append(ex.submit(self.fetch_gnews))
            futs.append(ex.submit(self.fetch_bing_rss, CONFIG['SEARCH_QUERY']))
            
            # 2. Iterate through all specialized queries including key figures
            for q in CONFIG.get('SEARCH_QUERIES', []):
                futs.append(ex.submit(self.fetch_duckduckgo, q, 'wt-wt', 6))
            
            # 3. Dedicated Site Searches (English + Persian tech sources)
            site_queries = [
                'site:techcrunch.com AI OR startup OR launch',
                'site:theverge.com AI OR gadget OR launch',
                'site:digiato.com هوش مصنوعی OR استارتاپ OR فناوری'
            ]
            for f_q in site_queries:
                futs.append(ex.submit(self.fetch_duckduckgo, f_q, 'wt-wt', 4))

            for fut in concurrent.futures.as_completed(futs):
                try:
                    batch = fut.result() or []
                    all_entries.extend(batch)
                except Exception as e:
                    logger.warning(f"Search worker failed: {e}")

        logger.info(f"Raw search hits (including site-specific queries): {len(all_entries)}")
        return all_entries

    # ───────────────────────── URL resolve ─────────────────────────

    def _resolve_final_url(self, url, raw_title=None):
        if not url:
            return None
        if "news.google.com" not in url:
            return url

        # Decode base64 Google News URL to avoid landing on JS redirect pages
        try:
            match = re.search(r'articles/([^?&]+)', url)
            if match:
                encoded = match.group(1)
                # Pad base64 string
                padded = encoded + '=' * (-len(encoded) % 4)
                import base64
                decoded_bytes = base64.urlsafe_b64decode(padded.encode('ascii'))
                # Extract embedded URL from protobuf bytes
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
        if not final_url or final_url.lower().endswith('.pdf'):
            img = self._get_fallback_image(fallback_snippet)
            return fallback_snippet, img, [img]

        host = urlparse(final_url).netloc.lower()
        if host in self.failed_hosts:
            img = self._pick_image(raw_image, fallback_text=fallback_snippet)
            return fallback_snippet, img, self._dedupe_images([raw_image, img])

        extracted_text = fallback_snippet
        extracted_image = raw_image if self._is_valid_image_url(raw_image) else None
        gallery_images = []
        max_chars = CONFIG.get('MAX_TEXT_CHARS', 6000)

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
                        extracted_image = extracted_image or meta.image
                except Exception:
                    pass
                # Reuse the already-downloaded HTML to build a full image gallery
                # (no extra HTTP request needed).
                try:
                    gallery_soup = BeautifulSoup(downloaded, 'lxml')
                    gallery_images = self._extract_gallery_images(gallery_soup)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"trafilatura failed {final_url}: {e}")
            self.failed_hosts.add(host)

        need_soup = (
            not extracted_image
            or not gallery_images
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

                if not extracted_image:
                    for prop in (
                        ('property', 'og:image'),
                        ('property', 'og:image:secure_url'),
                        ('name', 'twitter:image'),
                        ('name', 'twitter:image:src'),
                        ('itemprop', 'image'),
                    ):
                        tag = soup.find('meta', attrs={prop[0]: prop[1]})
                        if tag and tag.get('content') and self._is_valid_image_url(tag['content']):
                            extracted_image = tag['content'].strip()
                            break

                    if not extracted_image:
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
                                if w and int(str(w).replace('px', '')) < 120:
                                    continue
                                if h and int(str(h).replace('px', '')) < 80:
                                    continue
                            except Exception:
                                pass
                            extracted_image = src
                            break

                if not gallery_images:
                    gallery_images = self._extract_gallery_images(soup)
            except Exception as e:
                logger.warning(f"Soup fallback failed {final_url}: {e}")
                self.failed_hosts.add(host)

        extracted_image = self._pick_image(
            extracted_image,
            raw_image,
            fallback_text=extracted_text or fallback_snippet
        )

        # Merge: main picked image first, then the rest of the gallery, then the
        # raw source image as a fallback — deduplicated and capped.
        all_images = self._dedupe_images([extracted_image, *gallery_images, raw_image])
        if not all_images:
            all_images = [extracted_image]

        return extracted_text, extracted_image, all_images

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
        Analyzes multiple candidate news articles in a SINGLE Gemini API request.
        candidates_data format: list of dicts with {'index', 'source', 'headline', 'text'}
        """
        if not candidates_data or not CONFIG.get('GEMINI_KEY'):
            return {}

        system_prompt = (
            "تو سردبیر ارشد یک خبرنامه تکنولوژی و هوش مصنوعی هستی، مسلط به ادبیات کانال‌های خبری تک فارسی (مثل زومیت و دیجیاتو).\n"
            "وظیفه تو تبدیل اخبار خام تکنولوژی و AI به تحلیل‌های کوتاه، جذاب، کاملاً انسانی، به فارسی روان است.\n\n"
            "🎯 **دستورالعمل پوشش موضوعات (مهم):**\n"
            " - **مدل‌ها و لانچ‌های AI:** انتشار مدل‌های جدید (OpenAI، Google، Anthropic، Meta و...)، ویژگی‌های کلیدی و تفاوت با نسخه قبل را واضح توضیح بده.\n"
            " - **استارتاپ و سرمایه‌گذاری:** مبلغ سرمایه، حوزه فعالیت استارتاپ، و اهمیت آن در بازار را روشن کن.\n"
            " - **گجت و سخت‌افزار:** مشخصات کلیدی، قیمت (در صورت وجود) و تفاوت با رقبا را برجسته کن.\n\n"
            "🔴 **قانون حیاتی حذف اخبار تکراری و هم‌پوشان (Deduplication):**\n"
            "- اگر چند خبر به یک رویداد واحد پرداخته‌اند (مثلاً چند رسانه مختلف عرضه یک مدل جدید را پوشش داده‌اند)، "
            "فقط و فقط یک مورد (کامل‌ترین منبع) را در خروجی بیاور و بقیه ایندکس‌های تکراری را از خروجی JSON حذف کن (آرایه فقط شامل آیتم‌های کاملاً مجزا و غیرتکراری باشد).\n\n"
            "🔴 قوانین حیاتی نگارش و انسانی‌سازی (مهم - حتماً رعایت شود):\n"
            "۱. **روانی، شفافیت و سادگی زبان (مهم):**\n"
            " - از کلمات قلم‌به‌سلم، پیچیده و عجیب دانشگاهی مطلقاً استفاده نکن.\n"
            " - **ممنوعیت ترجمه تحت‌اللفظی:** اصطلاحات تخصصی تک را به فارسی رایج در جامعه فناوری برگردان، نه ترجمه کلمه‌به‌کلمه.\n"
            " - جملات باید بسیار روان، صریح و شفاف باشند تا مخاطب با یک‌بار خواندن متوجه اصل ماجرا شود.\n\n"
            "۲. **ممنوعیت مطلق عبارت‌های کلیشه‌ای رباتیک:**\n"
            " استفاده از این عبارات مطلقاً ممنوع است: ('به نظر می‌رسد'، 'نشان‌دهنده این است که'، 'لازم به ذکر است'، 'در نهایت'، 'پیامدهای عمیق'، 'ابعاد جدیدی از'، 'در این راستا'، 'شایان ذکر است').\n\n"
            "۳. **تنوع در ساختار جملات:**\n"
            " جملات نباید همه با یک فرمول شروع شوند. گاهی با یک فعل حاد، گاهی با یک آمار، و گاهی با یک ارزیابی مستقیم شروع کن.\n\n"
            "۴. **تعداد نقطه‌نظرات شناور:**\n"
            " بخش summary می‌تواند بین ۲ تا ۴ مورد باشد. اگر خبر کوتاه است ۲ نکته عمیق و روان کافیست، برای خبرهای مهم ۴ نکته بنویس. خودت را به ۳ نقطه اجباری محدود نکن.\n\n"
            "۵. **تغییر لحن بر اساس اهمیت (Urgency):**\n"
            " - اگر خبر مهم و تأثیرگذار است (۸ تا ۱۰): لحن ضربتی، کوتاه و هیجان‌انگیز باشد.\n"
            " - اگر خبر تحلیلی/میان‌رده است (۴ تا ۷): لحن توضیحی و روشنگرانه باشد.\n\n"
            "قواعد امتیازبندی فوریت (Urgency Score 1-10):\n"
            "- 9-10: لانچ یک مدل/محصول بزرگ و تأثیرگذار (مثلاً مدل پرچمدار جدید OpenAI/Google/Anthropic)، خرید بزرگ شرکتی، اتفاق نادر در صنعت.\n"
            "- 7-8: عرضه محصول یا فیچر مهم، دور سرمایه‌گذاری بزرگ، تحقیق/breakthrough قابل‌توجه.\n"
            "- 4-6: به‌روزرسانی‌های میان‌رده، اخبار استارتاپی معمولی، تحلیل بازار.\n"
            "- 1-3: اخبار جزئی و روتین.\n\n"
            "تو فهرستی از آیتم‌های خبری با شناسه index دریافت می‌کنی. خروجی باید یک لیست JSON معتبر شامل تحلیل تک تک این آیتم‌ها با ساختار زیر باشد:\n"
            "[\n"
            "  {\n"
            '    "index": 0,\n'
            '    "title_fa": "تیتر جذاب، روان، غیرتکراری و بدون کلمات خنثی (حداکثر ۱۰ کلمه)",\n'
            '    "summary": ["نکته تحلیلی ۱ به فارسی روان و بدون کلمات اضافه", "نکته تحلیلی ۲ با تمرکز بر واقعیت پشت خبر"],\n'
            '    "impact": "تأثیر عملیاتی یا اقتصادی خبر در یک جمله کوتاه، روان و ضربتی",\n'
            '    "tag": "کلمه کلیدی اصلی (مثلاً: هوش‌مصنوعی، استارتاپ، گجت، سخت‌افزار)",\n'
            '    "urgency": عدد بین 1 تا 10,\n'
            '    "sentiment": عدد بین -1.0 تا 1.0\n'
            "  }\n"
            "]"
        )

        items_input = []
        for item in candidates_data:
            items_input.append(
                f"--- ITEM INDEX: {item['index']} ---\n"
                f"SOURCE: {item['source']}\n"
                f"HEADLINE: {item['headline']}\n"
                f"TEXT: {item['text'][:2500]}\n"
            )

        user_prompt = "لطفاً تمامی آیتم‌های زیر را تحلیل و در قالب JSON مشخص‌شده برگردان:\n\n" + "\n".join(items_input)

        data = self._call_gemini(system_prompt, user_prompt, temperature=0.25)
        if isinstance(data, list):
            return {item.get('index'): item for item in data if 'index' in item}
        return {}
        
    def generate_daily_summary(self):
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        todays_items = [
            item for item in self.existing_news
            if datetime.fromtimestamp(item.get("timestamp", 0), timezone.utc) >= today_start
        ]
        if len(todays_items) < 3:
            return None
        todays_items.sort(key=lambda x: x.get("urgency", 0), reverse=True)
        news_context = []
        for item in todays_items[:20]:
            news_context.append(
                f"Title: {item.get('title_en')}\nSource: {item.get('source')}\n"
                f"Urgency: {item.get('urgency')}\nTag: {item.get('tag')}\n"
                f"Impact: {item.get('impact')}\nSummary: {' '.join(item.get('summary', []))}"
            )
        news_block = "\n\n".join(news_context)
        previous_summary = self._load_previous_daily_summary()
        previous_block = ""
        if previous_summary:
            previous_block = (
                f"Previous Strategic Assessment:\nThemes: {previous_summary.get('themes')}\n"
                f"Strategic Assessment: {previous_summary.get('strategic_assessment')}\n"
                f"Market Impact: {previous_summary.get('market_impact')}\n"
                f"Risk Level: {previous_summary.get('risk_level')}"
            )
        return self.analyze_daily_summary_with_ai(news_block, previous_block)

    def analyze_daily_summary_with_ai(self, news_block, previous_block):
        system_prompt = """
You are a senior technology and AI industry analyst writing a rolling daily briefing for a Persian-language tech newsletter.
You will receive:
1) All today's tech/AI news events
2) The previous run's briefing (if available)
Your job:
- Detect what's new or evolved compared to the previous briefing.
- Identify the biggest signals in AI, startups, and hardware today.
- Provide sharp, well-grounded analysis of what these developments mean for the industry.
OUTPUT LANGUAGE: Persian (Farsi)
STRICT OUTPUT JSON:
{
  "date": "YYYY-MM-DD HH:MM",
  "executive_tldr": "1 punchy sentence summarizing today's biggest tech/AI story",
  "themes": [3-5 bullet points on today's key themes],
  "ai_landscape": {
    "model_releases": "1 sentence on notable AI model releases or updates today",
    "research_breakthroughs": "1 sentence on notable AI research or breakthroughs",
    "industry_moves": "1 sentence on major AI company moves (partnerships, hires, pivots)"
  },
  "startup_pulse": "1 sentence on the day's notable funding rounds or startup news",
  "hardware_watch": "1 sentence on notable gadget/hardware/chip news",
  "forecast": {
    "most_likely_scenario": "1 paragraph predicting realistic developments over the next 3-7 days",
    "watch_for": "The specific event/announcement to watch for next"
  },
  "key_players_in_focus": ["Company/Person 1 - Reason", "Company/Person 2 - Reason"],
  "strategic_assessment": "1-2 paragraphs of sharp, realistic industry analysis",
  "market_impact": "1 paragraph on how today's news could affect the tech market",
  "risk_level": "integer (1-10, how disruptive/significant today's news is)",
  "change_from_previous": "افزایش اهمیت | کاهش اهمیت | بدون تغییر"
}
"""
        user_prompt = f"TODAY NEWS:\n{news_block}\n\nPREVIOUS SUMMARY:\n{previous_block}"
        return self._call_gemini(system_prompt, user_prompt, temperature=0.2)

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
        text, photo_url, gallery_images = self.scrape_article_data(
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

        photo_url = self._pick_image(photo_url, entry.get('image'), fallback_text=raw_title)
        images = self._dedupe_images([photo_url, *gallery_images, entry.get('image')])
        news_id = self._generate_news_id(clean_final_url)

        return {
            "id": news_id,
            "title_fa": ai.get('title_fa', raw_title),
            "title_en": raw_title,
            "summary": ai.get('summary', [snippet]),
            "full_text": text,
            "impact": ai.get('impact', '...'),
            "tag": ai.get('tag', 'General'),
            "urgency": urgency_val,
            "sentiment": ai.get('sentiment', 0),
            "source": publisher,
            "url": final_url,
            "clean_url": clean_final_url,
            "image": photo_url,
            "images": images,
            "timestamp": ts
        }

    # ───────────────────────── telegram senders ─────────────────────────

    def send_special_report_to_telegram(self, report):
        """Format and send Special Topic Report to Telegram nightly."""
        token = CONFIG['TELEGRAM']['BOT_TOKEN']
        chat_id = CONFIG['TELEGRAM']['CHANNEL_ID']
        if not token or not chat_id or not report:
            logger.warning("TG credentials or report missing. Skipping TG dispatch.")
            return False

        def esc(s):
            return html.escape(str(s or ''), quote=False)

        tehran_now = self._get_tehran_time()
        time_str = tehran_now.strftime("%H:%M")
        date_str = tehran_now.strftime("%Y/%m/%d")

        tag = esc(report.get('topic_tag', 'پرونده ویژه')).replace(' ', '_')
        headline = esc(report.get('headline', 'گزارش ویژه'))
        lead = esc(report.get('lead_paragraph', ''))

        findings_li = "".join([f"<li>🔹 {esc(f)}</li>\n" for f in report.get('key_findings', [])])
        deep_dive = esc(report.get('deep_dive', ''))
        strategic_outlook = esc(report.get('strategic_outlook', ''))

        rich_html = (
            f"<h1>📂 پرونده ویژه شبانگاهی: {headline}</h1>\n"
            f"<p>⏱ <b>زمان صدور:</b> {time_str} — {date_str} (تهران) | 🏷 #{tag}</p>\n"
            f"<hr/>\n"
            f"<p>📌 <b>اصل ماجرا:</b> {lead}</p>\n"
            f"<h2>🔍 یافته‌های کلیدی</h2>\n"
            f"<ul>\n{findings_li}</ul>\n"
            f"<hr/>\n"
            f"<h2>🔬 نگاه عمیق‌تر</h2>\n"
            f"<p>{deep_dive}</p>\n"
            f"<h2>🔮 چشم‌انداز صنعت</h2>\n"
            f"<p>{strategic_outlook}</p>\n"
            f"\n"
            f"<aside><a href='https://t.me/wirtech'>WirTech</a><cite>Technology News</cite></aside>\n"
        )

        # 1. Send Rich Message
        rich_api = f"https://api.telegram.org/bot{token}/sendRichMessage"
        payload = {
            "chat_id": chat_id,
            "rich_message": {
                "html": rich_html,
                "is_rtl": True,
            },
        }

        try:
            resp = self.scraper.post(rich_api, json=payload, timeout=30)
            if resp.status_code == 200:
                logger.info(">>> Special Topic Report successfully sent as Rich Message.")
                return True
            logger.warning(f"sendRichMessage for Special Report failed ({resp.status_code}), falling back.")
        except Exception as e:
            logger.warning(f"Special Report Rich Message exception: {e}, falling back.")

        # 2. Fallback sendMessage
        findings_text = "".join([f"🔹 {esc(f)}\n" for f in report.get('key_findings', [])])
        fallback_text = (
            f"📂 <b>پرونده ویژه شبانگاهی: {headline}</b>\n"
            f"⏱ <b>زمان:</b> {time_str} — {date_str} | 🏷 #{tag}\n\n"
            f"📌 <b>اصل ماجرا:</b>\n{lead}\n\n"
            f"🔍 <b>یافته‌های کلیدی:</b>\n{findings_text}\n"
            f"🔬 <b>نگاه عمیق‌تر:</b>\n{deep_dive}\n\n"
            f"🔮 <b>چشم‌انداز:</b>\n{strategic_outlook}\n\n"
            f" <aside><a href='https://t.me/wirtech'>WirTech</a><cite>Technology News</cite></aside>"
        )

        standard_api = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            resp = self.scraper.post(standard_api, json={
                "chat_id": chat_id,
                "text": fallback_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=30)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Special Report standard fallback error: {e}")
            return False

    def send_daily_summary_to_telegram(self, summary):
        """Format and send Daily Summary using Telegram Rich Messages with RTL support."""
        token = CONFIG['TELEGRAM']['BOT_TOKEN']
        chat_id = CONFIG['TELEGRAM']['CHANNEL_ID']
        if not token or not chat_id or not summary:
            logger.warning("TG credentials or summary missing. Skipping TG dispatch.")
            return False

        def esc(s):
            return html.escape(str(s or ''), quote=False)

        tehran_now = self._get_tehran_time()
        time_str = tehran_now.strftime("%H:%M")
        date_str = tehran_now.strftime("%Y/%m/%d")

        themes_li = "".join([f"<li>🔹 {esc(t)}</li>\n" for t in summary.get('themes', [])])

        forecast = summary.get('forecast', {})
        most_likely = esc(forecast.get('most_likely_scenario', ''))
        watch_for = esc(forecast.get('watch_for', ''))

        ai_landscape = summary.get('ai_landscape', {})
        ai_text = esc(ai_landscape.get('model_releases') or ai_landscape.get('industry_moves') or '')

        rich_html = (
            f"<h1>📊 خلاصه و ارزیابی روزانه تکنولوژی</h1>\n"
            f"<p>⏱ <b>زمان صدور:</b> {time_str} — {date_str} (تهران)</p>\n"
            f"<hr/>\n"
            f"<details open>\n"
            f"<summary>📌 <b>چکیده مدیریتی</b></summary>\n"
            f"<p>{esc(summary.get('executive_tldr'))}</p>\n"
            f"</details>\n"
            f"<h2>🎯 محورهای کلیدی امروز</h2>\n"
            f"<ul>\n{themes_li}</ul>\n"
            f"<hr/>\n"
            f"<h2>🧠 تحلیل و بررسی صنعت</h2>\n"
            f"<p>{esc(summary.get('strategic_assessment'))}</p>\n"
            f"<h2>🔮 پیش‌بینی روزهای آینده</h2>\n"
            f"<p>{most_likely}</p>\n"
            f"<h2>👀 در انتظار چه چیزی باشیم</h2>\n"
            f"<p>{watch_for}</p>\n"
            f"<hr/>\n"
            f"<h2>📈 ریسک و اهمیت</h2>\n"
            f"<ul>\n"
            f"<li>🚨 <b>سطح اهمیت:</b> {summary.get('risk_level', '?')}/10 ({esc(summary.get('change_from_previous', ''))})</li>\n"
            f"<li>🤖 <b>بروز AI:</b> {ai_text}</li>\n"
            f"</ul>\n"
            f"<aside><a href='https://t.me/wirtech'>WirTech</a><cite>Technology News</cite></aside>\n"
        )

        # 1. Primary Attempt: Send Rich Message
        rich_api = f"https://api.telegram.org/bot{token}/sendRichMessage"
        payload = {
            "chat_id": chat_id,
            "rich_message": {
                "html": rich_html,
                "is_rtl": True,
            },
        }

        try:
            resp = self.scraper.post(rich_api, json=payload, timeout=30)
            if resp.status_code == 200:
                logger.info(">>> Daily Summary successfully sent as Rich Message.")
                return True
            logger.warning(f"sendRichMessage for Daily Summary failed ({resp.status_code}), falling back to sendMessage.")
        except Exception as e:
            logger.warning(f"Daily Summary Rich Message exception: {e}, falling back.")

        # 2. Fallback: Standard Telegram HTML sendMessage
        fallback_text = (
            f"📊 <b>خلاصه و ارزیابی روزانه تکنولوژی</b>\n"
            f"⏱ <b>زمان:</b> {time_str} — {date_str} (تهران)\n\n"
            f"📌 <b>چکیده مدیریتی:</b>\n{esc(summary.get('executive_tldr'))}\n\n"
            f"🧠 <b>تحلیل:</b>\n{esc(summary.get('strategic_assessment'))}\n\n"
            f"🔮 <b>پیش‌بینی:</b>\n{most_likely}\n\n"
            f"📈 <b>سطح اهمیت:</b> <b>{summary.get('risk_level', '?')}/10</b>\n\n"
            f"<aside><a href='https://t.me/wirtech'>WirTech</a><cite>Technology News</cite></aside>"
        )

        standard_api = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            resp = self.scraper.post(standard_api, json={
                "chat_id": chat_id,
                "text": fallback_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=30)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Daily Summary standard fallback error: {e}")
            return False

    def send_bulletin_to_telegram(self, bulletin):
        """Format and send Scheduled Bulletin using Telegram Rich Messages with RTL support."""
        token = CONFIG['TELEGRAM']['BOT_TOKEN']
        chat_id = CONFIG['TELEGRAM']['CHANNEL_ID']
        if not token or not chat_id or not bulletin:
            logger.warning("TG credentials or bulletin missing. Skipping TG dispatch.")
            return False

        def esc(s):
            return html.escape(str(s or ''), quote=False)

        title = esc(bulletin.get('title', 'بولتن خبری'))
        date_str = esc(bulletin.get('date', ''))
        time_str = esc(bulletin.get('time', '23:00'))

        bullets_li = "".join([f"<li>🔹 {esc(b)}</li>\n" for b in bulletin.get('bullets', [])])
        bottom_line = esc(bulletin.get('bottom_line', ''))

        rich_html = (
            f"<h1>🗞 {title}</h1>\n"
            f"<p>⏱ <b>زمان صدور:</b> {time_str} — {date_str} (تهران)</p>\n"
            f"<hr/>\n"
            f"<h2>📌 سرخط مهم‌ترین نکات بولتن</h2>\n"
            f"<ul>\n{bullets_li}</ul>\n"
            f"<hr/>\n"
            f"<details open>\n"
            f"<summary>💡 <b>جمع‌بندی نهایی</b></summary>\n"
            f"<p>{bottom_line}</p>\n"
            f"</details>\n"
            f"<aside><a href='https://t.me/wirtech'>WirTech</a><cite>Technology News</cite></aside>"
        )

        # 1. Primary Attempt: Send Rich Message
        rich_api = f"https://api.telegram.org/bot{token}/sendRichMessage"
        payload = {
            "chat_id": chat_id,
            "rich_message": {
                "html": rich_html,
                "is_rtl": True,
            },
        }

        try:
            resp = self.scraper.post(rich_api, json=payload, timeout=30)
            if resp.status_code == 200:
                logger.info(">>> Scheduled Bulletin successfully sent as Rich Message.")
                return True
            logger.warning(f"sendRichMessage for Bulletin failed ({resp.status_code}), falling back to sendMessage.")
        except Exception as e:
            logger.warning(f"Bulletin Rich Message exception: {e}, falling back.")

        # 2. Fallback: Standard Telegram HTML sendMessage
        bullets_text = "".join([f"🔹 {esc(b)}\n\n" for b in bulletin.get('bullets', [])])
        fallback_text = (
            f"🗞 <b>{title}</b>\n"
            f"⏱ <b>زمان:</b> {time_str} — {date_str} (تهران)\n"
            f"───────────────────\n\n"
            f"{bullets_text}"
            f"💡 <b>جمع‌بندی نهایی:</b>\n{bottom_line}\n\n"
            f"<aside><a href='https://t.me/wirtech'>WirTech</a><cite>Technology News</cite></aside>"
        )

        standard_api = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            resp = self.scraper.post(standard_api, json={
                "chat_id": chat_id,
                "text": fallback_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=30)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Bulletin standard fallback error: {e}")
            return False

    def send_digest_to_telegram(self, items):
        """Send digest via Telegram Rich Messages with real photo blocks."""
        token = CONFIG['TELEGRAM']['BOT_TOKEN']
        chat_id = CONFIG['TELEGRAM']['CHANNEL_ID']
        if not token or not chat_id or not items:
            return

        items.sort(key=lambda x: x.get('urgency', 3), reverse=True)

        def to_farsi_num(num):
            return str(num).translate(str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹'))

        def esc(s):
            return html.escape(str(s or ''), quote=False)

        now_ir = self._get_tehran_time()
        ir_time_str = to_farsi_num(now_ir.strftime("%H:%M"))
        ir_date_str = to_farsi_num(now_ir.strftime("%Y/%m/%d"))

        # ── Collect valid images ──
        photo_urls = []
        for item in items:
            img = item.get('image')
            if self._is_valid_image_url(img) and img not in photo_urls:
                photo_urls.append(img)
            if len(photo_urls) >= 8:
                break
        if not photo_urls:
            photo_urls = [self._get_fallback_image(items[0].get('title_en', 'tech'))]

        # ── Media block(s) ──
        media_html = ""
        if len(photo_urls) == 1:
            media_html = (
                f"<figure>"
                f"<img src=\"{esc(photo_urls[0])}\"/>"
                f"<figcaption>WirTech — {ir_time_str}</figcaption>"
                f"</figure>\n"
            )
        elif len(photo_urls) <= 4:
            imgs = "".join(f"<img src=\"{esc(u)}\"/>" for u in photo_urls)
            media_html = (
                f"<tg-collage>{imgs}"
                f"<figcaption>تصاویر مرتبط با اخبار مهم</figcaption>"
                f"</tg-collage>\n"
            )
        else:
            imgs = "".join(f"<img src=\"{esc(u)}\"/>" for u in photo_urls)
            media_html = (
                f"<tg-slideshow>{imgs}"
                f"<figcaption>گالری اخبار تکنولوژی</figcaption>"
                f"</tg-slideshow>\n"
            )

        # ── Headlines list ──
        headlines_li = []
        for item in items[:10]:
            title = esc(item.get('title_fa') or item.get('title_en'))
            source = esc(item.get('source', ''))
            urgency = item.get('urgency', 3)
            icon = "🔥" if urgency >= 9 else ("🚨" if urgency >= 7 else "🔹")
            src_url = item.get('url') or '#'
            headlines_li.append(
                f"<li>{icon} <a href=\"{esc(src_url)}\">{title}</a> <i>({source})</i></li>"
            )
        headlines_html = "<ul>\n" + "\n".join(headlines_li) + "\n</ul>\n"

        # ── Per-item analysis ──
        details_parts = []
        all_tags = set()
        for i, item in enumerate(items[:6], 1):
            title = esc(item.get('title_fa') or item.get('title_en'))
            source = esc(item.get('source', 'Unknown'))
            impact = esc(item.get('impact', ''))
            src_url = item.get('url') or '#'

            summary_raw = item.get('summary', [])
            if isinstance(summary_raw, str):
                summary_raw = [summary_raw]
            safe_summary = "".join(f"<li>{esc(s)}</li>" for s in summary_raw if s)

            tag = str(item.get('tag', 'General')).replace(' ', '_')
            all_tags.add(f"#{esc(tag)}")

            # Use ALL images gathered for this item (not just one), skipping the
            # hero image already shown in the top collage to avoid repeats.
            item_images = item.get('images') or [item.get('image')]
            item_images = [
                u for u in item_images
                if self._is_valid_image_url(u) and u not in photo_urls[:1]
            ]
            item_images = self._dedupe_images(item_images)
            if not item_images:
                item_media = ""
            elif len(item_images) == 1:
                item_media = f"<img src=\"{esc(item_images[0])}\"/>\n"
            else:
                imgs = "".join(f"<img src=\"{esc(u)}\"/>" for u in item_images)
                item_media = f"<tg-collage>{imgs}</tg-collage>\n"

            full_text = esc(item.get('full_text') or '')
            full_text_html = (
                f"<p>📰 <b>متن کامل خبر:</b></p>\n<p>{full_text}</p>\n"
                if full_text else ""
            )

            open_attr = " open" if i == 1 else ""
            details_parts.append(
                f"<details{open_attr}>\n"
                f"<summary><b>{to_farsi_num(i)}. {title}</b></summary>\n"
                f"{item_media}"
                f"<p>📝 <b>تحلیل خبر:</b></p>\n"
                f"<ul>{safe_summary}</ul>\n"
                f"{full_text_html}"
                f"<p>🎯 <b>اثرگذاری:</b> {impact}</p>\n"
                f"<p>🔗 <a href=\"{esc(src_url)}\">منبع اصلی ({source})</a></p>\n"
                f"</details>\n"
                f"<hr/>\n"
            )
        details_html = "".join(details_parts)

        tags_html = f"<p>{' '.join(sorted(all_tags))}</p>\n" if all_tags else ""

        full_html = (
            f"<h1>🚀 WirTech — اخبار تکنولوژی و هوش مصنوعی</h1>\n"
            f"<p>⏱ <b>زمان بروزرسانی:</b> {ir_time_str} — {ir_date_str} (تهران)</p>\n"
            f"<hr/>\n"
            f"{media_html}"
            f"<h2>📌 سرخط مهم‌ترین اخبار</h2>\n"
            f"{headlines_html}"
            f"<hr/>\n"
            f"<h2>📋 تحلیل و جزئیات</h2>\n"
            f"{details_html}"
            f"{tags_html}"
            f"<aside><a href='https://t.me/wirtech'>WirTech</a><cite>Technology News</cite></aside>"
        )

        if len(full_html) > 30000:
            full_html = full_html[:30000]

        api_url = f"https://api.telegram.org/bot{token}/sendRichMessage"
        payload = {
            "chat_id": chat_id,
            "rich_message": {
                "html": full_html,
                "is_rtl": True,
            },
        }

        try:
            resp = self.scraper.post(api_url, json=payload, timeout=30)
            if resp.status_code == 200:
                logger.info(">>> Rich Message with media blocks sent to Telegram.")
                return

            logger.error(f"sendRichMessage failed: {resp.status_code} | {resp.text[:500]}")

            photo_api = f"https://api.telegram.org/bot{token}/sendPhoto"
            caption_lines = [
                "🚀 <b>WirTech — اخبار تکنولوژی و هوش مصنوعی</b>",
                f"⏱ {ir_time_str} (تهران)",
                "",
            ]
            for item in items[:5]:
                t = esc(item.get('title_fa') or item.get('title_en'))
                u = item.get('urgency', 3)
                icon = "🔥" if u >= 9 else ("🚨" if u >= 7 else "🔹")
                caption_lines.append(f"{icon} {t}")
            caption = "\n".join(caption_lines)[:1024]

            resp2 = self.scraper.post(photo_api, json={
                "chat_id": chat_id,
                "photo": photo_urls[0],
                "caption": caption,
                "parse_mode": "HTML",
            }, timeout=20)
            if resp2.status_code == 200:
                logger.info(">>> Fallback sendPhoto succeeded.")
            else:
                logger.error(f"sendPhoto fallback failed: {resp2.status_code} | {resp2.text[:300]}")
        except Exception as e:
            logger.error(f"TG Rich Message send error: {e}")

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
                    item['image'] = self._pick_image(
                        item.get('image'),
                        fallback_text=item.get('title_en') or item.get('title_fa') or ''
                    )
                    item['images'] = self._dedupe_images(
                        [item['image'], *item.get('images', [])]
                    ) or [item['image']]
                    unique_news.append(item)
            unique_news.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
            final_list = unique_news[:CONFIG['HISTORY_SIZE']]
            self._atomic_json_dump(CONFIG['FILES']['NEWS'], final_list)
            logger.info(">>> news.json updated successfully.")
            return final_list
        except Exception as e:
            logger.error(f"Save Failed: {e}")
            return self.existing_news

    def save_daily_summary(self, summary):
        if not summary:
            return
        try:
            self._atomic_json_dump(CONFIG['FILES']['DAILY_SUMMARY'], summary)
            logger.info(">>> daily_summary.json updated successfully.")
        except Exception as e:
            logger.error(f"Failed to save daily summary: {e}")

    def generate_scheduled_bulletin(self):
        tehran_time = self._get_tehran_time()
        hour = tehran_time.hour
        if 6 <= hour < 12:
            edition_key, edition_title = "morning", "بولتـن صبحگاهی"
        elif 12 <= hour < 18:
            edition_key, edition_title = "midday", "بولتـن نیمروزی"
        else:
            edition_key, edition_title = "evening", "بولتـن شبانگاهی (جمع‌بندی روز)"

        top_items = sorted(self.existing_news, key=lambda x: x.get('urgency', 0), reverse=True)[:5]
        if not top_items:
            return None

        news_text = "\n".join([
            f"- {item.get('title_fa')}: {' '.join(item.get('summary', []))}"
            for item in top_items
        ])
        system_prompt = f"""
تو سردبیر ارشد بخش اخبار فوری هستی. برای "{edition_title}" یک خلاصه خبر ۳ دقیقه‌ای روان، ضربتی و بسیار جذاب به فارسی بنویس.
خروجی باید JSON زیر باشد:
{{
  "edition": "{edition_key}",
  "title": "{edition_title}",
  "time": "{tehran_time.strftime('%H:%M')}",
  "date": "{tehran_time.strftime('%Y/%m/%d')}",
  "bullets": ["نکته ۱", "نکته ۲", "نکته ۳", "نکته ۴"],
  "bottom_line": "نتیجه‌گیری در یک جمله کوتاه"
}}
"""
        data = self._call_gemini(system_prompt, news_text, temperature=0.2)
        if data:
            self._atomic_json_dump('bulletins.json', data)
            logger.info(f">>> Scheduled Bulletin ({edition_title}) generated successfully.")
        return data

    def generate_special_topic_report(self):
        if len(self.existing_news) < 5:
            return None
        tag_clusters = {}
        for item in self.existing_news[:30]:
            tag = item.get('tag', 'عمومی')
            tag_clusters.setdefault(tag, []).append(item)
        top_tag = max(tag_clusters, key=lambda k: len(tag_clusters[k]))
        cluster_items = tag_clusters[top_tag]
        if len(cluster_items) < 2:
            return None

        cluster_context = "\n---\n".join([
            f"منبع: {i.get('source')}\nتیتر: {i.get('title_fa')}\n"
            f"تحلیل: {i.get('impact')}\nخلاصه: {' '.join(i.get('summary', []))}"
            for i in cluster_items[:6]
        ])
        system_prompt = """
تو تیم تحریریه پرونده‌های ویژه یک خبرنامه تکنولوژی و هوش مصنوعی هستی. بر اساس گزارش‌های ورودی که همگی درباره یک موضوع پرخبر امروز در حوزه تک هستند، یک «پرونده ویژه اختصاصی» به فارسی روان، جذاب و تحلیل‌گرایانه بنویس.
خروجی باید JSON زیر باشد:
{
  "topic_tag": "موضوع پرونده",
  "headline": "تیتر اصلی و جذاب پرونده ویژه",
  "lead_paragraph": "مقدمه و اصل ماجرا در دو جمله بسیار روان",
  "key_findings": [
    "یافته و زاویه دید ۱",
    "یافته و زاویه دید ۲",
    "یافته و زاویه دید ۳"
  ],
  "deep_dive": "بررسی عمیق‌تر ابعاد فنی، رقابتی یا بازار این موضوع در یک پاراگراف",
  "strategic_outlook": "پیش‌بینی ادامه روند این پرونده در هفته آینده"
}
"""
        data = self._call_gemini(system_prompt, f"موضوع: {top_tag}\n\nگزارش‌ها:\n{cluster_context}", temperature=0.25)
        if data:
            self._atomic_json_dump('special_reports.json', data)
            logger.info(f">>> Special Report on ({top_tag}) generated successfully.")
        return data

    # ───────────────────────── main run ─────────────────────────

    def run(self):
        logger.info(">>> Radar Started (optimized search + extract + photos)...")

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

            # 1. First pass: filter by age, seen URLs, and exact hashes
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

            # 2. Sort by domain reliability first so top sources are preferred
            candidates.sort(
                key=lambda x: self._domain_score(
                    x.get('url'),
                    x.get('publisher', {}).get('title', '')
                ),
                reverse=True
            )

            # 3. Second pass: Cross-deduplicate against historical news AND within current batch
            accepted_candidates = []
            for item in candidates:
                raw_t = item.get('title', '').rsplit(' - ', 1)[0].strip()
                
                # Check against historical news AND candidates already accepted in this run
                if self._is_duplicate_fuzzy(raw_t, self.existing_news) or self._is_duplicate_fuzzy(raw_t, accepted_candidates):
                    continue

                accepted_candidates.append(item)

            candidates = accepted_candidates[:CONFIG.get('MAX_CANDIDATES', 15)]

        logger.info(
            f"Total Fetched: {len(results)} | Candidates (new/recent/capped): {len(candidates)}"
        )

        new_processed_items = []
        if candidates:
            # 1. Parallel Content Extraction
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
                        text, photo, gallery_images = fut.result()
                        scraped_items.append({
                            'index': idx,
                            'cand': cand,
                            'headline': raw_title,
                            'source': publisher,
                            'url': final_url,
                            'clean_url': clean_u,
                            'snippet': snippet,
                            'text': text,
                            'photo': photo,
                            'gallery_images': gallery_images
                        })
                    except Exception as e:
                        logger.error(f"Scrape worker error: {e}")

            # 2. Batch AI Analysis in ONE Request
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

                    photo_url = self._pick_image(item['photo'], item['cand'].get('image'), fallback_text=item['headline'])
                    images = self._dedupe_images([
                        photo_url, *item.get('gallery_images', []), item['cand'].get('image')
                    ])
                    news_id = self._generate_news_id(item['clean_url'])

                    res = {
                        "id": news_id,
                        "title_fa": ai.get('title_fa', item['headline']),
                        "title_en": item['headline'],
                        "summary": ai.get('summary', [item['snippet']]),
                        "full_text": item['text'],
                        "impact": ai.get('impact', '...'),
                        "tag": ai.get('tag', 'General'),
                        "urgency": urgency_val,
                        "sentiment": ai.get('sentiment', 0),
                        "source": item['source'],
                        "url": item['url'],
                        "clean_url": item['clean_url'],
                        "image": photo_url,
                        "images": images,
                        "timestamp": ts
                    }
                    new_processed_items.append(res)
                    self.seen_urls.add(res['clean_url'])
                    self.recent_title_hashes.add(self._title_hash(res.get('title_en', '')))

        if new_processed_items:
            self.existing_news = self.save_news(new_processed_items)

            telegram_items = []
            min_urgency = CONFIG['MIN_TELEGRAM_URGENCY']
            for item in new_processed_items:
                urgency = item.get('urgency', 0)
                tag = str(item.get('tag', '')).lower()
                is_breaking_tech = any(w in tag for w in [
                    'security', 'hack', 'breach', 'vulnerability', 'exploit', 'ransomware',
                    'outage', 'ban', 'lawsuit', 'acquisition', 'chip', 'launch', 'ai',
                    'امنیت', 'هک', 'نقض', 'آسیب‌پذیری', 'قطعی', 'ممنوعیت', 'ادغام',
                    'تراشه', 'عرضه', 'هوش‌مصنوعی', 'هوش مصنوعی'
                ])
                if urgency >= min_urgency or (urgency >= 6 and is_breaking_tech):
                    telegram_items.append(item)

            if telegram_items:
                logger.info(f"Sending {len(telegram_items)} urgent items to Telegram.")
                self.send_digest_to_telegram(telegram_items)
            else:
                logger.info("New items saved, but urgency too low for Telegram digest.")
        else:
            logger.info(">>> No valid new items found.")

        # ───────────────────────── SCHEDULED DISPATCHES ─────────────────────────
        tehran_now = self._get_tehran_time()
        curr_hour = tehran_now.hour
        today_date_str = tehran_now.strftime("%Y-%m-%d")

        # NIGHTLY SPECIAL REPORT DISPATCH (Target Window: 20:00 -> 02:00 Tehran Time)
        report_date_str = today_date_str
        if 0 <= curr_hour < 2:
            yesterday = tehran_now - timedelta(days=1)
            report_date_str = yesterday.strftime("%Y-%m-%d")

        if curr_hour >= 20 or curr_hour < 2:
            special_report_slot = f"special_report_night_{report_date_str}"
            if not self._is_schedule_already_sent(special_report_slot):
                logger.info(f"Generating nightly Special Topic Report for slot: {special_report_slot}")
                special_report = self.generate_special_topic_report()
                if special_report:
                    sent_ok = self.send_special_report_to_telegram(special_report)
                    if sent_ok:
                        self._mark_schedule_as_sent(special_report_slot)
            else:
                logger.info(f"Nightly Special Report slot [{special_report_slot}] was already sent today.")

        # Always generate and save daily_summary JSON for local dashboard use only (not dispatched to TG)
        daily_summary = self.generate_daily_summary()
        if daily_summary:
            self.save_daily_summary(daily_summary)

        # 23:00 Bulletin Window
        bulletin_date_str = today_date_str
        if 0 <= curr_hour < 2:
            yesterday = tehran_now - timedelta(days=1)
            bulletin_date_str = yesterday.strftime("%Y-%m-%d")

        if curr_hour >= 22 or curr_hour < 2:
            bulletin_slot = f"bulletin_23_{bulletin_date_str}"
            if not self._is_schedule_already_sent(bulletin_slot):
                scheduled_bulletin = self.generate_scheduled_bulletin()
                if scheduled_bulletin:
                    logger.info(f"Triggering 23:00 Bulletin for slot: {bulletin_slot}")
                    sent_ok = self.send_bulletin_to_telegram(scheduled_bulletin)
                    if sent_ok:
                        scheduled_bulletin['telegram_sent'] = True
                        scheduled_bulletin['sent_slot'] = bulletin_slot
                        self._atomic_json_dump('bulletins.json', scheduled_bulletin)
                        self._mark_schedule_as_sent(bulletin_slot)
            else:
                logger.info(f"23:00 Bulletin slot [{bulletin_slot}] was already confirmed sent.")

        logger.info(
            f">>> Done. New={len(new_processed_items)} | "
            f"Failed hosts this run={len(self.failed_hosts)}"
        )


if __name__ == "__main__":
    TechNewsRadar().run()
