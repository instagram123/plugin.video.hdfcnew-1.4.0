# -*- coding: utf-8 -*-
"""Page and source discovery for hdfilmcehennemi.nl.

Navigation: categories (scraped from the site menu), paginated listings,
search, series -> seasons -> episodes.
Playback discovery: the player sources of a movie/episode page and their
language variants (site markup: language tab -> source buttons).

No Kodi imports - usable from tools/test_sources.py.
"""
import copy
import json
import re
import time
from html import unescape

try:
    from urllib.parse import quote, urljoin, urlparse
except ImportError:  # pragma: no cover
    from urllib import quote
    from urlparse import urljoin, urlparse

from .common import HttpError, cache_get, cache_set, log

CATEGORY_CACHE_SECONDS = 24 * 3600
SERIES_FALLBACK_PATH = '/yabancidiziizle-5/'

# Menu groups: url path prefix -> group id
CATEGORY_GROUPS = (
    ('/tur/', 'genres'),
    ('/category/', 'categories'),
    ('/yil/', 'years'),
    ('/ulke/', 'countries'),
    ('/dil/', 'languages'),
)
# Listings that never contain playable titles
CATEGORY_BLACKLIST = re.compile(r'fragman', re.I)


# --------------------------------------------------------------------------
# small HTML helpers
# --------------------------------------------------------------------------
def attr(tag, name):
    m = re.search(r'\b%s\s*=\s*"([^"]*)"' % re.escape(name), tag or '', re.I) or \
        re.search(r"\b%s\s*=\s*'([^']*)'" % re.escape(name), tag or '', re.I)
    return unescape(m.group(1)) if m else ''


def text(fragment):
    fragment = re.sub(r'<(svg|style|script)\b.*?</\1>', ' ', fragment or '', flags=re.S | re.I)
    fragment = re.sub(r'<[^>]+>', ' ', fragment)
    return re.sub(r'\s+', ' ', unescape(fragment)).strip()


def has_class(tag, name):
    return name in attr(tag, 'class').split()


def _first(pattern, html, flags=re.S | re.I):
    m = re.search(pattern, html or '', flags)
    return m.group(1) if m else ''


def _json_ld(html):
    """All JSON-LD objects; the site puts raw newlines inside strings, hence strict=False."""
    out = []
    for blob in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html or '', re.S | re.I):
        try:
            data = json.loads(blob, strict=False)
        except ValueError as exc:
            log('JSON-LD parse error: %s' % exc, 'debug')
            continue
        out.extend(data if isinstance(data, list) else [data])
    return [d for d in out if isinstance(d, dict)]


def is_series_url(url):
    path = urlparse(url).path
    return path.startswith('/dizi/') and '/sezon-' not in path


def episode_numbers(url):
    m = re.search(r'/sezon-(\d+)/bolum-(\d+)', url or '')
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _label_from_slug(path):
    slug = path.strip('/').split('/')[-1]
    slug = re.sub(r'-?\d+$', '', slug)
    words = [w for w in slug.split('-') if w not in ('izle', 'izleyin', 'film', 'filmleri')]
    return ' '.join(w.capitalize() for w in words) or slug


