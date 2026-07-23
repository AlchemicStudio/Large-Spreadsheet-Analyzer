"""JSON-catalog based internationalization for the GUI.

Code always uses keys (``tr("nav.next")``); catalogs live in
``lsa/core/locales/<code>.json``.  Missing keys fall back to English, then to
the key itself, so an incomplete catalog can never crash the UI.
"""

from __future__ import annotations

import json
import locale as locale_module
import os
from functools import cache
from importlib import resources

DEFAULT_LOCALE = "en"


def _locales_dir():
    return resources.files("lsa.core").joinpath("locales")


@cache
def available_locales() -> tuple[str, ...]:
    """Locale codes shipped with the application, English first."""
    codes = sorted(
        entry.name.removesuffix(".json")
        for entry in _locales_dir().iterdir()
        if entry.name.endswith(".json")
    )
    if DEFAULT_LOCALE in codes:
        codes.remove(DEFAULT_LOCALE)
        codes.insert(0, DEFAULT_LOCALE)
    return tuple(codes)


@cache
def load_catalog(locale: str) -> dict[str, str]:
    """Load one message catalog; raises FileNotFoundError for unknown locales."""
    text = _locales_dir().joinpath(f"{locale}.json").read_text(encoding="utf-8")
    catalog = json.loads(text)
    if not isinstance(catalog, dict):
        raise ValueError(f"catalog {locale}.json must be a JSON object")
    return catalog


def system_locale(default: str = DEFAULT_LOCALE) -> str:
    """Best-effort system language, restricted to the shipped locales."""
    candidates: list[str] = []
    try:
        code = locale_module.getlocale()[0]
        if code:
            candidates.append(code)
    except ValueError:  # pragma: no cover - platform-specific
        pass
    for env in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(env)
        if value:
            candidates.append(value)
    for candidate in candidates:
        short = candidate.replace("-", "_").split("_")[0].split(".")[0].lower()
        if short in available_locales():
            return short
    return default


class Translator:
    """Callable translator bound to a locale: ``tr("run.rows", rows=5)``."""

    def __init__(self, locale: str = DEFAULT_LOCALE):
        self.locale = locale if locale in available_locales() else DEFAULT_LOCALE

    def set_locale(self, locale: str) -> None:
        if locale not in available_locales():
            raise ValueError(f"unknown locale {locale!r} (available: {available_locales()})")
        self.locale = locale

    def __call__(self, key: str, **kwargs: object) -> str:
        text = load_catalog(self.locale).get(key)
        if text is None and self.locale != DEFAULT_LOCALE:
            text = load_catalog(DEFAULT_LOCALE).get(key)
        if text is None:
            text = key
        return text.format(**kwargs) if kwargs else text
