# -*- coding: utf-8 -*-
"""Dialogs, notifications and directory items."""
import xbmc
import xbmcgui
import xbmcplugin

from .common import addon, localize, log

# Option id -> string id of its dialog label
OPTION_LABELS = {'tr_dub': 30040, 'tr_sub': 30041, 'original': 30042}
# Setting value -> option / source name
PREFERRED_LANGUAGE = {1: 'tr_dub', 2: 'tr_sub', 3: 'original'}
PREFERRED_SOURCE = {1: 'close', 2: 'rapidrame'}


def notify(message, kind='info', ms=4000):
    icon = {'info': xbmcgui.NOTIFICATION_INFO, 'warning': xbmcgui.NOTIFICATION_WARNING,
            'error': xbmcgui.NOTIFICATION_ERROR}.get(kind, xbmcgui.NOTIFICATION_INFO)
    xbmcgui.Dialog().notification(addon().getAddonInfo('name'), message, icon, ms)


def keyboard(heading, default=''):
    kb = xbmc.Keyboard(default, heading)
    kb.doModal()
    if kb.isConfirmed():
        return kb.getText().strip()
    return None


# --------------------------------------------------------------------------
# Playback dialogs
# --------------------------------------------------------------------------
def select_source(sources, failed=(), preferred=None):
    """Step 1 - pick the player source. Skipped when the preferred source exists
    (and has not failed) or when there is only one source."""
    if preferred:
        match = next((s for s in sources if s['name'] == preferred and s['name'] not in failed), None)
        if match:
            log('preferred source %s selected automatically' % match['label'])
            return match
    if len(sources) == 1 and not failed:
        return sources[0]
    ordered = [s for s in sources if s['name'] not in failed] + [s for s in sources if s['name'] in failed]
    labels = []
    for s in ordered:
        tabs = ', '.join(dict.fromkeys(v['lang_label'] for v in s['variants']))
        label = '%s  [COLOR gray]%s[/COLOR]' % (s['label'], tabs) if tabs else s['label']
        if s['name'] in failed:
            label += '  [COLOR red]%s[/COLOR]' % localize(30032)
        labels.append(label)
    index = xbmcgui.Dialog().select(localize(30030), labels)
    return ordered[index] if index >= 0 else None


def option_label(option, options):
    label = localize(OPTION_LABELS[option['id']]) if option['id'] in OPTION_LABELS else option['tab_label']
    if option.get('hardsub'):
        label += ' - ' + localize(30043)
    if sum(1 for o in options if o['id'] == option['id']) > 1:
        label += '  [COLOR gray][%s][/COLOR]' % option['tab_label']
    return label


def select_language(options, preferred=None, source_label=''):
    """Step 2 - pick the language version of the chosen source. Skipped when the
    preferred version exists or when there is only one version."""
    if preferred:
        match = next((o for o in options if o['id'] == preferred), None)
        if match:
            log('preferred language %s selected automatically' % preferred)
            return match
        log('preferred language %s not available for this title/source - asking' % preferred)
    if len(options) == 1:
        return options[0]
    index = xbmcgui.Dialog().select(localize(30031) % source_label, [option_label(o, options) for o in options])
    return options[index] if index >= 0 else None


# --------------------------------------------------------------------------
# Directory items
# --------------------------------------------------------------------------
def _info(li, item, mediatype):
    tag = li.getVideoInfoTag()
    tag.setTitle(item.get('title') or '')
    tag.setMediaType(mediatype)
    plot = item.get('plot') or ''
    extras = [x for x in (item.get('lang'), 'IMDb %s' % item['rating'] if item.get('rating') else '') if x]
    if extras and not plot:
        plot = ' | '.join(extras)
    tag.setPlot(plot)
    try:
        if item.get('year'):
            tag.setYear(int(item['year']))
        if item.get('rating'):
            tag.setRating(float(str(item['rating']).replace(',', '.')))
    except ValueError:
        pass
    if mediatype == 'episode':
        tag.setTvShowTitle(item.get('tvshowtitle') or '')
        tag.setSeason(int(item.get('season') or 0))
        tag.setEpisode(int(item.get('episode') or 0))


def _art(li, image):
    if image:
        li.setArt({'poster': image, 'thumb': image, 'icon': image})
    else:
        icon = addon().getAddonInfo('icon')
        li.setArt({'thumb': icon, 'icon': icon})


def add_folder(handle, url, label, image='', item=None, context=None):
    li = xbmcgui.ListItem(label=label, offscreen=True)
    _art(li, image)
    if item:
        _info(li, item, 'tvshow')
    if context:
        li.addContextMenuItems(context)
    xbmcplugin.addDirectoryItem(handle, url, li, isFolder=True)


def add_playable(handle, url, item, mediatype='movie', label=None):
    li = xbmcgui.ListItem(label=label or item.get('title') or '', offscreen=True)
    _art(li, item.get('image'))
    _info(li, item, mediatype)
    li.setProperty('IsPlayable', 'true')
    xbmcplugin.addDirectoryItem(handle, url, li, isFolder=False)


def end_directory(handle, content='videos', cache=True, succeeded=True):
    if content:
        xbmcplugin.setContent(handle, content)
    xbmcplugin.endOfDirectory(handle, succeeded=succeeded, cacheToDisc=cache)