# --------------------------------------------------------------------------
class Scraper(object):
    def __init__(self, http):
        self.http = http
        self.base = http.base_url
        self._embeds = {}  # video_id -> embed URL, for this invocation only

    def page(self, url):
        return self.http.get_text(url, referer=self.base + '/')

    # -- navigation ---------------------------------------------------------
    def categories(self, use_cache=True):
        """{'genres': [{'title','url'}], 'categories': [...], 'years': [...],
        'countries': [...], 'languages': [...], 'series': url}"""
        key = 'categories:' + self.base
        if use_cache:
            cached = cache_get(key, CATEGORY_CACHE_SECONDS)
            if cached:
                return cached
        html = self.page(self.base + '/')
        groups = dict((gid, []) for _, gid in CATEGORY_GROUPS)
        seen = set()
        series = ''
        for m in re.finditer(r'(<a\b[^>]*>)(.*?)</a>', html, re.S | re.I):
            href = attr(m.group(1), 'href')
            if not href:
                continue
            url = urljoin(self.base + '/', href)
            if urlparse(url).netloc != urlparse(self.base).netloc:
                continue
            path = urlparse(url).path
            label = text(m.group(2))
            if label.lower() == 'diziler' and not path.startswith('/dizi/'):
                series = series or url
                continue
            for prefix, gid in CATEGORY_GROUPS:
                if path.startswith(prefix) and len(path) > len(prefix) and path not in seen:
                    if CATEGORY_BLACKLIST.search(path):
                        break
                    seen.add(path)
                    if not label or label.lower() in ('tümünü gör', 'tumunu gor'):
                        label = _label_from_slug(path)
                    groups[gid].append({'title': label, 'url': url})
                    break
        groups['series'] = series or self.base + SERIES_FALLBACK_PATH
        groups['years'].sort(key=lambda c: c['title'], reverse=True)
        if any(groups[gid] for _, gid in CATEGORY_GROUPS):
            cache_set(key, groups)
        else:
            log('no categories found in the site menu', 'warning')
        return groups

    def listing(self, url, page=1, action=None, pages=None):
        """One page of a listing.

        Page 1 is the category page itself; it carries
        <nav class="pagination-container" data-pages="N" data-page-action="genres/x">
        and page n>1 is GET /load/page/<n>/<action>/ -> {"html": ...}."""
        page = int(page or 1)
        if page > 1 and action:
            data = self.http.get_json('/load/page/%d/%s/' % (page, action.strip('/')),
                                      referer=self.absolute(url), ajax=True)
            html = data.get('html', '') if isinstance(data, dict) else ''
            items = parse_posters(html, self.base)
        else:
            html = self.page(url)
            nav = re.search(r'<nav\b[^>]*pagination-container[^>]*>', html, re.I)
            if nav:
                action = attr(nav.group(0), 'data-page-action') or action
                pages = attr(nav.group(0), 'data-pages') or pages
            start = html.find('data-page-items')
            end = html.find('pagination-container', start) if start >= 0 else -1
            block = html[start:end if end > 0 else None] if start >= 0 else html
            items = parse_posters(block, self.base)
        try:
            pages = int(pages or 0)
        except ValueError:
            pages = 0
        return {'items': items, 'page': page, 'pages': pages, 'action': action or ''}

    def search(self, query):
        """GET /search?q= (X-Requested-With: fetch) -> {"results": [html, ...]}"""
        data = self.http.get_json('/search?q=' + quote(query), referer=self.base + '/', ajax=True)
        items = []
        for snippet in (data.get('results') or []) if isinstance(data, dict) else []:
            a = re.search(r'<a\b[^>]*>', snippet)
            href = attr(a.group(0), 'href') if a else ''
            if not href:
                continue
            url = urljoin(self.base + '/', href)
            image = attr(re.search(r'<img\b[^>]*>', snippet).group(0), 'src') if '<img' in snippet else ''
            kind = text(_first(r'<span[^>]*class="type"[^>]*>(.*?)</span>', snippet)).lower()
            items.append({
                'title': text(_first(r'<h4[^>]*>(.*?)</h4>', snippet)),
                'url': url,
                'image': image.replace('/images/thumb/', '/images/list/'),
                'year': text(_first(r'<span[^>]*class="year"[^>]*>(.*?)</span>', snippet)),
                'rating': text(_first(r'<span[^>]*class="imdb"[^>]*>(.*?)</span>', snippet)),
                'lang': '',
                'series': kind == 'dizi' or is_series_url(url),
            })
        return items

    def series(self, url):
        """{'info': title_info, 'seasons': {season: [episode, ...]}}

        Source of truth is JSON-LD TVSeries.containsSeason[].episode[]; episode
        links in the HTML are merged in (some pages lack the JSON-LD)."""
        html = self.page(url)
        info = self.title_info(html, url)
        episodes = {}

        def add(ep_url, season, number, name=''):
            ep_url = urljoin(self.base + '/', ep_url)
            key = (season, number)
            if season and number and key not in episodes:
                episodes[key] = {'url': ep_url, 'season': season, 'episode': number,
                                 'title': name, 'tvshowtitle': info['title']}

        for obj in _json_ld(html):
            if obj.get('@type') != 'TVSeries':
                continue
            for season in obj.get('containsSeason') or []:
                for ep in season.get('episode') or []:
                    s, e = episode_numbers(ep.get('url', ''))
                    try:
                        s = int(season.get('seasonNumber') or s)
                        e = int(ep.get('episodeNumber') or e)
                    except ValueError:
                        pass
                    add(ep.get('url', ''), s, e, unescape(ep.get('name') or ''))
        for m in re.finditer(r'href="([^"]*/sezon-(\d+)/bolum-(\d+)[^"/]*/?)"', html):
            add(m.group(1), int(m.group(2)), int(m.group(3)))

        seasons = {}
        for (s, _), ep in sorted(episodes.items()):
            name = ep['title']
            if name.startswith(info['title']):
                name = name[len(info['title']):].strip(' -')
            ep['title'] = re.sub(r'\s+izle$', '', name, flags=re.I) or '%d. Sezon %d. Bölüm' % (s, ep['episode'])
            seasons.setdefault(s, []).append(ep)
        return {'info': info, 'seasons': seasons}

    def title_info(self, html, url=''):
        """Metadata of a movie / series / episode page for ListItem info tags."""
        info = {'title': '', 'plot': '', 'image': '', 'year': 0, 'genres': [], 'rating': 0.0,
                'url': url, 'mediatype': 'movie'}
        for obj in _json_ld(html):
            kind = obj.get('@type')
            if kind not in ('Movie', 'TVSeries', 'TVEpisode'):
                continue
            info['mediatype'] = {'Movie': 'movie', 'TVSeries': 'tvshow', 'TVEpisode': 'episode'}[kind]
            info['title'] = unescape(obj.get('name') or '')
            info['plot'] = text(obj.get('description') or '')
            info['image'] = obj.get('image') or ''
            genres = obj.get('genre') or []
            info['genres'] = [unescape(g) for g in (genres if isinstance(genres, list) else [genres])]
            try:
                info['rating'] = round(float((obj.get('aggregateRating') or {}).get('ratingValue') or 0), 1)
            except (TypeError, ValueError):
                pass
            break
        h1 = _first(r'<h1[^>]*class="[^"]*section-title[^"]*"[^>]*>(.*?)</h1>', html)
        year = _first(r'<small>\s*\(?(\d{4})\)?\s*</small>', h1)
        if year:
            info['year'] = int(year)
        if not info['title']:
            info['title'] = text(re.sub(r'<small>.*?</small>', '', h1, flags=re.S))
        info['title'] = re.sub(r'\s+izle$', '', info['title'], flags=re.I).strip()
        if not info['plot']:
            info['plot'] = text(_first(r'<article[^>]*post-info-content[^>]*>\s*<p[^>]*>(.*?)</p>', html))
        if info['mediatype'] == 'episode' or '/sezon-' in url:
            info['mediatype'] = 'episode'
            info['season'], info['episode'] = episode_numbers(url)
            info['tvshowtitle'] = re.sub(r'\s*\d+\.\s*Sezon.*$', '', info['title'])
        return info

    def absolute(self, url):
        return urljoin(self.base + '/', url or '')

    # -- player sources ----------------------------------------------------
    def sources(self, html):
        """Player sources of a movie/episode page, source first:

        [{'name': 'close', 'label': 'Close',
          'variants': [{'lang': 'dual', 'lang_label': 'DUAL (...)', 'video_id': '248753', 'label': 'Close'}]}]

        Site markup (2026-10):
          <nav class="video-alternatives">
            <button class="language-link" data-lang="dual">DUAL (Türkçe Dublaj & Altyazılı)</button>
            <div class="alternative-links" data-lang="dual">
              <button class="alternative-link" data-video="248753">Close</button>
              <button class="alternative-link" data-video="247126">Rapidrame</button>
        """
        nav = re.search(r'<nav[^>]*class="[^"]*\bvideo-alternatives\b[^"]*"[^>]*>(.*?)</nav>', html or '',
                        re.S | re.I)
        nav = nav.group(1) if nav else ''
        tab_labels = {}
        for m in re.finditer(r'(<button\b[^>]*>)(.*?)</button>', nav, re.S | re.I):
            if has_class(m.group(1), 'language-link'):
                tab_labels[attr(m.group(1), 'data-lang')] = text(m.group(2))
        sources = []
        by_name = {}
        for g in re.finditer(r'(<div\b[^>]*\balternative-links\b[^>]*>)(.*?)</div>', nav, re.S | re.I):
            lang = attr(g.group(1), 'data-lang')
            for b in re.finditer(r'(<button\b[^>]*>)(.*?)</button>', g.group(2), re.S | re.I):
                video_id = attr(b.group(1), 'data-video')
                if not video_id:
                    continue
                label = text(b.group(2)) or 'Player'
                name = re.split(r'[\s(]', label.lower(), 1)[0] or 'player'
                source = by_name.get(name)
                if source is None:
                    source = by_name[name] = {'name': name, 'label': label, 'variants': []}
                    sources.append(source)
                source['variants'].append({'lang': lang, 'lang_label': tab_labels.get(lang) or lang.upper(),
                                           'video_id': video_id, 'label': label})
        return sources

    def embed_url(self, video_id, page_url):
        """GET /video/<id>/ -> {"success":true,"data":{"html":"<iframe data-src=...>"}}"""
        if video_id in self._embeds:
            return self._embeds[video_id]
        data = self.http.get_json('/video/%s/' % video_id, referer=page_url, ajax=True)
        html = ((data or {}).get('data') or {}).get('html', '') if isinstance(data, dict) else ''
        src = _first(r'<iframe[^>]+?(?:data-src|src)="([^"]+)"', html)
        if not src:
            raise HttpError('no iframe in /video/%s/ response' % video_id)
        self._embeds[video_id] = urljoin(page_url, src.replace('\\/', '/'))
        return self._embeds[video_id]


