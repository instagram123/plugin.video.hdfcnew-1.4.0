# -*- coding: utf-8 -*-
"""Stream extraction (Close, Rapidrame), language variants and Kodi playback.

Extractors share one interface::

    extract_close(embed_url, referer, http) / extract_rapidrame(embed_url, referer, http) ->
    {'url': ..., 'type': 'hls' | 'mp4', 'headers': {...},
     'subtitles': [{'lang', 'url', 'label', 'forced', 'default'}],
     'audio': [{'name', 'lang', 'default', 'turkish'}],   # HLS audio renditions
     'embed': embed_url, 'source': 'close' | 'rapidrame' | ...}

Both players are JW Player pages whose stream URL is produced by a
per-request randomized JavaScript decoder (plus a decoy). We run that decoder
with jsdecode.JSInterpreter instead of pattern-matching its constants.

The Kodi part (ListItem, setResolvedUrl, audio/subtitle switching after
onAVStarted) is only defined when running inside Kodi.
"""
import json
import os
import re
import time

try:
    from urllib.parse import quote, urljoin, urlparse
except ImportError:  # pragma: no cover
    from urllib import quote
    from urlparse import urljoin, urlparse

from . import jsdecode
from .common import HttpError, IN_KODI, addon, kodi_major, localize, log, make_http, setting_bool, sleep, temp_dir

if IN_KODI:
    import xbmc
    import xbmcgui
    import xbmcplugin


class ExtractionError(Exception):
    pass


# Language variant ids (also used by the "Preferred language" setting)
TR_DUB, TR_SUB, ORIGINAL = 'tr_dub', 'tr_sub', 'original'


# --------------------------------------------------------------------------
# JW Player config -> stream URL + tracks
# --------------------------------------------------------------------------
def _balanced(text, start):
    """text[start:end] of the bracket expression starting at text[start]."""
    pairs = {'[': ']', '{': '}', '(': ')'}
    stack, i, quote_char = [], start, None
    while i < len(text):
        c = text[i]
        if quote_char:
            if c == '\\':
                i += 2
                continue
            if c == quote_char:
                quote_char = None
        elif c in '"\'`':
            quote_char = c
        elif c in pairs:
            stack.append(pairs[c])
        elif stack and c == stack[-1]:
            stack.pop()
            if not stack:
                return text[start:i + 1]
        i += 1
    return ''


def _config_value(code, key):
    """Source text of ``key: <expr>`` (first occurrence) inside a JS object."""
    m = re.search(r'["\']?\b%s["\']?\s*:\s*' % key, code)
    if not m:
        return ''
    i = m.end()
    if i < len(code) and code[i] in '[{(':
        return _balanced(code, i)
    j, depth = i, 0
    while j < len(code):
        c = code[j]
        if c in '([{':
            depth += 1
        elif c in ')]}':
            if depth == 0:
                break
            depth -= 1
        elif c == ',' and depth == 0:
            break
        j += 1
    return code[i:j].strip()


