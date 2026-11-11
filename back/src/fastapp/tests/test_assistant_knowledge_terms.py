"""Query normalisation for the knowledge base search (#461 phase 2). Pure."""

from __future__ import annotations

import pytest

from fastapp.assistant.knowledge import query_terms, to_tsquery_text


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        # Darija, Arabic script: filler words gone, article + clitics stripped.
        ("واش خاصني نسقي الزيتون فالصيف؟", ["نسقي", "زيتون", "صيف"]),
        # Hamza / teh marbuta variants fold like the index does.
        ("الأرض المسقية", ["ارض", "مسقيه"]),
        # French: stopwords dropped, compound split, duplicates removed.
        (
            "Comment irriguer la tomate en goutte-à-goutte ?",
            ["irriguer", "tomate", "goutte"],
        ),
        # Arabizi keeps its digits.
        ("wach khasni nsqi zitoun b 3ochba", ["nsqi", "zitoun", "3ochba"]),
        ("", []),
        ("? ! ...", []),
    ],
)
def test_query_terms(question, expected):
    assert query_terms(question) == expected


def test_terms_are_capped():
    words = " ".join(f"mot{i}" for i in range(30))
    assert len(query_terms(words)) == 12


def test_tsquery_is_an_or_of_prefixes_and_cannot_inject_operators():
    terms = query_terms("sol & !humide | (olivier):* <-> x")
    assert to_tsquery_text(terms) == "sol:* | humide:* | olivier:*"