def parse_posters(html, base):
    """<a class="poster" href=...> cards of listing pages."""
    items = []
    seen = set()
    for m in re.finditer(r'(<a\b[^>]*>)(.*?)</a>', html or '', re.S | re.I):
        tag, body = m.group(1), m.group(2)
        if not has_class(tag, 'poster'):
            continue
        url = urljoin(base + '/', attr(tag, 'href'))
        if not attr(tag, 'href') or url in seen:
            continue
        seen.add(url)
        img = re.search(r'<img\b[^>]*>', body)
        image = ''
        if img:
            image = attr(img.group(0), 'data-src') or attr(img.group(0), 'src')
            if image.startswith('data:'):
                image = ''
        meta = re.findall(r'<span[^>]*>(.*?)</span>', _first(r'<div[^>]*class="poster-meta"[^>]*>(.*?)</div>', body))
        year = next((text(s) for s in meta if re.match(r'^\s*\d{4}\s*$', text(s))), '')
        items.append({
            'title': text(_first(r'<strong[^>]*poster-title[^>]*>(.*?)</strong>', body)) or attr(tag, 'title'),
            'url': url,
            'image': urljoin(base + '/', image) if image else '',
            'year': year,
            'rating': text(_first(r'<span[^>]*class="imdb"[^>]*>(.*?)</span>', body)),
            'lang': text(_first(r'<span[^>]*class="poster-lang"[^>]*>(.*?)</span>\s*</div>', body)),
            'series': is_series_url(url),
        })
    return items