def parse_jwplayer(html):
    """Return (stream_url, tracks, sources_expr) from an embed page.

    1. collect inline scripts + unpacked p.a.c.k.e.r blobs (Rapidrame packs its decoder),
    2. drop commented-out lines (Rapidrame has ``//sources: [{file:atob(file_link)}]``),
    3. find ``sources: [{file: <expr>`` and ``tracks: [...]``,
    4. run the scripts that assign the identifiers used by <expr>, evaluate <expr>.
    """
    scripts = [m.group(1) for m in re.finditer(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>',
                                               html or '', re.S | re.I)]
    blobs = []
    for script in scripts:
        blobs.extend(jsdecode.find_packed(script))
    units = [re.sub(r'(?m)^\s*//.*$', '', code) for code in scripts + blobs]

    sources_expr = tracks_expr = ''
    for code in units:
        m = re.search(r'\bsources\s*:\s*\[\s*\{', code)
        if m:
            sources_expr = _config_value(_balanced(code, m.end() - 1), 'file')
            tracks_expr = _config_value(code[m.start():], 'tracks')
            break
    if not sources_expr:
        raise ExtractionError('JW Player "sources" not found in embed page')

    js = jsdecode.JSInterpreter()
    names = set(re.findall(r'[A-Za-z_$][\w$]*', sources_expr)) - {'atob', 'decodeURIComponent', 'unescape'}
    for code in units:
        if any(re.search(r'(?:\bvar|\blet|\bconst|[;,{}\s])\s*%s\s*=(?!=)' % re.escape(n), code) for n in names):
            js.run(code, tolerant=True)
    try:
        stream = js.evaluate(sources_expr)
    except jsdecode.JSError as exc:
        raise ExtractionError('decoder failed: %s' % exc)
    if not isinstance(stream, jsdecode.str_types) or not re.match(r'(?:https?:)?//|/', stream):
        raise ExtractionError('decoded source is not a URL: %r' % (stream,))

    tracks = []
    if tracks_expr:
        try:
            tracks = js.evaluate(tracks_expr)
        except jsdecode.JSError:
            try:
                tracks = json.loads(tracks_expr.replace('\\/', '/'))
            except ValueError:
                tracks = []
    return stream, [t for t in tracks if isinstance(t, dict)] if isinstance(tracks, list) else [], sources_expr


# --------------------------------------------------------------------------
# Subtitles
# --------------------------------------------------------------------------
# Close labels tracks with English language names, Rapidrame with Turkish labels
# plus a "language" field ("tr", "en", "forced").
LANG_NAMES = {
    'turkish': 'tr', 't\u00fcrk\u00e7e': 'tr', 'turkce': 'tr', 'tur': 'tr',
    'english': 'en', 'eng': 'en', 'ingilizce': 'en', 'i\u0307ngilizce': 'en',
    'portuguese': 'pt', 'spanish': 'es', 'french': 'fr', 'german': 'de', 'italian': 'it',
    'dutch': 'nl', 'danish': 'da', 'finnish': 'fi', 'swedish': 'sv', 'norwegian': 'no',
    'polish': 'pl', 'romanian': 'ro', 'bulgarian': 'bg', 'greek': 'el', 'ukrainian': 'uk',
    'russian': 'ru', 'arabic': 'ar', 'indonesian': 'id', 'malay': 'ms', 'japanese': 'ja',
    'korean': 'ko', 'chinese': 'zh', 'thai': 'th', 'hungarian': 'hu', 'czech': 'cs',
    'hebrew': 'he', 'persian': 'fa', 'vietnamese': 'vi', 'croatian': 'hr', 'serbian': 'sr',
}
_ISO1 = set(LANG_NAMES.values())
ISO2 = {'tr': 'tur', 'en': 'eng', 'pt': 'por', 'es': 'spa', 'fr': 'fre', 'de': 'ger', 'it': 'ita',
        'nl': 'dut', 'da': 'dan', 'fi': 'fin', 'sv': 'swe', 'no': 'nor', 'pl': 'pol', 'ro': 'rum',
        'bg': 'bul', 'el': 'gre', 'uk': 'ukr', 'ru': 'rus', 'ar': 'ara', 'id': 'ind', 'ms': 'may',
        'ja': 'jpn', 'ko': 'kor', 'zh': 'chi', 'th': 'tha', 'hu': 'hun', 'cs': 'cze', 'he': 'heb',
        'fa': 'per', 'vi': 'vie', 'hr': 'hrv', 'sr': 'srp'}


def subtitle_language(label, language, url):
    """(iso 639-1 code, forced) of one JW Player track."""
    forced = language == 'forced' or 'forced' in label.lower()
    if language and language != 'forced':
        return language, forced
    for word in re.findall(r'[^\W\d_]+', label.lower()):
        if word in LANG_NAMES:
            return LANG_NAMES[word], forced
    # Close file names carry the code: ...-tur-..., ...subtitles02.tur.vtt, -fr-, -pt-
    for token in re.split(r'[-_.]', os.path.basename(urlparse(url).path).lower()):
        if token in LANG_NAMES:
            return LANG_NAMES[token], forced
        if token in _ISO1:
            return token, forced
    # A bare "Forced" track is the Turkish forced track (both players treat it so).
    return ('tr' if forced else 'und'), forced


