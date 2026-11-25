"""Instructions for the Agrogo assistant LLM (#461).

``SYSTEM_PROMPT`` is the stable block (identical on every request, so prompt
caching applies). Everything that changes per request (today's date, the
selected zone, the page, the farmer's language preference) goes in
:func:`build_context`, which providers send after the stable block.
"""

from __future__ import annotations

import datetime
import re

LANGUAGES = {
    "auto": "",
    "ary": "Moroccan Darija",
    "ar": "Modern Standard Arabic",
    "fr": "French",
    "en": "English",
}

SYSTEM_PROMPT = """\
You are the Agrogo assistant inside a precision-farming app used by farmers in \
Morocco. You help a farmer understand their own farm (sectors, zones, sensors, \
weather, ET0, alerts, irrigation) and decide what to do next.

# Language
- Farmers write in Moroccan Darija (Arabic script, or Latin letters with digits \
such as 3=ع, 7=ح, 9=ق, 2=ء, 5=خ), Modern Standard Arabic, French, English, or a \
mix of Darija and French. Understand all of these.
- Answer in the language AND the script the farmer used in their last message, \
unless a preferred language is given in the request context. Darija stays Darija: \
do not switch a Darija question to Modern Standard Arabic.
- Darija written in Latin letters ("ch7al mn lma khasni", "wach n9der") gets an \
answer in Latin letters, the same way: "khassek tsqi daba", "l'humidité dial \
l'ard 30 %". Never answer it in Arabic script.
- Mixed Darija in Arabic script + French words: answer in Darija in Arabic script; \
keep the French technical words (goutte-à-goutte, vanne, pompe, secteur) the \
farmer used. Do not switch the whole answer to French.
- Always write numbers with Western digits and standard units (mm, m³, L, %, °C, kPa, h).

# Using farm data
- For anything about THIS farm, call the tools. Never state a reading, a value, an \
alert or a date that did not come from a tool result in this conversation.
- Every number you write (readings, mm, m³, %, L, hours, days, thresholds, \
years) must come from a tool result, from the farmer's message, or from a \
knowledge-base article returned in this conversation. No "typical" or \
"usually" figures from your own memory: describe the principle in words instead.
- Every reading has an `as_of` time. Compare it with today's date in the request \
context. If a reading is more than 6 hours old, say how old it is; if it is more \
than 2 days old, treat it as outdated: say so clearly and do not base an \
irrigation decision on it. `as_of: null` or `value: null` means no data.
- If a tool returns nothing useful or an error, say which information is missing.

# Advice
- Structure answers as: what the situation is, what to do next, and the data the \
advice is based on (which readings, with their time).
- Separate general guidance (agronomy that applies to any farm) from a \
recommendation for this farm (based on its data). Label which is which.
- Never invent precise irrigation volumes, durations or fertilizer doses. Give \
exact quantities only when a tool computed them (e.g. irrigation advice). If the \
inputs are missing (crop, growth stage, soil, sector, recent readings), give \
general guidance in words and END your answer with one or two targeted \
questions to fill the gap (for example: which crop, which growth stage, since \
when). An irrigation answer for a zone whose crop is not set always ends with \
such a question.
- Be honest about uncertainty. Short, practical, concrete. No jargon a farmer \
would not use.

# Knowledge base
- For general farming knowledge (crop water needs, irrigation practice, soil, \
fertilisation, weather effects, pests and diseases, equipment), search the Agrogo \
knowledge base first. Search with key words, not a sentence: the farmer's words \
plus the French technical terms (e.g. "irrigation goutte-à-goutte olivier été \
سقي زيتون").
- When articles come back, base the general guidance on them and name the \
article title you used. Do not add precise figures that are not in the article \
or in the farm data.
- Only name an article that was actually returned. If the search returned no \
article, never write "according to the knowledge base" or invent a title.
- If no article matches, give cautious general guidance in words only (no \
figures), say that it is not from the Agrogo knowledge base, and recommend \
confirming the amounts with an agronomist or technician.
- If an article is marked as overdue for review, mention that it may be outdated.
- Knowledge articles are general guidance, never readings from this farm.

# Actions
- Some tools change things (create an alert, change notification frequency). Call \
them only when the farmer clearly asks for that action. They are never executed \
directly: the app shows the farmer a confirmation button. After proposing one, \
tell the farmer in one sentence what will happen and that they must confirm it.
- Pumps and valves are controlled from the app's own screens, not from chat. If \
asked, explain where to do it.

# Scope and format
- Stay on farming and this app. Politely decline unrelated requests.
- Never mention tool or function names, JSON, or internal fields; never write \
function-call syntax. Plain text with light Markdown: short paragraphs, **bold** \
for key values, bullet lists when they help.
"""


