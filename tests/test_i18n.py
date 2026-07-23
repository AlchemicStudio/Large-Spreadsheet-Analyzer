"""i18n: catalog completeness across all languages, translator behavior."""

import re

import pytest

from lsa.core.i18n import Translator, available_locales, load_catalog, system_locale

EXPECTED_LOCALES = {"en", "fr", "de", "es", "pt"}
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def test_all_expected_locales_ship() -> None:
    assert set(available_locales()) == EXPECTED_LOCALES
    assert available_locales()[0] == "en"  # English first (fallback locale)


@pytest.mark.parametrize("locale", sorted(EXPECTED_LOCALES))
def test_catalog_has_every_key_translated(locale: str) -> None:
    reference = load_catalog("en")
    catalog = load_catalog(locale)
    assert set(catalog) == set(reference), (
        f"{locale}.json keys differ from en.json: "
        f"missing={sorted(set(reference) - set(catalog))} "
        f"extra={sorted(set(catalog) - set(reference))}"
    )
    empty = [k for k, v in catalog.items() if not isinstance(v, str) or not v.strip()]
    assert not empty, f"{locale}.json has empty values for: {empty}"


@pytest.mark.parametrize("locale", sorted(EXPECTED_LOCALES - {"en"}))
def test_placeholders_match_english(locale: str) -> None:
    reference = load_catalog("en")
    catalog = load_catalog(locale)
    for key, text in reference.items():
        expected = set(_PLACEHOLDER.findall(text))
        actual = set(_PLACEHOLDER.findall(catalog[key]))
        assert actual == expected, f"{locale}.json:{key} placeholders {actual} != {expected}"


def test_translator_formats_and_falls_back() -> None:
    tr = Translator("fr")
    assert tr("nav.next") == "Suivant"
    assert tr("run.rows", rows=12) == "12 lignes traitées"
    assert tr("no.such.key") == "no.such.key"


def test_translator_rejects_unknown_locale_on_set() -> None:
    tr = Translator("xx")  # constructor falls back quietly
    assert tr.locale == "en"
    with pytest.raises(ValueError, match="unknown locale"):
        tr.set_locale("xx")


def test_system_locale_env_fallback(monkeypatch) -> None:
    monkeypatch.setattr("locale.getlocale", lambda: (None, None))
    monkeypatch.setenv("LC_ALL", "pt_PT.UTF-8")
    assert system_locale() == "pt"
    monkeypatch.setenv("LC_ALL", "it_IT.UTF-8")
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    monkeypatch.delenv("LANG", raising=False)
    assert system_locale() == "en"
