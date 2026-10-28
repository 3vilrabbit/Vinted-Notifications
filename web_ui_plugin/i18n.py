"""
Tiny gettext-style translation layer for the web interface.

The English text is the message id, so templates stay readable: {{ _("Save changes") }}.
Catalogs live in translations/<code>.json as {"English text": "translated text"}; a
"_name" entry holds the language's own name. Anything missing from a catalog falls back
to English. To add a language, copy translations/it.json to <code>.json and translate it.

Messages with variables use named placeholders: _("Deleted %(n)s items", n=3).
"""

import json
import os

from markupsafe import Markup

TRANSLATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "translations")
DEFAULT_LANGUAGE = "en"

# Strings used from JavaScript (vnT in base.html). They are sent to the browser as a dict.
JS_MESSAGES = (
    "Close",
    "Running",
    "Stopped",
    "Unknown",
    "Start",
    "Stop",
    "Remove %(name)s",
    "No log entries found",
    "%(a)s–%(b)s of %(c)s",
    "%(n)s entries",
    "Error loading logs",
    "Auto-Refresh: ON",
    "Auto-Refresh: OFF",
    "just now",
    "%(n)s min ago",
    "%(n)s h ago",
    "%(n)s d ago",
    "%(n)s new item",
    "%(n)s new items",
    "%(n)s+ new items",
    "Show",
    "Dismiss",
    "Install app",
    "This listing is no longer in your items.",
    "Checked %(c)s items, removed %(r)s",
    "Checking...",
    "Could not remove the item",
    "Removed from your items",
    "No longer available: removed from your items",
    "No longer available",
    "Sold",
    "A push reached this device and its notification was shown.",
    "A push reached this device.",
    "Notifications are now on for this device.",
    "Could not turn on notifications: %(e)s",
    "no",
    "yes",
    "Service worker: %(w)s",
    "Installed app: %(v)s",
    "Permission: %(p)s",
    "Server online",
    "The server and the scraper are running",
    "The scraper has not completed its first scan yet",
    "No scan has finished recently: check the logs",
    "Update available",
    "The server is running, a newer version is available",
    "This page cannot reach the server",
    "Server starting",
    "Scraper not responding",
    "Server unreachable",
    "At least %(n)s characters",
    "%(n)s more characters needed",
    "Strong",
    "Acceptable: a few random words are stronger",
    "This browser cannot receive push notifications.",
    "Add the app to the Home Screen to receive notifications on this iPhone or iPad.",
    "Blocked in the browser settings for this site.",
    "Off on this device",
    "On for this device",
    "Turn off",
    "Turn on",
)

_catalogs = {}
_names = {DEFAULT_LANGUAGE: "English"}


def load_catalogs():
    """(Re)read every translations/*.json file."""
    _catalogs.clear()
    _names.clear()
    _names[DEFAULT_LANGUAGE] = "English"
    if not os.path.isdir(TRANSLATIONS_DIR):
        return
    for filename in sorted(os.listdir(TRANSLATIONS_DIR)):
        if not filename.endswith(".json"):
            continue
        code = filename[:-5]
        with open(os.path.join(TRANSLATIONS_DIR, filename), encoding="utf-8") as handle:
            data = json.load(handle)
        _names[code] = data.get("_name", code)
        _catalogs[code] = {k: v for k, v in data.items() if not k.startswith("_")}


def available_languages():
    """[(code, native name)] with English first."""
    others = sorted((code, name) for code, name in _names.items() if code != DEFAULT_LANGUAGE)
    return [(DEFAULT_LANGUAGE, _names[DEFAULT_LANGUAGE])] + others


def resolve_language(setting, accept_languages):
    """
    Pick the interface language: the one chosen in Configuration if valid, otherwise the best
    match for the browser's Accept-Language header, otherwise English.
    """
    codes = [code for code, _ in available_languages()]
    if setting in codes:
        return setting
    match = accept_languages.best_match(codes) if accept_languages is not None else None
    return match or DEFAULT_LANGUAGE


def translate(lang, msgid, **kwargs):
    """Translate `msgid` into `lang` and fill the %(name)s placeholders, if any."""
    text = _catalogs.get(lang, {}).get(msgid) or msgid
    if not kwargs:
        return text
    if any(isinstance(value, Markup) for value in kwargs.values()):
        # Keep trusted markup, escape everything else
        return Markup(text) % kwargs
    return text % kwargs


def js_catalog(lang):
    """Translations of JS_MESSAGES for the browser."""
    catalog = _catalogs.get(lang, {})
    return {msgid: catalog.get(msgid, msgid) for msgid in JS_MESSAGES}


load_catalogs()