# --------------------------------------------------------------------------
# Details page of a movie / series (added for the details view; nothing above
# calls it). Markup of a title page (2026-10), see investigation/samples/:
#
#   <h1 class="section-title">Title izle <small>(2026)</small></h1>
#   <aside class="post-info-poster"><img data-src=... data-srcset="x.webp 1x, x@2x.webp 2x"></aside>
#   <div class="play-that-video"><img src=.../images/list/cover/x.webp srcset="... 1x, ...@2x.webp 2x"></div>
#   <article class="post-info-content"><p>plot</p></article>
#   <div class="post-info-imdb-rating"><span>8.2 <small>(568255 oy)</small></span></div>
#   <a data-id="tt12042730"> inside .post-info-imdb
#   <button data-modal="trailer/YOUTUBE_ID"> inside .post-info-trailer
#   <div class="post-info-duration">78 dakika</div>            (0 dakika = unknown)
#   <div class="post-info-year-country"><a>2026</a><a>ABD</a></div>
#   <div class="post-info-genres"><a>Bilim Kurgu</a></div>
#   <div class="post-info-cast"><a title="Name"><img><strong>Name</strong><small>role</small></a>...</div>
#
# The site has no director and no original title; the JSON-LD "director" is the
# site's own brand name and is ignored.
# --------------------------------------------------------------------------
DETAILS_CACHE_SECONDS = 600
DETAILS_CACHE_MAX = 64
DETAILS_CAST_LIMIT = 8
_DETAILS_CACHE = {}  # url -> (time, details); in memory only, the window lives as long as the Python process
_BRAND = re.compile(r'hdfilm\s*cehennemi', re.I)