def normalize_tracks(tracks, embed_url):
    subs = []
    for t in tracks:
        if (t.get('kind') or 'captions') not in ('captions', 'subtitles'):
            continue
        url = t.get('file') or t.get('src')
        if not url:
            continue
        url = urljoin(embed_url, url)
        label = (t.get('label') or '').strip()
        lang, forced = subtitle_language(label, (t.get('language') or '').lower(), url)
        subs.append({'lang': lang, 'label': label, 'url': url, 'forced': forced,
                     'default': bool(t.get('default'))})
    return subs


# --------------------------------------------------------------------------
# HLS
# --------------------------------------------------------------------------
TURKISH_AUDIO = re.compile(r't[u\u00fc]rk|dublaj', re.I)


def parse_audio_renditions(master):
    renditions = []
    for line in master.splitlines():
        if line.startswith('#EXT-X-MEDIA:') and 'TYPE=AUDIO' in line:
            attrs = dict(re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line[len('#EXT-X-MEDIA:'):]))
            attrs = dict((k, v.strip('"')) for k, v in attrs.items())
            name, lang = attrs.get('NAME', ''), attrs.get('LANGUAGE', '').lower()
            renditions.append({'name': name, 'lang': lang, 'default': attrs.get('DEFAULT') == 'YES',
                               'turkish': lang in ('tr', 'tur') or bool(TURKISH_AUDIO.search(name))})
    return renditions


def probe_hls(http, url, headers):
    """Download the master playlist (validates URL + headers) and return its audio renditions."""
    try:
        resp = http.get(url, headers=headers)
    except HttpError as exc:
        raise ExtractionError('master playlist: %s' % exc)
    body = resp.content.decode('utf-8', 'replace')
    if '#EXTM3U' not in body[:200]:
        raise ExtractionError('not an HLS playlist (%s)' % resp.headers.get('content-type'))
    return parse_audio_renditions(body)


def stream_headers(embed_url, user_agent):
    """Both CDNs answer 404 without a Referer; the embed page URL works for both."""
    p = urlparse(embed_url)
    return {'User-Agent': user_agent, 'Referer': embed_url, 'Origin': '%s://%s' % (p.scheme, p.netloc)}


# --------------------------------------------------------------------------
# Extractors
# --------------------------------------------------------------------------
def _jwplayer_stream(http, embed_url, referer, source):
    http = http or make_http()
    html = http.get_text(embed_url, referer=referer)
    stream, tracks, expr = parse_jwplayer(html)
    url = urljoin(embed_url, stream)
    headers = stream_headers(embed_url, http.user_agent)
    kind = 'mp4' if re.search(r'\.mp4(?:$|\?)', url) and '/hls' not in url else 'hls'
    audio = probe_hls(http, url, headers) if kind == 'hls' else []
    log('%s: sources[0].file=%s -> %s (%d audio, %d subtitle tracks)'
        % (source, expr, url, len(audio), len(tracks)), 'debug')
    return {'url': url, 'type': kind, 'headers': headers, 'subtitles': normalize_tracks(tracks, embed_url),
            'audio': audio, 'embed': embed_url, 'source': source}


def extract_close(embed_url, referer=None, http=None):
    """Close player - https://hdfilmcehennemi.mobi/video/embed/<id>/?rapidrame_id=<rid>

    * decoder: plain inline <script> (randomized per request) + decoy URL that
      equals the JSON-LD "contentUrl" - only sources[0].file is real
    * stream: https://srvN.cdnimagesNNNN.shop|.cyou/hls/<file>.mp4/txt/master.txt
      (host changes on every request), TS segments disguised as .jpg, separate
      audio renditions NAME="Turkish" / "Original Audio" without LANGUAGE
    * headers: Referer = embed URL (hdfilmcehennemi.mobi) - site root gives 404
    """
    return _jwplayer_stream(http, embed_url, referer, 'close')


