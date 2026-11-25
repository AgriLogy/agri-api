"""Latin-letter Darija hint in the request context (#549).

The system prompt asks for a Latin-letter answer to Darija typed in Latin
letters, but models often answer in Arabic script anyway. The server spots
these messages and repeats the instruction in the per-request context.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from fastapp.assistant.prompt import build_context, latin_darija

NOW = datetime.datetime(2026, 11, 25, 9, 0, tzinfo=datetime.timezone.utc)
HINT = "Darija written in Latin letters"
CASES = Path(__file__).resolve().parents[1] / "assistant" / "eval" / "cases.json"


def _questions():
    groups = json.loads(CASES.read_text(encoding="utf-8"))["groups"]
    for group in groups:
        for variant, text in group["questions"].items():
            yield pytest.param(variant, text, id=f"{group['id']}.{variant}")


@pytest.mark.parametrize(("variant", "text"), list(_questions()))
def test_eval_questions_are_classified_by_script(variant, text):
    """Only the `ary_lat` phrasing of each eval question is Latin Darija."""
    assert latin_darija(text) is (variant == "ary_lat")


@pytest.mark.parametrize(
    "text",
    [
        "fin kayn lma?",
        "3afak 3tini l'humidité",
        "WACH KHASNI NSQI DABA",
        "ch7al mn m3 khassni?",
    ],
)
def test_latin_darija_detected(text):
    assert latin_darija(text)


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "salam",
        "wach?",
        "J'ai mis 30 m3 hier sur la zone 2, c'est assez ?",
        "Le pH est à 7,2 et la CE à 2,5 dS/m",
        "Il faut irriguer 3h ce soir ?",
        "co2 et h2o dans la serre",
        "la vanne est ouverte a 50%",
        "How much water did zone 1 get in the last 2h?",
        "واش خاصني نسقي daba؟",
    ],
)
def test_not_latin_darija(text):
    """French, English, units, one-word greetings and Arabic script: no hint."""
    assert not latin_darija(text)


def _context(message, language="auto"):
    return build_context(
        now=NOW, language=language, zone=None, page=None, message=message
    )


def test_context_adds_hint_for_latin_darija():
    ctx = _context("wach khasni nsa9i lyoum f zone 1?")
    assert HINT in ctx
    assert "Do not use Arabic script" in ctx


@pytest.mark.parametrize("language", [None, "auto", "ary"])
def test_hint_follows_auto_and_darija_preferences(language):
    assert HINT in _context("ch7al l'humidité dyal trab daba?", language)


@pytest.mark.parametrize("language", ["fr", "en", "ar"])
def test_explicit_other_language_wins(language):
    """A farmer who chose French, English or MSA keeps that language."""
    assert HINT not in _context("ch7al l'humidité dyal trab daba?", language)


def test_no_hint_for_french_or_arabic_script():
    assert HINT not in _context("Est-ce que je dois irriguer la zone 1 ?")
    assert HINT not in _context("واش خاصني نسقي اليوم فالزون 1؟")
    assert HINT not in _context(None)
