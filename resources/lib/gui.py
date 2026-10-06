# -*- coding: utf-8 -*-
"""Full-screen browser (UI only).

Layout: resources/skins/Default/1080i/hdfcnew_browser.xml
Data:   the existing Scraper functions (categories, listing, search, series) and
        scraper.fetch_movie_details for the details view.
Play:   the existing play route plugin://plugin.video.hdfcnew/?action=play&url=...
        via PlayMedia, so the source/language dialogs, extraction and track
        switching are exactly the same as in the classic lists.
Details view: a movie / series opens a details page inside this window (properties
        d_*, shown instead of the browser while details_open is set); Play / Episodes
        start the old flow. Episodes still play directly.

Kodi re-creates the controls of a Python window every time it is shown again
(e.g. after fullscreen video), so all state lives here and onInit re-renders it.
"""
import threading
import time
from contextlib import contextmanager

try:
    from urllib.parse import urlencode
except ImportError:  # pragma: no cover
    from urllib import urlencode

import xbmc
import xbmcgui

from . import actors, ui
from .common import ADDON_ID, HttpError, addon, localize, log, make_http, setting_bool
from .scraper import Scraper, fetch_movie_details

XML_FILE = 'hdfcnew_browser.xml'
PLUGIN_ROOT = 'plugin://%s/' % ADDON_ID
PROP_OPEN = ADDON_ID + '.gui_open'
PROP_CLOSED = ADDON_ID + '.gui_closed'

SEARCH, SETTINGS, CATEGORIES, GRID, BUTTONS, PREV, NEXT = 200, 210, 300, 500, 600, 601, 602
DETAILS_BUTTONS, DETAILS_MAIN, DETAILS_TRAILER, DETAILS_BACK, DETAILS_SCROLL = 700, 701, 702, 703, 710
YOUTUBE_PLAY = 'plugin://plugin.video.youtube/play/?video_id=%s'
BACK_ACTIONS = (9, 10, 92)  # ACTION_PARENT_DIR, ACTION_PREVIOUS_MENU, ACTION_NAV_BACK
TITLE_CHARS = 44            # ~2 lines of font12 in a 220px tile


def _short(text, limit=TITLE_CHARS):
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(' ', 1)[0].rstrip(' -:,')
    return (cut or text[:limit]) + '…'