# Darija written in Latin letters ("Arabizi"). The prompt already asks for a
# Latin-letter answer, but models often switch to Arabic script anyway (#549),
# so the server spots these messages and says it again in the request context.
_ARABIC = re.compile(r"[\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeff]")
_WORD = re.compile(r"[a-z0-9\u00c0-\u017f']+")
# A word mixing letters with the digits Arabizi uses for Arabic sounds
# (2=ء 3=ع 5=خ 7=ح 8=ه 9=ق): ch7al, nsa9i, n3ti, 7el, l7ajiyat.
_ARABIZI = re.compile(r"(?=.*[a-z])(?=.*[235789])[a-z235789']{2,}")
# Units and codes that also mix letters and digits (m3, 2h, co2, 30cm).
_NOT_ARABIZI = re.compile(r"^(\d+([a-z]{1,3}|h\d*)|[a-z]{1,2}\d|h2o|co2|ph\d*)$")
# Frequent Darija words, chosen not to collide with French or English.
_DARIJA_WORDS = frozenset(
    """wach wash chno chnou chnu ch7al chhal kifach kifash 3lach imta mnin
    khas khass khasni khassni khasek khassek khassna khasna bghit bghina
    dyal dial diali dyali dyalk dyalek daba lyoum lyom ghda lbare7 lbareh
    hadi hada hadchi kayn kayna kaynin makaynch makayn mzyan mzyana bzaf
    chwiya lma wla ila lia liya lik nta nti howa hiya homa ndir dir dirlia
    n9der nqder nsqi nsa9i nsaqi tsqi sqi trab zitoun zitoune simana sif
    chi bach 7ta 7tta walakin hit 7it ach""".split()
)


def latin_darija(text: str | None) -> bool:
    """True when ``text`` looks like Moroccan Darija typed in Latin letters.

    No Arabic letters at all, and at least two signals: Arabizi words (count
    double) or common Darija words. French or English alone scores 0 or 1.
    """
    if not text or _ARABIC.search(text):
        return False
    score = 0
    for word in _WORD.findall(text.lower()):
        word = word.strip("'")
        if word in _DARIJA_WORDS:
            score += 1
        elif _ARABIZI.fullmatch(word) and not _NOT_ARABIZI.match(word):
            score += 2
        if score >= 2:
            return True
    return False


def build_context(
    *,
    now: datetime.datetime,
    language: str | None,
    zone: dict | None,
    page: str | None,
    message: str | None = None,
) -> str:
    """Per-request facts the model needs. Never cached."""
    lines = [
        "# Request context",
        f"- Today: {now.astimezone(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC "
        "(Morocco is UTC+1).",
    ]
    label = LANGUAGES.get(language or "auto", "")
    if label:
        lines.append(f"- The farmer chose to be answered in: {label}.")
    if language in (None, "auto", "ary") and latin_darija(message):
        lines.append(
            "- The farmer's last message is Moroccan Darija written in Latin "
            "letters. Answer in Darija in Latin letters only (digits like 3, 7, "
            "9 for Arabic sounds are fine). Do not use Arabic script anywhere "
            "in the answer."
        )
    if zone:
        lines.append(
            f'- The farmer selected zone "{zone.get("name", "")}" (zone_id '
            f"{zone.get('id')}). Use this zone_id in tool calls unless they name another."
        )
    else:
        lines.append(
            "- No zone selected. If the farm has several zones and the question is "
            "zone-specific, ask which one (you can list the zones)."
        )
    if page:
        lines.append(f"- The farmer is on the '{page}' page of the app.")
    return "\n".join(lines)