def empty_details(url=''):
    """Every key of fetch_movie_details(), empty. Strings are '', lists are []."""
    return {'url': url, 'title': '', 'original_title': '', 'poster': '', 'backdrop': '', 'plot': '',
            'year': '', 'duration': '', 'rating': '', 'votes': '', 'imdb_id': '', 'genres': [],
            'countries': [], 'director': '', 'cast': [], 'trailer': '', 'languages': [], 'sources': [],
            'series': False, 'error': ''}


def _div(html, cls):
    """Inner HTML of the first <div class="... cls ...">. The blocks read here contain no nested div."""
    m = re.search(r'<div\b[^>]*\bclass="[^"]*(?<![\w-])%s(?![\w-])[^"]*"[^>]*>(.*?)</div>' % re.escape(cls),
                  html or '', re.S | re.I)
    return m.group(1) if m else ''


def _anchors(fragment):
    return [text(m.group(1)) for m in re.finditer(r'<a\b[^>]*>(.*?)</a>', fragment or '', re.S | re.I)
            if text(m.group(1))]


def _image_url(tag, base):
    """Best URL of an <img>: the highest density of data-srcset/srcset, else data-src, else src.
    The lazy-load placeholder in src/srcset is a base64 data: URI and is skipped."""
    srcset = attr(tag, 'data-srcset')
    if not srcset:
        candidate = attr(tag, 'srcset')
        srcset = '' if candidate.startswith('data:') else candidate
    best, density = '', 0.0
    for part in srcset.split(','):
        bits = part.split()
        if not bits:
            continue
        try:
            value = float(bits[1].rstrip('x')) if len(bits) > 1 else 1.0
        except ValueError:
            value = 1.0
        if value >= density:
            best, density = bits[0], value
    url = best or attr(tag, 'data-src') or attr(tag, 'src')
    return urljoin((base or '') + '/', url) if url and not url.startswith('data:') else ''


def _ld_main(html):
    return next((o for o in _json_ld(html) if o.get('@type') in ('Movie', 'TVSeries', 'TVEpisode')), {})


def _d_title(html, ld):
    name = unescape(ld.get('name') or '')
    if not name:
        h1 = _first(r'<h1[^>]*class="[^"]*section-title[^"]*"[^>]*>(.*?)</h1>', html)
        name = text(re.sub(r'<small>.*?</small>', '', h1, flags=re.S))
    return re.sub(r'\s+izle$', '', name, flags=re.I).strip()


def _d_year(html):
    h1 = _first(r'<h1[^>]*class="[^"]*section-title[^"]*"[^>]*>(.*?)</h1>', html)
    year = _first(r'<small>\s*\(?(\d{4})\)?\s*</small>', h1)
    if not year:
        year = next((a for a in _anchors(_div(html, 'post-info-year-country')) if re.match(r'^\d{4}$', a)), '')
    return year


def _d_duration(html, ld):
    """Minutes as a string, '' when unknown (the site shows '0 dakika' for new titles)."""
    m = re.search(r'(\d+)', text(_div(html, 'post-info-duration')))
    minutes = int(m.group(1)) if m else 0
    if not minutes:
        m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?', str(ld.get('duration') or ld.get('timeRequired') or ''))
        if m:
            minutes = int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    return str(minutes) if minutes > 0 else ''


def _d_imdb(html):
    """(rating, votes) of the IMDb box; the JSON-LD aggregateRating is the site's own vote, not IMDb."""
    span = _first(r'<span[^>]*>(.*?)</span>', _div(html, 'post-info-imdb-rating'))
    m = re.match(r'\s*(\d+(?:[.,]\d+)?)', text(re.sub(r'<small.*?</small>', '', span, flags=re.S)))
    rating = m.group(1).replace(',', '.') if m else ''
    if rating and float(rating) <= 0:
        rating = ''
    votes = re.sub(r'\D', '', text(_first(r'<small[^>]*>(.*?)</small>', span))) if rating else ''
    return rating, votes