def _minutes_label(minutes):
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return ''
    if minutes <= 0:
        return ''
    if minutes >= 60:
        return localize(30084) % (minutes // 60, minutes % 60)
    return localize(30083) % minutes


def details_properties(item, data, loading):
    """Window properties of the details view: the grid item is shown at once, the fetched
    details (scraper.fetch_movie_details) fill in when they arrive."""
    data = data or {}
    year = data.get('year') or str(item.get('year') or '')
    rating = data.get('rating') or str(item.get('rating') or '')
    meta = [year, _minutes_label(data.get('duration')),
            '[COLOR FFF5A623]IMDb %s[/COLOR]' % rating if rating else '', ', '.join(data.get('genres') or [])]
    title = data.get('title') or item.get('title') or ''
    original = data.get('original_title') or ''
    series = bool(item.get('series') or data.get('series'))
    poster = data.get('poster') or item.get('image') or ''
    sources = ', '.join(x for x in (', '.join(data.get('sources') or []), ', '.join(data.get('languages') or [])) if x)
    return {
        'd_title': title,
        'd_original': original if original != title else '',
        'd_poster': poster,
        'd_backdrop': data.get('backdrop') or poster,
        'd_meta': '   |   '.join(p for p in meta if p),
        'd_plot': data.get('plot') or '',
        'd_director': data.get('director') or '',
        'd_cast': ', '.join(data.get('cast') or []),
        'd_sources': sources,
        'd_trailer': 'true' if data.get('trailer') else '',
        'd_main_label': localize(30080 if series else 30079),
        'd_loading': 'true' if loading else '',
    }


def _home():
    return xbmcgui.Window(10000)


def recently_closed(seconds=4):
    """True right after the window was closed: Kodi then re-shows the plugin
    root it was started from, which must not open the window again."""
    try:
        return time.time() - float(_home().getProperty(PROP_CLOSED) or 0) < seconds
    except ValueError:
        return False


def is_open():
    return _home().getProperty(PROP_OPEN) == 'true'


class Browser(xbmcgui.WindowXML):

    def __init__(self, *args, **kwargs):
        self.scraper = Scraper(make_http())
        self.entries = []        # sidebar model: entries and group headers with children
        self.rows = []           # sidebar rows currently shown (flattened)
        self.expanded = set(['group:actors'])  # expanded group keys (the actors start open)
        self.active_key = ''     # highlighted sidebar entry ('search' for search results)
        self.stack = []          # views; stack[-1] is shown
        self.last_query = ''
        self.restore = None      # (control id, grid position) to restore in onInit
        self.started = False
        self._details_thread = None

    # ------------------------------------------------------------------ init
    def onInit(self):
        self.sidebar = self.getControl(CATEGORIES)
        self.grid = self.getControl(GRID)
        self.setProperty('addon_name', addon().getAddonInfo('name'))
        if not self.started:
            self.started = True
            self._load_entries()
            self._render_sidebar(position=0)
            if self.entries:
                self._open_entry(self.entries[0])
            self.setFocusId(CATEGORIES)
            return
        # back from fullscreen video (or another window): rebuild from memory, no network
        self._render_sidebar()
        if self.stack:
            self._render(self.stack[-1])
        control, position = self.restore or (CATEGORIES, None)
        if control == GRID and self.grid.size():
            self.grid.selectItem(min(position or 0, self.grid.size() - 1))
        self._focus(control)

    # ------------------------------------------------------------- sidebar
    def _load_entries(self):
        try:
            with self._busy():
                cats = self.scraper.categories()
        except HttpError as exc:
            log('categories unavailable: %s' % exc, 'error')
            ui.notify(localize(30056), 'error')
            cats = {}
        base = self.scraper.base
        entries = [
            {'key': 'home', 'label': localize(30012), 'url': base + '/'},
            {'key': 'series', 'label': localize(30013),
             'url': cats.get('series') or self.scraper.absolute('/yabancidiziizle-5/')},
        ]
        for cat in cats.get('languages') or []:
            entries.append({'key': cat['url'], 'label': cat['title'], 'url': cat['url']})
        for group, string_id in (('genres', 30015), ('years', 30017), ('categories', 30016), ('countries', 30018)):
            children = [{'key': c['url'], 'label': c['title'], 'url': c['url'], 'child': True}
                        for c in cats.get(group) or []]
            if children:
                entries.append({'key': 'group:' + group, 'label': localize(string_id), 'header': True,
                                'children': children})
        favorites = [{'key': a['url'], 'label': a['name'], 'url': a['url'], 'child': True}
                     for a in actors.load_actors(base)]
        if favorites:
            entries.append({'key': 'group:actors', 'label': localize(30093), 'header': True, 'children': favorites})
        self.entries = entries

    def _render_sidebar(self, position=None):
        if position is None:
            position = self.sidebar.getSelectedPosition()
        self.rows, items = [], []

        def add(entry, active):
            li = xbmcgui.ListItem(entry['label'], offscreen=True)
            if entry.get('header'):
                li.setProperty('header', 'true')
                li.setProperty('arrow', '-' if entry['key'] in self.expanded else '+')
            if entry.get('child'):
                li.setProperty('child', 'true')
            if active:
                li.setProperty('active', 'true')
            self.rows.append(entry)
            items.append(li)

        for entry in self.entries:
            if entry.get('header'):
                open_ = entry['key'] in self.expanded
                contains_active = any(c['key'] == self.active_key for c in entry['children'])
                add(entry, contains_active and not open_)
                if open_:
                    for child in entry['children']:
                        add(child, child['key'] == self.active_key)
            else:
                add(entry, entry['key'] == self.active_key)
        self.sidebar.reset()
        self.sidebar.addItems(items)
        if items and position is not None and position >= 0:
            self.sidebar.selectItem(min(position, len(items) - 1))
        self.setProperty('search_active', 'true' if self.active_key == 'search' else '')

    def _sidebar_click(self):
        position = self.sidebar.getSelectedPosition()
        if not 0 <= position < len(self.rows):
            return
        entry = self.rows[position]
        if entry.get('header'):
            self.expanded.symmetric_difference_update([entry['key']])
            self._render_sidebar(position)
            return
        self._open_entry(entry)

    # --------------------------------------------------------------- views
    def _open_entry(self, entry):
        view = {'kind': 'listing', 'title': entry['label'], 'url': entry['url'],
                'page': 1, 'pages': 0, 'action': '', 'items': [], 'focus': 0}
        if self._load_page(view, 1):
            self.active_key = entry['key']
            self.stack = [view]
            self._render_sidebar()
            self._render(view)

    def _load_page(self, view, page):
        """Existing pagination: Scraper.listing (page 1 = category HTML, page n = /load/page/n/<action>/)."""
        try:
            with self._busy():
                data = self.scraper.listing(view['url'], page, view['action'] if page > 1 else None,
                                            view['pages'] if page > 1 else None)
        except HttpError as exc:
            log('listing %s page %d failed: %s' % (view['url'], page, exc), 'error')
            ui.notify(localize(30056), 'error')
            return False
        view.update(items=data['items'], page=data['page'], pages=data['pages'], action=data['action'], focus=0)
        return True

    def _change_page(self, delta):
        view = self.stack[-1] if self.stack else None
        if not view or view['kind'] != 'listing':
            return
        if self._load_page(view, max(1, view['page'] + delta)):
            self._render(view)
            self.setFocusId(GRID)

    def _search(self):
        query = ui.keyboard(localize(30010), self.last_query)
        if not query:
            return
        self.last_query = query
        try:
            with self._busy():
                items = self.scraper.search(query)
        except HttpError as exc:
            log('search failed: %s' % exc, 'error')
            ui.notify(localize(30056), 'error')
            return
        view = {'kind': 'search', 'title': localize(30071) % query, 'items': items, 'focus': 0,
                'subtitle': localize(30070) % len(items)}
        self.active_key = 'search'
        self.stack = [view]
        self._render_sidebar()
        self._render(view)
        self.setFocusId(GRID if items else SEARCH)

    def _open_series(self, item):
        try:
            with self._busy():
                data = self.scraper.series(item['url'])
        except HttpError as exc:
            log('series %s failed: %s' % (item['url'], exc), 'error')
            ui.notify(localize(30056), 'error')
            return
        seasons = data['seasons']
        if not seasons:
            ui.notify(localize(30050), 'warning')
            return
        info = data['info']
        title = info.get('title') or item.get('title') or ''
        image = item.get('image') or info.get('image') or ''
        if len(seasons) == 1:
            number = next(iter(seasons))
            view = self._episodes_view(title, image, number, seasons[number])
        else:
            tiles = [{'kind': 'season', 'season': n, 'title': localize(30021) % n, 'image': image,
                      'badge': localize(30073) % len(seasons[n])} for n in sorted(seasons)]
            view = {'kind': 'seasons', 'title': title, 'items': tiles, 'focus': 0, 'seasons': seasons,
                    'image': image, 'subtitle': localize(30072) % len(seasons)}
        self._push(view)

    def _episodes_view(self, title, image, season, episodes):
        tiles = [{'kind': 'episode', 'url': ep['url'], 'title': ep['title'], 'image': image,
                  'badge': 'S%02dE%02d' % (ep['season'], ep['episode'])} for ep in episodes]
        return {'kind': 'episodes', 'title': '%s - %s' % (title, localize(30021) % season), 'items': tiles,
                'focus': 0, 'subtitle': localize(30073) % len(tiles)}

    # ------------------------------------------------------------- details
    def _open_details(self, item):
        """Details page of a movie / series: poster and title from the grid item at once, the rest
        (scraper.fetch_movie_details, cached 10 minutes) is loaded in a background thread."""
        view = {'kind': 'details', 'title': item.get('title') or '', 'items': [], 'focus': 0, 'item': item,
                'data': None, 'loading': True}
        self._push(view, DETAILS_MAIN)
        thread = threading.Thread(target=self._load_details, args=(view,))
        thread.daemon = True
        self._details_thread = thread
        thread.start()

    def _load_details(self, view):
        data = fetch_movie_details(view['item']['url'], self.scraper.http)
        view['data'], view['loading'] = data, False
        try:
            if self.stack and self.stack[-1] is view:  # still the visible view
                self._render_details(view)
                if data.get('error'):
                    ui.notify(localize(30089), 'warning')
        except Exception as exc:  # the window was closed in the meantime
            log('details view not updated: %s' % exc, 'debug')

    def _render_details(self, view):
        for key, value in details_properties(view['item'], view.get('data'), view.get('loading')).items():
            self.setProperty(key, value)
        self.setProperty('details_open', 'true')

    def _details_view(self):
        view = self.stack[-1] if self.stack else None
        return view if view and view['kind'] == 'details' else None

    def _details_main(self):
        """Play (movie) starts the old flow unchanged; Episodes (series) opens the old seasons list."""
        view = self._details_view()
        if not view:
            return
        item = view['item']
        if item.get('series') or (view.get('data') or {}).get('series'):
            self._open_series(item)
        else:
            self.restore = (DETAILS_MAIN, None)
            self._play_media(item['url'])

    def _details_trailer(self):
        view = self._details_view()
        video_id = (view.get('data') or {}).get('trailer') if view else ''
        if not video_id:
            return
        if not xbmc.getCondVisibility('System.AddonIsEnabled(plugin.video.youtube)'):
            ui.notify(localize(30090), 'warning')
            return
        self.restore = (DETAILS_TRAILER, None)
        xbmc.executebuiltin('PlayMedia(%s)' % (YOUTUBE_PLAY % video_id))

    def _focus(self, control_id):
        """setFocusId, retried while the control is still hidden: a group's visible condition is only
        re-evaluated on the next frame after a window property changed."""
        for _ in range(12):
            self.setFocusId(control_id)
            try:
                if self.getFocusId() == control_id:
                    return
            except RuntimeError:  # nothing focused yet
                pass
            xbmc.sleep(40)
        log('could not focus control %d' % control_id, 'debug')

    def _push(self, view, focus=GRID):
        if self.stack and self.stack[-1]['kind'] != 'details':
            self.stack[-1]['focus'] = max(0, self.grid.getSelectedPosition())
        self.stack.append(view)
        self._render(view)
        self._focus(focus)

    def _back(self):
        if len(self.stack) > 1:
            self.stack.pop()
            view = self.stack[-1]
            self._render(view)
            self._focus(DETAILS_MAIN if view['kind'] == 'details' else GRID)
        elif self.getFocusId() in (GRID, PREV, NEXT, BUTTONS):
            self.setFocusId(CATEGORIES)
        else:
            self.close()

    def _render(self, view):
        if view['kind'] == 'details':
            self._render_details(view)
            return
        self.clearProperty('details_open')
        items = view['items']
        self.grid.reset()
        self.grid.addItems([self._tile(item) for item in items])
        if items:
            self.grid.selectItem(min(view.get('focus', 0), len(items) - 1))
        paged = view['kind'] == 'listing'
        has_prev = paged and view['page'] > 1
        has_next = paged and bool(items) and bool(view['action']) and (not view['pages'] or view['page'] < view['pages'])
        if paged:
            page_label = (localize(30066) % (view['page'], view['pages']) if view['pages']
                          else localize(30065) % view['page'])
        else:
            page_label = ''
        self.setProperty('view_title', view['title'])
        self.setProperty('view_subtitle', view.get('subtitle') or page_label)
        self.setProperty('page_label', page_label)
        self.setProperty('has_prev', 'true' if has_prev else '')
        self.setProperty('has_next', 'true' if has_next else '')
        self.setProperty('empty', '' if items else 'true')

    def _tile(self, item):
        title = item.get('title') or ''
        li = xbmcgui.ListItem(title, offscreen=True)
        image = item.get('image') or ''
        if image:
            li.setArt({'poster': image, 'thumb': image})
        li.setProperty('shorttitle', _short(title))
        li.setProperty('fulltitle', title)
        year, rating, lang = item.get('year') or '', item.get('rating') or '', item.get('lang') or ''
        li.setProperty('year', str(year))
        li.setProperty('rating', str(rating))
        badge = item.get('badge') or ''
        if not badge:
            if item.get('series'):
                badge = localize(30074)
            elif 'dublaj' in lang.lower():
                badge = localize(30075)
            elif 'altyaz' in lang.lower():
                badge = localize(30076)
        li.setProperty('badge', badge)
        li.setProperty('meta', '  ·  '.join(x for x in (str(year), 'IMDb %s' % rating if rating else '', lang) if x))
        return li

    # -------------------------------------------------------------- events
    def onClick(self, control_id):
        if control_id == SEARCH:
            self._search()
        elif control_id == SETTINGS:
            addon().openSettings()
        elif control_id == CATEGORIES:
            self._sidebar_click()
        elif control_id == PREV:
            self._change_page(-1)
        elif control_id == NEXT:
            self._change_page(1)
        elif control_id == GRID:
            self._grid_click()
        elif control_id == DETAILS_MAIN:
            self._details_main()
        elif control_id == DETAILS_TRAILER:
            self._details_trailer()
        elif control_id == DETAILS_BACK:
            self._back()

    def _grid_click(self):
        if not self.stack:
            return
        view = self.stack[-1]
        position = self.grid.getSelectedPosition()
        if not 0 <= position < len(view['items']):
            return
        item = view['items'][position]
        kind = item.get('kind')
        if kind == 'season':
            view['focus'] = position
            self._push(self._episodes_view(view['title'], view['image'], item['season'],
                                           view['seasons'][item['season']]))
        elif kind == 'episode':
            self._play(item, position)
        elif setting_bool('show_details', True):
            self._open_details(item)
        elif item.get('series'):
            self._open_series(item)
        else:
            self._play(item, position)

    def _play(self, item, position):
        """Hand over to the existing play route (source + language dialogs, setResolvedUrl)."""
        self.stack[-1]['focus'] = position
        self.restore = (GRID, position)
        self._play_media(item['url'])

    def _play_media(self, page_url):
        url = PLUGIN_ROOT + '?' + urlencode({'action': 'play', 'url': page_url})
        log('gui: PlayMedia %s' % url)
        xbmc.executebuiltin('PlayMedia(%s)' % url)

    def onAction(self, action):
        if action.getId() in BACK_ACTIONS:
            self._back()

    # ------------------------------------------------------------- helpers
    @contextmanager
    def _busy(self):
        self.setProperty('loading', 'true')
        try:
            yield
        finally:
            self.clearProperty('loading')


def open_browser():
    """Show the full-screen browser (blocks until it is closed)."""
    home = _home()
    if is_open():
        log('gui already open')
        return
    home.setProperty(PROP_OPEN, 'true')
    try:
        window = Browser(XML_FILE, addon().getAddonInfo('path'), 'Default', '1080i')
        window.doModal()
        del window
    finally:
        home.clearProperty(PROP_OPEN)
        home.setProperty(PROP_CLOSED, str(time.time()))
    # Leave the one-item plugin root the window was started from.
    xbmc.sleep(200)
    if xbmc.getInfoLabel('Container.FolderPath').rstrip('/') == PLUGIN_ROOT.rstrip('/'):
        xbmc.executebuiltin('Action(Back)')