def extract_rapidrame(embed_url, referer=None, http=None):
    """Rapidrame player - https://www.hdfilmcehennemi.nl/rplayer/<rid>/

    * decoder: inside a p.a.c.k.e.r eval() blob, same two templates as Close
    * stream: https://sNNN.rapidrame.com/hls2/.../<rid>_,l,n,.urlset/master.m3u8?t=<token>&s=<ts>&e=14400
      (signed, expires after 4 h - always resolve at play time)
    * audio renditions LANGUAGE="tr" (Türkçe) + original (en/fr/de/ja/...)
    * subtitles are relative (/srt/<n>/<rid>_Turkish.vtt), "language": tr/en/forced
    * headers: Referer = embed URL (any www.hdfilmcehennemi.nl page works)
    """
    return _jwplayer_stream(http, embed_url, referer, 'rapidrame')


def extract_generic(embed_url, referer=None, http=None):
    """Unknown player button: try the same JW Player approach."""
    return _jwplayer_stream(http, embed_url, referer, 'generic')


EXTRACTORS = {'close': extract_close, 'rapidrame': extract_rapidrame}


def extract(http, source_name, embed_url, referer):
    """Run the extractor for one embed; retry once (tokens / anti-bot / flaky CDN)."""
    extractor = EXTRACTORS.get(source_name, extract_generic)
    last = None
    for attempt in (1, 2):
        try:
            return extractor(embed_url, referer=referer, http=http)
        except (ExtractionError, HttpError, jsdecode.JSError) as exc:
            last = exc
            log('%s extraction attempt %d failed for %s: %s' % (source_name, attempt, embed_url, exc), 'warning')
            if attempt == 1 and sleep(1):
                break
    raise ExtractionError('%s: %s' % (source_name, last))


# --------------------------------------------------------------------------
# Language variants
# --------------------------------------------------------------------------
def language_options(variant, info):
    """Language versions that really exist for one source inside one language tab.

    option = {'id': tr_dub|tr_sub|original|<tab>, 'audio': 'turkish'|'original'|None,
              'subtitle': track|None, 'hardsub': bool, 'tab': ..., 'tab_label': ..., 'stream': info}
    'audio' is applied in Kodi after playback starts; None = keep the only/default track.
    """
    subs, audio = info['subtitles'], info['audio']
    tr_sub = next((s for s in subs if s['lang'] == 'tr' and not s['forced']), None)
    tr_forced = next((s for s in subs if s['lang'] == 'tr' and s['forced']), None)
    has_tr_audio = any(a['turkish'] for a in audio)
    has_orig_audio = any(not a['turkish'] for a in audio)
    tab = variant['lang']
    opts = []

    def add(oid, audio_pref, subtitle, hardsub=False):
        opts.append({'id': oid, 'audio': audio_pref, 'subtitle': subtitle, 'hardsub': hardsub,
                     'tab': tab, 'tab_label': variant['lang_label'], 'stream': info})

    if tab == 'dual' or (has_tr_audio and has_orig_audio):
        # one stream, two audio tracks: switch tracks like the site's own buttons
        if has_tr_audio:
            add(TR_DUB, 'turkish', tr_forced)
        if has_orig_audio and tr_sub:
            add(TR_SUB, 'original', tr_sub)
        if has_orig_audio:
            add(ORIGINAL, 'original', None)
    elif tab == 'tr':
        add(TR_DUB, None, tr_forced)
    elif tab == 'en':
        # soft Turkish track -> switchable; no track at all -> burned-in subtitles
        add(TR_SUB, None, tr_sub, hardsub=tr_sub is None)
        if tr_sub:
            add(ORIGINAL, None, None)
    else:
        add(tab or 'other', None, tr_sub)
    return opts


