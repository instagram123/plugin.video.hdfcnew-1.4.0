# -*- coding: utf-8 -*-
"""Favorite actors: a name + the site's /oyuncu/<slug>/ page.

An actor page is paginated exactly like a category (data-page-action="cast/<slug>"
-> /load/page/<n>/cast/<slug>/), so the entries are listed with Scraper.listing().

Two files are merged (no Kodi imports here, usable from tools/):
  1. resources/data/actors.json                     shipped with the add-on
  2. <addon profile>/actors.json                    yours; survives add-on updates
Each is a JSON list of {"name": "Jason Statham", "url": "/oyuncu/jason-statham/"}
(a full URL, or {"name":..., "slug": "jason-statham"}, also work). The shipped
list comes first; a profile entry with the same slug replaces the shipped one in
place, new profile entries follow. A missing, empty or invalid file is ignored
(and logged), a bad entry is skipped.
"""
import json
import os
import re

try:
    from urllib.parse import urljoin, urlparse
except ImportError:  # pragma: no cover
    from urlparse import urljoin, urlparse

from .common import log, profile_dir

SHIPPED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'actors.json')
PROFILE_NAME = 'actors.json'
ACTOR_PATH = re.compile(r'^/oyuncu/([^/]+)/?$')


def _read_list(path, label):
    try:
        with open(path, 'r', encoding='utf-8-sig') as fh:
            content = fh.read()
    except (IOError, OSError):
        log('actors: no %s file (%s)' % (label, path), 'debug')
        return []
    if not content.strip():
        log('actors: %s file is empty, ignored' % label, 'warning')
        return []
    try:
        data = json.loads(content)
    except ValueError as exc:
        log('actors: %s file is not valid JSON, ignored (%s)' % (label, exc), 'warning')
        return []
    if isinstance(data, dict):
        data = data.get('actors')
    if not isinstance(data, list):
        log('actors: %s file must contain a list, ignored' % label, 'warning')
        return []
    return data


def _entry(raw, base):
    """{'name', 'slug', 'url'} or None. The URL is rebuilt on the current site address."""
    if not isinstance(raw, dict):
        return None
    url = str(raw.get('url') or '').strip()
    if not url and raw.get('slug'):
        url = '/oyuncu/%s/' % str(raw['slug']).strip().strip('/')
    m = ACTOR_PATH.match(urlparse(url).path or '')
    if not m:
        log('actors: skipped entry without a /oyuncu/<slug>/ url: %r' % (raw,), 'warning')
        return None
    slug = m.group(1)
    name = str(raw.get('name') or '').strip() or slug.replace('-', ' ').title()
    return {'name': name, 'slug': slug, 'url': urljoin(base.rstrip('/') + '/', '/oyuncu/%s/' % slug)}


def load_actors(base, profile_path=None, shipped_path=None):
    """Merged actor list for the site address `base`. Never raises."""
    merged = {}  # slug -> entry (dicts keep insertion order)
    try:
        if profile_path is None:
            profile_path = os.path.join(profile_dir(), PROFILE_NAME)
        sources = ((shipped_path or SHIPPED_FILE, 'shipped'), (profile_path, 'profile'))
        for path, label in sources:
            for raw in _read_list(path, label):
                entry = _entry(raw, base)
                if entry:
                    merged[entry['slug']] = entry
    except Exception as exc:  # defensive: the sidebar must always build
        log('actors: could not load (%s)' % exc, 'error')
    return list(merged.values())
