"""Internationalization (gettext).

Source strings are English. Translations live in po/<lang>.po and are compiled
to locale/<lang>/LC_MESSAGES/matiz.mo (see tools/update-translations).

The language follows the system (LANGUAGE, LC_ALL, LC_MESSAGES, LANG). It can
be forced with "language" in ~/.config/matiz/config.json (e.g. "en", "pt_BR").

Usage:
    from .i18n import _, N_, ngettext
    _("Close tab")
    _("Copied: {path}").format(path=p)          # named placeholders, never f-strings
    ngettext("{n} tab", "{n} tabs", n).format(n=n)
    LABEL = N_("Red")  ...  _(LABEL)              # constants: mark now, translate at use
"""
from __future__ import annotations

import gettext
import locale
from pathlib import Path

DOMAIN = "matiz"
LOCALE_DIR = Path(__file__).resolve().parent.parent / "locale"

try:
    locale.setlocale(locale.LC_ALL, "")
except locale.Error:
    pass

_translation: gettext.NullTranslations = gettext.translation(DOMAIN, LOCALE_DIR, fallback=True)


def setup(language: str | None = None) -> None:
    """Reload translations; `language` overrides the system language."""
    global _translation
    langs = [language] if language else None
    _translation = gettext.translation(DOMAIN, LOCALE_DIR, languages=langs, fallback=True)


def _(message: str) -> str:
    return _translation.gettext(message)


def N_(message: str) -> str:
    """Marks a string for extraction without translating it (for constants)."""
    return message


def ngettext(singular: str, plural: str, n: int) -> str:
    return _translation.ngettext(singular, plural, n)


def current_language() -> str:
    info = getattr(_translation, "info", lambda: {})()
    return info.get("language", "en") if info else "en"


def fmt_decimal(value: float, decimals: int = 1) -> str:
    """Number with the decimal separator of the current locale (1.2 / 1,2)."""
    try:
        return locale.format_string(f"%.{decimals}f", value)
    except (ValueError, TypeError):
        return f"{value:.{decimals}f}"


def fmt_time(ts: float, fmt: str) -> str:
    """strftime with a translatable format (e.g. _("%Y-%m-%d %H:%M"))."""
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime(fmt)