def resolve_source(scraper, page_url, source):
    """All language options of one source (resolved now, never cached).

    Tabs that point at the same embed (e.g. Rapidrame "dual" and "tr") are
    merged. Raises ExtractionError when no variant could be resolved."""
    options, seen, errors = [], set(), []
    for variant in source['variants']:
        try:
            embed = scraper.embed_url(variant['video_id'], page_url)
        except HttpError as exc:
            errors.append('%s/%s: %s' % (source['name'], variant['lang'], exc))
            log(errors[-1], 'error')
            continue
        key = re.sub(r'[?#].*$', '', embed)
        if key in seen:
            log('%s tab %s uses the same embed as another tab' % (source['name'], variant['lang']), 'debug')
            continue
        try:
            info = extract(scraper.http, source['name'], embed, page_url)
        except ExtractionError as exc:
            errors.append(str(exc))
            log('extraction failed: %s' % exc, 'error')
            continue
        seen.add(key)
        options.extend(language_options(variant, info))
    if not options:
        raise ExtractionError('; '.join(errors) or 'no playable variant')
    return options


# --------------------------------------------------------------------------
# Playback properties (pure, testable outside Kodi)
# --------------------------------------------------------------------------
def header_string(headers):
    """'k=urlencoded_v&...' as expected by ISA *_headers and the '|' URL suffix."""
    return '&'.join('%s=%s' % (k, quote(v, safe='')) for k, v in headers.items())


def playback_properties(info, isa=True, kodi=21):
    """(path, mimetype, {property: value}) for the ListItem of a resolved stream."""
    hdr = header_string(info['headers'])
    if info['type'] == 'hls' and isa:
        props = {
            'inputstream': 'inputstream.adaptive',
            'inputstream.adaptive.manifest_headers': hdr,   # playlists (Kodi 20+)
            'inputstream.adaptive.stream_headers': hdr,     # segments (Kodi 21: segments only)
        }
        if kodi < 22:
            # Kodi 21 auto-detects HLS from the content-type and only logs a deprecation
            # warning for this property; it is removed in Kodi 22.
            props['inputstream.adaptive.manifest_type'] = 'hls'
        return info['url'], 'application/vnd.apple.mpegurl', props
    mime = 'application/vnd.apple.mpegurl' if info['type'] == 'hls' else 'video/mp4'
    return '%s|%s' % (info['url'], hdr), mime, {}


def _safe_name(text):
    text = text.replace('\u0131', 'i').replace('\u0130', 'I')
    try:
        import unicodedata
        text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    except Exception:
        pass
    return re.sub(r'[^A-Za-z0-9 ]+', ' ', text).strip() or 'Subtitle'


def prepare_subtitles(http, info, attach_all=True):
    """Download the Turkish/English tracks and save them as '<label>.<lang>[.forced].vtt'.

    Kodi takes the language and the forced flag of external subtitles from the
    file name; the site's own names carry neither. Other languages are passed
    as remote URLs (Close file names contain -fr-, -pt-, ... which Kodi parses).
    Returns [{'path', 'lang', 'forced', 'label', 'url'}] in setSubtitles() order."""
    folder = temp_dir('subtitles')
    for name in os.listdir(folder):
        try:
            os.remove(os.path.join(folder, name))
        except OSError:
            pass
    order = {'tr': 0, 'en': 1}
    tracks = sorted(info['subtitles'], key=lambda s: (order.get(s['lang'], 2), s['forced']))
    prepared, used = [], set()
    for sub in tracks:
        entry = dict(sub)
        if sub['lang'] in order:
            base = '%s.%s%s' % (_safe_name(sub['label'] or sub['lang']), sub['lang'],
                                '.forced' if sub['forced'] else '')
            fname, n = base + '.vtt', 2
            while fname in used:
                fname, n = '%s %d.vtt' % (base, n), n + 1
            used.add(fname)
            path = os.path.join(folder, fname)
            try:
                data = http.get(sub['url'], referer=info['embed']).content
                with open(path, 'wb') as fh:
                    fh.write(data)
                entry['path'] = path
            except (HttpError, IOError, OSError) as exc:
                log('subtitle download failed (%s): %s' % (sub['url'], exc), 'warning')
                entry['path'] = sub['url']
        elif attach_all:
            entry['path'] = sub['url']
        else:
            continue
        prepared.append(entry)
    return prepared


