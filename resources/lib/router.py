# -*- coding: utf-8 -*-
"""plugin:// URL routing: menus, listings, search, series and the
source -> language playback flow."""
import shutil
import sys

try:
    from urllib.parse import parse_qsl, urlencode
except ImportError:  # pragma: no cover
    from urllib import urlencode
    from urlparse import parse_qsl

import xbmc
import xbmcgui
import xbmcplugin

from . import actors, player, ui
from .common import HttpError, JsonStore, addon, localize, log, make_http, setting_int, temp_dir
from .scraper import Scraper

SEARCH_HISTORY_SIZE = 20


class Router(object):
    def __init__(self, base, handle):
        self.base = base
        self.handle = handle
        self.http = make_http()
        self.scraper = Scraper(self.http)

    def url(self, **params):
        return '%s?%s' % (self.base, urlencode(dict((k, v) for k, v in params.items() if v not in (None, ''))))

    def dispatch(self, params):
        action = params.get('action', '')
        handler = getattr(self, 'do_' + action, None) if action else self.do_root
        if handler is None:
            log('unknown action %r' % action, 'error')
            return
        log('action=%s params=%s' % (action or 'root', params), 'debug')
        try:
            handler(params)
        except HttpError as exc:
            log('%s failed: %s' % (action or 'root', exc), 'error')
            ui.notify(localize(30056), 'error')
            if action != 'play':
                ui.end_directory(self.handle, succeeded=False, cache=False)

    # -- menus -------------------------------------------------------------
    def do_root(self, params):
        if setting_int('ui_mode') == 1:
            return self.do_classic(params)
        # Full-screen interface: a tiny listing to come back to, and the window itself.
        from . import gui
        h = self.handle
        li = xbmcgui.ListItem(localize(30064), offscreen=True)
        li.setArt({'icon': addon().getAddonInfo('icon'), 'thumb': addon().getAddonInfo('icon')})
        xbmcplugin.addDirectoryItem(h, self.url(action='gui'), li, isFolder=False)
        ui.add_folder(h, self.url(action='classic'), localize(30078))
        ui.end_directory(h, content='', cache=True)
        if not gui.is_open() and not gui.recently_closed():
            xbmc.executebuiltin('RunPlugin(%s)' % self.url(action='gui'))

    def do_gui(self, params):
        from . import gui
        gui.open_browser()

    def do_classic(self, params):
        try:
            cats = self.scraper.categories()
        except HttpError as exc:
            log('categories unavailable: %s' % exc, 'error')
            cats = {}
        h = self.handle
        ui.add_folder(h, self.url(action='search_menu'), localize(30010))
        ui.add_folder(h, self.url(action='listing', url=self.http.base_url + '/'), localize(30012))
        ui.add_folder(h, self.url(action='listing', url=cats.get('series') or self.http.absolute('/yabancidiziizle-5/')),
                      localize(30013))
        for lang in cats.get('languages') or []:
            ui.add_folder(h, self.url(action='listing', url=lang['url']), lang['title'])
        for group, string_id in (('genres', 30015), ('categories', 30016), ('years', 30017), ('countries', 30018)):
            if cats.get(group):
                ui.add_folder(h, self.url(action='categories', group=group), localize(string_id))
        if actors.load_actors(self.http.base_url):
            ui.add_folder(h, self.url(action='actors'), localize(30093))
        li = xbmcgui.ListItem(localize(30019), offscreen=True)
        li.setArt({'icon': 'DefaultAddonProgram.png'})
        xbmcplugin.addDirectoryItem(h, self.url(action='settings'), li, isFolder=False)
        ui.end_directory(h, content='', cache=False)

    def do_categories(self, params):
        for cat in self.scraper.categories().get(params.get('group'), []):
            ui.add_folder(self.handle, self.url(action='listing', url=cat['url']), cat['title'])
        ui.end_directory(self.handle, content='')

    def do_actors(self, params):
        for actor in actors.load_actors(self.http.base_url):
            ui.add_folder(self.handle, self.url(action='listing', url=actor['url']), actor['name'])
        ui.end_directory(self.handle, content='')

    # -- listings ----------------------------------------------------------
    def _add_items(self, items):
        for item in items:
            label = item['title'] + (' (%s)' % item['year'] if item.get('year') else '')
            if item.get('series'):
                ui.add_folder(self.handle, self.url(action='series', url=item['url']), label,
                              item.get('image'), item)
            else:
                ui.add_playable(self.handle, self.url(action='play', url=item['url']), item, label=label)
        kinds = set(bool(i.get('series')) for i in items)
        return 'tvshows' if kinds == {True} else 'movies' if kinds == {False} else 'videos'

    def do_listing(self, params):
        page = int(params.get('page') or 1)
        data = self.scraper.listing(params['url'], page, params.get('page_action'), params.get('pages'))
        content = self._add_items(data['items'])
        if data['items'] and data['action'] and (not data['pages'] or page < data['pages']):
            label = localize(30020) % (page + 1, data['pages']) if data['pages'] else localize(30023)
            ui.add_folder(self.handle, self.url(action='listing', url=params['url'], page=page + 1,
                                                page_action=data['action'], pages=data['pages']), label)
        ui.end_directory(self.handle, content)

    # -- search ------------------------------------------------------------
    def _history(self):
        return JsonStore('search_history')

    def do_search_menu(self, params):
        ui.add_folder(self.handle, self.url(action='search_new'), '[B]%s[/B]' % localize(30011))
        for query in self._history().load([]) or []:
            ui.add_folder(self.handle, self.url(action='search', q=query), query, context=[
                (localize(30024), 'RunPlugin(%s)' % self.url(action='search_remove', q=query))])
        if self._history().load([]):
            li = xbmcgui.ListItem(localize(30022), offscreen=True)
            xbmcplugin.addDirectoryItem(self.handle, self.url(action='search_clear'), li, isFolder=False)
        ui.end_directory(self.handle, content='', cache=False)

    def do_search_new(self, params):
        query = ui.keyboard(localize(30010))
        # Never list results from this URL: going "back" would pop the keyboard again.
        ui.end_directory(self.handle, content='', succeeded=False, cache=False)
        if query:
            history = [q for q in (self._history().load([]) or []) if q.lower() != query.lower()]
            self._history().save(([query] + history)[:SEARCH_HISTORY_SIZE])
            xbmc.executebuiltin('Container.Update(%s)' % self.url(action='search', q=query))

    def do_search(self, params):
        items = self.scraper.search(params.get('q', ''))
        if not items:
            ui.notify(localize(30057))
        content = self._add_items(items)
        ui.end_directory(self.handle, content, cache=False)

    def do_search_remove(self, params):
        store = self._history()
        store.save([q for q in (store.load([]) or []) if q != params.get('q')])
        xbmc.executebuiltin('Container.Refresh')

    def do_search_clear(self, params):
        self._history().clear()
        xbmc.executebuiltin('Container.Refresh')

    # -- series ------------------------------------------------------------
    def do_series(self, params):
        data = self.scraper.series(params['url'])
        seasons = data['seasons']
        if len(seasons) == 1:
            return self._episodes(data, next(iter(seasons)))
        info = data['info']
        for number in sorted(seasons):
            ui.add_folder(self.handle, self.url(action='season', url=params['url'], season=number),
                          localize(30021) % number, info.get('image'), info)
        if not seasons:
            ui.notify(localize(30050), 'warning')
        ui.end_directory(self.handle, 'tvshows')

    def do_season(self, params):
        self._episodes(self.scraper.series(params['url']), int(params['season']))

    def _episodes(self, data, season):
        info = data['info']
        for ep in data['seasons'].get(season, []):
            item = dict(ep, image=info.get('image'), plot=info.get('plot'))
            label = '%dx%02d  %s' % (ep['season'], ep['episode'], ep['title'])
            ui.add_playable(self.handle, self.url(action='play', url=ep['url']), item, 'episode', label)
        ui.end_directory(self.handle, 'episodes')

    # -- playback ----------------------------------------------------------
    def _cancel(self):
        xbmcplugin.setResolvedUrl(self.handle, False, xbmcgui.ListItem(offscreen=True))

    def do_play(self, params):
        page_url = params['url']
        try:
            html = self.scraper.page(page_url)
        except HttpError as exc:
            log('cannot load %s: %s' % (page_url, exc), 'error')
            ui.notify(localize(30056), 'error')
            return self._cancel()
        sources = self.scraper.sources(html)
        if not sources:
            log('no player sources on %s' % page_url, 'warning')
            ui.notify(localize(30050), 'warning')
            return self._cancel()
        meta = self.scraper.title_info(html, page_url)
        log('sources on %s: %s' % (page_url, [(s['label'], [v['lang'] for v in s['variants']]) for s in sources]))

        preferred_source = ui.PREFERRED_SOURCE.get(setting_int('preferred_source'))
        preferred_language = ui.PREFERRED_LANGUAGE.get(setting_int('preferred_language'))
        failed, auto = set(), True
        while True:
            source = ui.select_source(sources, failed, preferred_source if auto else None)
            if source is None:
                return self._cancel()
            try:
                options = player.resolve_source(self.scraper, page_url, source)
            except player.ExtractionError as exc:
                log('%s failed on %s: %s' % (source['label'], page_url, exc), 'error')
                failed.add(source['name'])
                auto = False
                others = [s['label'] for s in sources if s['name'] not in failed]
                if others:
                    ui.notify(localize(30051) % (source['label'], others[0]), 'error')
                    continue
                ui.notify(localize(30052) % source['label'], 'error')
                if len(sources) == 1:
                    return self._cancel()
                continue
            log('%s options: %s' % (source['label'], [(o['id'], o['tab']) for o in options]))
            option = ui.select_language(options, preferred_language if auto else None, source['label'])
            if option is None:
                if len(sources) > 1:
                    auto = False  # back to the source dialog
                    continue
                return self._cancel()
            break
        player.play(self.handle, self.http, option, meta)

    # -- misc --------------------------------------------------------------
    def do_settings(self, params):
        addon().openSettings()

    def do_clear_cache(self, params):
        JsonStore('cache').clear()
        shutil.rmtree(temp_dir(), ignore_errors=True)
        ui.notify(localize(30058))


def run(argv=None):
    argv = argv or sys.argv
    params = dict(parse_qsl(argv[2][1:] if len(argv) > 2 else ''))
    Router(argv[0], int(argv[1])).dispatch(params)
