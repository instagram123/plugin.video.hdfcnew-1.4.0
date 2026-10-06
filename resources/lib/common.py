# -*- coding: utf-8 -*-
"""Shared helpers: Kodi glue (log, settings, strings, paths), HTTP client and a
small JSON store.

Kodi modules are optional so scraper.py / player.py can also run outside Kodi
(see tools/test_sources.py).
"""
import json
import os
import sys
import tempfile
import time

try:
    from urllib.parse import urljoin
except ImportError:  # pragma: no cover - Kodi 21 is Python 3
    from urlparse import urljoin

import requests

try:
    import xbmc
    import xbmcaddon
    import xbmcvfs
except ImportError:  # running outside Kodi
    xbmc = xbmcaddon = xbmcvfs = None

ADDON_ID = 'plugin.video.hdfcnew'
DEFAULT_BASE_URL = 'https://www.hdfilmcehennemi.nl'
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36')
TIMEOUT = 20

IN_KODI = xbmc is not None
_addon = None
_debug = None


def addon():
    global _addon
    if _addon is None and IN_KODI:
        _addon = xbmcaddon.Addon(ADDON_ID)
    return _addon


# --------------------------------------------------------------------------
# Logging / settings / strings
# --------------------------------------------------------------------------
def log(msg, level='info'):
    """xbmc.log with an add-on prefix; 'debug' messages become INFO when the
    "debug logging" setting is on, so they show up without Kodi's debug log."""
    global _debug
    text = '[%s] %s' % (ADDON_ID, msg)
    if not IN_KODI:
        if level != 'debug' or os.environ.get('HDFC_DEBUG'):
            sys.stderr.write(text + '\n')
        return
    if _debug is None:
        _debug = setting_bool('debug')
    levels = {'debug': xbmc.LOGINFO if _debug else xbmc.LOGDEBUG, 'info': xbmc.LOGINFO,
              'warning': xbmc.LOGWARNING, 'error': xbmc.LOGERROR}
    xbmc.log(text, levels.get(level, xbmc.LOGINFO))


def setting(key, default=''):
    a = addon()
    if a is None:
        return default
    try:
        value = a.getSetting(key)
    except Exception:  # unknown id / corrupt settings
        return default
    return value if value != '' else default


def setting_int(key, default=0):
    try:
        return int(setting(key, str(default)))
    except ValueError:
        return default


def setting_bool(key, default=False):
    return setting(key, 'true' if default else 'false').lower() == 'true'


def localize(string_id, fallback=''):
    a = addon()
    text = a.getLocalizedString(string_id) if a is not None else ''
    return text or fallback or str(string_id)


def kodi_major():
    if not IN_KODI:
        return 21
    try:
        return int(xbmc.getInfoLabel('System.BuildVersion').split('.')[0])
    except (ValueError, IndexError):
        return 21


def base_url():
    return (setting('base_url', DEFAULT_BASE_URL) or DEFAULT_BASE_URL).strip().rstrip('/')


def _ensure_dir(path):
    if not os.path.isdir(path):
        os.makedirs(path)
    return path


def profile_dir():
    if IN_KODI:
        return _ensure_dir(xbmcvfs.translatePath('special://profile/addon_data/%s/' % ADDON_ID))
    return _ensure_dir(os.path.join(tempfile.gettempdir(), ADDON_ID, 'profile'))


def temp_dir(*parts):
    root = xbmcvfs.translatePath('special://temp/%s/' % ADDON_ID) if IN_KODI \
        else os.path.join(tempfile.gettempdir(), ADDON_ID, 'temp')
    return _ensure_dir(os.path.join(root, *parts))


def sleep(seconds):
    """Abort-aware sleep inside Kodi."""
    if IN_KODI:
        return xbmc.Monitor().waitForAbort(seconds)
    time.sleep(seconds)
    return False


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class HttpError(Exception):
    def __init__(self, message, status=None):
        Exception.__init__(self, message)
        self.status = status


class HttpClient(object):
    """requests.Session with browser-like headers.

    * ``ajax=True`` adds ``X-Requested-With: fetch`` - the site answers 403/404
      to its JSON endpoints (/video/<id>/, /load/page/..., /search) without it.
    * HTTP 429 (the site rate-limits bursts) and network errors are retried once.
    """

    def __init__(self, base=None, user_agent=USER_AGENT):
        self.base_url = (base or DEFAULT_BASE_URL).rstrip('/')
        self.user_agent = user_agent
        self.session = requests.Session()

    def absolute(self, url):
        return urljoin(self.base_url + '/', url or '')

    def get(self, url, referer=None, ajax=False, headers=None):
        url = self.absolute(url)
        hdrs = {
            'User-Agent': self.user_agent,
            'Accept': '*/*' if ajax else 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'tr-TR,tr;q=0.9,en-US;q=0.6,en;q=0.5',
        }
        if referer:
            hdrs['Referer'] = referer
        if ajax:
            hdrs['X-Requested-With'] = 'fetch'
        if headers:
            hdrs.update(headers)
        resp = None
        for attempt in (1, 2):
            try:
                resp = self.session.get(url, headers=hdrs, timeout=TIMEOUT)
            except requests.RequestException as exc:
                if attempt == 2:
                    raise HttpError('%s: %s' % (url, exc))
                log('network error on %s (%s), retrying' % (url, exc), 'warning')
                if sleep(1):
                    raise HttpError('aborted')
                continue
            if resp.status_code == 429 and attempt == 1:
                try:
                    wait = min(int(resp.headers.get('Retry-After') or 3), 8)
                except ValueError:
                    wait = 3
                log('HTTP 429 on %s, waiting %ds' % (url, wait), 'warning')
                if sleep(wait):
                    raise HttpError('aborted')
                continue
            break
        if resp.status_code >= 400:
            raise HttpError('HTTP %d for %s' % (resp.status_code, url), resp.status_code)
        return resp

    def get_text(self, url, **kwargs):
        return self.get(url, **kwargs).content.decode('utf-8', 'replace')

    def get_json(self, url, **kwargs):
        text = self.get_text(url, **kwargs)
        try:
            return json.loads(text)
        except ValueError:
            raise HttpError('invalid JSON from %s' % self.absolute(url))


def make_http():
    return HttpClient(base_url())


# --------------------------------------------------------------------------
# Tiny persistent store (category cache, search history)
# --------------------------------------------------------------------------
class JsonStore(object):
    def __init__(self, name):
        self.path = os.path.join(profile_dir(), name + '.json')

    def load(self, default=None):
        try:
            with open(self.path, 'r', encoding='utf-8') as fh:
                return json.load(fh)
        except (IOError, OSError, ValueError):
            return default

    def save(self, data):
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, self.path)

    def clear(self):
        try:
            os.remove(self.path)
        except OSError:
            pass


def cache_get(key, max_age):
    entry = (JsonStore('cache').load({}) or {}).get(key)
    if entry and time.time() - entry.get('time', 0) < max_age:
        return entry.get('value')
    return None


def cache_set(key, value):
    store = JsonStore('cache')
    data = store.load({}) or {}
    data[key] = {'time': time.time(), 'value': value}
    store.save(data)