# --------------------------------------------------------------------------
# Kodi: ListItem, setResolvedUrl, track switching
# --------------------------------------------------------------------------
def _isa_available():
    return xbmc.getCondVisibility('System.AddonIsEnabled(inputstream.adaptive)')


def build_listitem(info, meta, subtitles):
    isa = _isa_available()
    if not isa:
        log('inputstream.adaptive is not enabled - falling back to Kodi\'s own HLS player', 'warning')
    path, mime, props = playback_properties(info, isa=isa, kodi=kodi_major())
    li = xbmcgui.ListItem(path=path, offscreen=True)
    li.setMimeType(mime)
    li.setContentLookup(False)
    for key, value in props.items():
        li.setProperty(key, value)
    if subtitles:
        li.setSubtitles([s['path'] for s in subtitles])
    tag = li.getVideoInfoTag()
    tag.setTitle(meta.get('episode_title') or meta.get('title') or '')
    tag.setPlot(meta.get('plot') or '')
    if meta.get('year'):
        tag.setYear(int(meta['year']))
    if meta.get('genres'):
        tag.setGenres(meta['genres'])
    if meta.get('mediatype') == 'episode':
        tag.setMediaType('episode')
        tag.setTvShowTitle(meta.get('tvshowtitle') or '')
        if meta.get('season'):
            tag.setSeason(int(meta['season']))
            tag.setEpisode(int(meta['episode']))
    else:
        tag.setMediaType('movie')
    if meta.get('image'):
        li.setArt({'poster': meta['image'], 'thumb': meta['image']})
    return li


def _rpc(method, params=None):
    request = {'jsonrpc': '2.0', 'id': 1, 'method': method}
    if params:
        request['params'] = params
    try:
        return json.loads(xbmc.executeJSONRPC(json.dumps(request))).get('result')
    except (ValueError, TypeError):
        return None


def _player_streams():
    """JSON-RPC gives name AND language (+ isforced/isoriginal); the Python API only
    returns the language, which ISA sets to "unk" for Close's tracks."""
    for p in _rpc('Player.GetActivePlayers') or []:
        if p.get('type') == 'video':
            return _rpc('Player.GetProperties', {'playerid': p['playerid'], 'properties': [
                'audiostreams', 'subtitles', 'currentaudiostream', 'currentsubtitle', 'subtitleenabled']}) or {}
    return {}


def _is_turkish(stream):
    return (stream.get('language') or '').lower() in ('tur', 'tr') or bool(TURKISH_AUDIO.search(stream.get('name') or ''))


def choose_audio(streams, want):
    """Index of the audio stream for want='turkish'|'original' (None if absent)."""
    if want == 'turkish':
        matches = [s for s in streams if _is_turkish(s)]
    else:
        matches = ([s for s in streams if s.get('isoriginal') and not _is_turkish(s)]
                   or [s for s in streams if re.search(r'orig|orij', s.get('name') or '', re.I)]
                   or [s for s in streams if not _is_turkish(s)])
    return matches[0]['index'] if matches else None


def choose_subtitle(streams, wanted, prepared):
    """Index of the Kodi subtitle stream for one prepared track (None if absent)."""
    codes = {wanted['lang'], ISO2.get(wanted['lang'], wanted['lang'])}
    same_lang = [s for s in streams if (s.get('language') or '').lower() in codes]
    exact = [s for s in same_lang if bool(s.get('isforced')) == wanted['forced']]
    if exact:
        return exact[0]['index']
    if same_lang and not wanted['forced']:
        return same_lang[0]['index']
    label = _safe_name(wanted['label']).lower()
    named = [s for s in streams if label and label in (s.get('name') or '').lower()]
    if named:
        return named[0]['index']
    # External subtitles are appended after the stream's own tracks, in setSubtitles() order.
    try:
        pos = [p['url'] for p in prepared].index(wanted['url'])
    except ValueError:
        return None
    index = len(streams) - len(prepared) + pos
    return index if 0 <= index < len(streams) else None