def _d_cast(html, ld):
    names = [text(m.group(1)) for m in re.finditer(r'<strong[^>]*>(.*?)</strong>', _div(html, 'post-info-cast'), re.S)]
    if not any(names):
        actors = ld.get('actor') or []
        names = [unescape(a.get('name') or '') for a in (actors if isinstance(actors, list) else [actors])
                 if isinstance(a, dict)]
    return [n for n in names if n][:DETAILS_CAST_LIMIT]


def _d_director(ld):
    director = ld.get('director') or []
    names = [unescape(d.get('name') or '') for d in (director if isinstance(director, list) else [director])
             if isinstance(d, dict)]
    return ', '.join(n for n in names if n and not _BRAND.search(n))


def parse_movie_details(html, url='', base=''):
    """Details dict (see empty_details) from the HTML of a movie / series page. Never raises:
    a field that cannot be read stays empty."""
    d = empty_details(url)
    html = html or ''

    def field(key, fn, *args):
        try:
            d[key] = fn(*args)
        except Exception as exc:  # markup changed: log and keep the field empty
            log('details field %s: %s' % (key, exc), 'debug')

    ld = _ld_main(html)
    field('series', lambda: ld.get('@type') == 'TVSeries' or is_series_url(url))
    field('title', _d_title, html, ld)
    field('original_title', lambda: unescape(ld.get('alternateName') or ld.get('originalTitle') or ''))
    field('year', _d_year, html)
    field('plot', lambda: text(_first(r'<article[^>]*post-info-content[^>]*>(.*?)</article>', html))
          or text(ld.get('description') or ''))

    def poster():
        img = re.search(r'<img\b[^>]*>', _first(r'<aside[^>]*post-info-poster[^>]*>(.*?)</aside>', html))
        return (_image_url(img.group(0), base) if img else '') or ld.get('image') or ''
    field('poster', poster)

    def backdrop():
        img = re.search(r'class="play-that-video"[^>]*>\s*(<img\b[^>]*>)', html, re.S)
        return _image_url(img.group(1), base) if img else ''
    field('backdrop', backdrop)

    field('duration', _d_duration, html, ld)
    try:
        d['rating'], d['votes'] = _d_imdb(html)
    except Exception as exc:
        log('details field rating: %s' % exc, 'debug')
    field('imdb_id', lambda: attr(_first(r'(<a\b[^>]*\bdata-id="tt\d+"[^>]*>)', html), 'data-id'))
    field('genres', lambda: _anchors(_div(html, 'post-info-genres')) or
          [unescape(g) for g in (ld.get('genre') if isinstance(ld.get('genre'), list) else [ld.get('genre') or ''])
           if g])
    field('countries', lambda: [a for a in _anchors(_div(html, 'post-info-year-country'))
                                if not re.match(r'^\d{4}$', a)])
    field('director', _d_director, ld)
    field('cast', _d_cast, html, ld)
    field('trailer', lambda: _first(r'data-modal="trailer/([\w-]+)"', html))
    return d


def fetch_movie_details(movie_url, http=None, use_cache=True):
    """Open a movie / series page and return its details (keys of empty_details()).

    Never raises: on a network error the empty dict comes back with 'error' set (and is not cached).
    Results are kept in memory for DETAILS_CACHE_SECONDS (10 minutes)."""
    hit = _DETAILS_CACHE.get(movie_url)
    if use_cache and hit and time.time() - hit[0] < DETAILS_CACHE_SECONDS:
        return copy.deepcopy(hit[1])
    try:
        from .common import make_http
        scraper = Scraper(http or make_http())
        url = scraper.absolute(movie_url)
        html = scraper.page(url)
        details = parse_movie_details(html, url, scraper.base)
        sources = scraper.sources(html)
        details['sources'] = [s['label'] for s in sources]
        for source in sources:
            for variant in source['variants']:
                if variant['lang_label'] not in details['languages']:
                    details['languages'].append(variant['lang_label'])
    except Exception as exc:
        log('details %s failed: %s' % (movie_url, exc), 'warning')
        details = empty_details(movie_url)
        details['error'] = str(exc) or exc.__class__.__name__
        return details
    if len(_DETAILS_CACHE) >= DETAILS_CACHE_MAX:
        _DETAILS_CACHE.pop(min(_DETAILS_CACHE, key=lambda k: _DETAILS_CACHE[k][0]))
    _DETAILS_CACHE[movie_url] = (time.time(), copy.deepcopy(details))
    return details