if IN_KODI:
    class TrackSelector(xbmc.Player):
        """Selects the audio/subtitle stream of the chosen language option as
        soon as Kodi reports onAVStarted (dual streams carry both languages)."""

        def __init__(self):
            xbmc.Player.__init__(self)
            self.started = False
            self.ended = False

        def onAVStarted(self):
            self.started = True

        def onPlayBackStopped(self):
            self.ended = True

        def onPlayBackEnded(self):
            self.ended = True

        def onPlayBackError(self):
            self.ended = True

        def wait_for_start(self, timeout=90):
            monitor = xbmc.Monitor()
            deadline = time.time() + timeout
            while not monitor.abortRequested():
                if self.started:
                    return True
                if self.ended or time.time() > deadline:
                    return False
                monitor.waitForAbort(0.25)
            return False

        def apply(self, option, prepared):
            streams = _player_streams()
            if not streams:  # JSON-RPC unavailable: fall back to the Python API
                streams = {'audiostreams': [{'index': i, 'language': n, 'name': n}
                                            for i, n in enumerate(self.getAvailableAudioStreams())],
                           'subtitles': [{'index': i, 'language': n, 'name': n}
                                         for i, n in enumerate(self.getAvailableSubtitleStreams())]}
            audio = streams.get('audiostreams') or []
            log('audio streams: %s' % [(a.get('index'), a.get('name'), a.get('language')) for a in audio], 'debug')
            if option['audio'] and len(audio) > 1:
                index = choose_audio(audio, option['audio'])
                if index is None:
                    log('no %s audio stream among %s' % (option['audio'], audio), 'warning')
                    _notify(localize(30053 if option['audio'] == 'turkish' else 30054))
                elif index != (streams.get('currentaudiostream') or {}).get('index'):
                    log('selecting audio stream %d (%s)' % (index, option['audio']))
                    self.setAudioStream(index)

            subs = streams.get('subtitles') or []
            log('subtitle streams: %s' % [(s.get('index'), s.get('name'), s.get('language'), s.get('isforced'))
                                          for s in subs], 'debug')
            wanted = option['subtitle']
            if wanted is None:
                self.showSubtitles(False)
                return
            index = choose_subtitle(subs, wanted, prepared)
            if index is None:
                log('subtitle %s not found among %s' % (wanted, subs), 'warning')
                _notify(localize(30055))
                return
            log('selecting subtitle stream %d (%s)' % (index, wanted['label']))
            self.setSubtitleStream(index)
            self.showSubtitles(True)


def _notify(message, icon=None):
    xbmcgui.Dialog().notification(addon().getAddonInfo('name'), message, icon or xbmcgui.NOTIFICATION_WARNING, 4000)


def play(handle, http, option, meta):
    """Resolve the chosen option to Kodi and switch tracks once playback runs."""
    info = option['stream']
    prepared = prepare_subtitles(http, info, attach_all=setting_bool('all_subtitles', True))
    selector = TrackSelector()
    li = build_listitem(info, meta, prepared)
    log('playing %s [%s / %s] %s' % (meta.get('title'), info['source'], option['id'], info['url']))
    xbmcplugin.setResolvedUrl(handle, True, li)
    if not selector.wait_for_start():
        log('playback did not start (or was stopped) - no track selection', 'warning')
        return
    sleep(0.5)  # let ISA publish all streams
    try:
        selector.apply(option, prepared)
    except Exception as exc:  # never break playback because of track selection
        log('track selection failed: %r' % (exc,), 'error')
