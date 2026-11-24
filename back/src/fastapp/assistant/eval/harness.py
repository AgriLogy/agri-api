"""Offline evaluation harness for the Agrogo assistant (#461).

Runs the real :class:`~fastapp.assistant.llm.LLMOrchestrator` (same system
prompt, tool catalog and confirm-before-write gate as production) against a
real provider, but answers every tool call from a canned *scenario* instead of
the database. A case is a farmer question in one language variant plus the
farm state it is asked against; the reply is graded by deterministic checks:

hard checks (a failure fails the case)
* ``tools_any`` / ``tools_none`` — the right lookups were (not) made
* ``pending_action`` — an action is only ever *proposed*, and only when asked
* ``script`` — reply in the farmer's script (Arabic vs Latin letters)
* ``grounded_numbers`` — every number in the reply comes from the question,
  a tool result or a small allowlist; nothing invented
* ``forbid_numbers`` — e.g. no irrigation volume from outdated readings
* ``asks_question`` — asks a follow-up when essential inputs are missing

soft checks (reported, never fail a case: wording varies too much to
match reliably across Darija / Arabizi / MSA / French)
* ``mentions_any`` — expected idea expressed with one of several phrasings
* ``darija_markers`` — the reply reads as Darija, not MSA

Nothing here talks to the database or to a provider by itself; the CLI in
``__main__`` wires a provider in and refuses to spend money without ``--yes``.
"""

from __future__ import annotations

import copy
import datetime
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..llm import LLMOrchestrator
from ..orchestrator import RuleBasedOrchestrator
from ..providers import LLMProvider, LLMResult
from ..tools import registry

CASES_PATH = Path(__file__).with_name("cases.json")

# The eval clock: mid-summer afternoon, so "today" and reading ages are stable.
NOW = datetime.datetime(2026, 8, 6, 15, 0, tzinfo=datetime.timezone.utc)
FRESH = (NOW - datetime.timedelta(minutes=20)).isoformat()
STALE = (NOW - datetime.timedelta(days=3)).isoformat()

LANGS = ("ary_ar", "ary_lat", "msa", "fr", "mix")

ZONE = {"id": 6, "name": "Zone 1"}

# Small integers are everywhere in good answers ("1 or 2 questions", "step 3")
# and in times; only numbers above this must be grounded.
SMALL_INT_MAX = 10

_ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_LATIN = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]")
# A number, with thousands written "144 638" (space, no-break or narrow
# no-break space) read as one number.
_NUMBER = re.compile(
    r"(?<![\w.])-?\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:[.,]\d+)?(?!\d)|(?<![\w.])-?\d+(?:[.,]\d+)?"
)
# Arabizi spells letters with digits (3=ع, 7=ح, 9=ق, 2=ء, 5=خ): "nsa9i" is a word.
_ARABIZI_WORD = re.compile(r"\b[a-z]*[23579][a-z]+\b|\b[a-z]+[23579][a-z]*\b", re.I)
_EASTERN_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

DARIJA_MARKERS = (
    "واش",
    "ديال",
    "دابا",
    "بزاف",
    "غادي",
    "كاين",
    "خاص",
    "شنو",
    "شحال",
    "ماشي",
    "مزيان",
    "باش",
    "ديالك",
    "فين",
    "علاش",
    "هاد",
)


# ---------------------------------------------------------------------------
# Scenarios: what each tool answers for a given farm state
# ---------------------------------------------------------------------------
def _metric(key, label, value, unit, status, as_of):
    return {
        "key": key,
        "label": label,
        "value": value,
        "unit": unit,
        "status": status,
        "as_of": as_of,
    }


def _farm(as_of: str | None, *, empty: bool = False) -> dict[str, dict]:
    v = (lambda x: None) if empty else (lambda x: x)
    at = None if empty else as_of
    soil = _metric(
        "soilMoisture",
        "Humidité du sol",
        v(18.4),
        "%",
        "unknown" if empty else "critical",
        at,
    )
    metrics = {
        "soil": [
            soil,
            _metric("soilTemp", "Température du sol", v(24.1), "°C", "ok", at),
        ],
        "farm": [
            soil,
            _metric("et0", "ET0", v(6.2), "mm", "ok", at),
            _metric("vpd", "VPD", v(2.1), "kPa", "warning", at),
            _metric("airTemp", "Température de l'air", v(34.5), "°C", "ok", at),
            _metric("humidity", "Humidité de l'air", v(22.0), "%", "ok", at),
        ],
        "weather": [
            _metric("airTemp", "Température de l'air", v(34.5), "°C", "ok", at),
            _metric("humidity", "Humidité de l'air", v(22.0), "%", "ok", at),
            _metric("et0", "ET0", v(6.2), "mm", "ok", at),
            _metric("vpd", "VPD", v(2.1), "kPa", "warning", at),
        ],
    }
    advice = (
        {
            "recommendation": "unknown",
            "reason": "Données insuffisantes pour une recommandation.",
            "soil_moisture_pct": None,
            "critical_moisture_threshold": 25.0,
            "et0_mm": None,
            "estimated_water_m3": None,
            "estimated_duration_min": None,
            "as_of": None,
        }
        if empty
        else {
            "recommendation": "irrigate",
            "reason": "Humidité du sol 18.4 % < seuil critique 25.0 %.",
            "soil_moisture_pct": 18.4,
            "critical_moisture_threshold": 25.0,
            "et0_mm": 6.2,
            "vpd_kpa": 2.1,
            "estimated_water_m3": 12.5,
            "estimated_duration_min": 45.0,
            "decision_source": "critical_threshold_fallback",
            "as_of": as_of,
        }
    )
    zone = {
        "id": 6,
        "name": "Zone 1",
        "area_ha": 1.2,
        "critical_moisture_pct": 25.0,
        "crop": None if empty else "olivier",
    }
    return {
        "get_soil_status": {"metrics": metrics["soil"], "zone_id": 6},
        "get_farm_status": {"metrics": metrics["farm"], "zone_id": 6},
        "get_weather": {"metrics": metrics["weather"]},
        "get_irrigation_advice": {**advice, "zone_id": 6, "zone_name": "Zone 1"},
        "list_zones": {"zones": [zone]},
        "get_zone_detail": {"zone": zone},
        "get_active_alerts": {"alerts": []},
        "search_knowledge": {"available": True, "query": "", "articles": []},
        "list_recent_notifications": {"notifications": []},
    }


def _alerts() -> dict[str, dict]:
    tools = _farm(FRESH)
    tools["get_active_alerts"] = {
        "alerts": [
            {
                "id": 41,
                "name": "Humidité sol basse",
                "sensor_key": "soil_moisture_low",
                "zone": "Zone 1",
                "condition": "<",
                "threshold": 20.0,
                "is_active": True,
                "triggered": True,
                "last_value": 18.4,
                "last_triggered_at": FRESH,
            }
        ]
    }
    return tools


SCENARIOS: dict[str, Any] = {
    "dry": lambda: _farm(FRESH),
    "stale": lambda: _farm(STALE),
    "empty": lambda: _farm(None, empty=True),
    "alerts": _alerts,
}


# ---------------------------------------------------------------------------
# Datasets: which farm data the scenarios are answered from
# ---------------------------------------------------------------------------
REAL_DATA_PATH = Path(__file__).with_name("scenarios_real.json")
# The system prompt's own numbers (6 h / 2 days staleness rules, 24 h, 48 h);
# the request-context date and hour are added per dataset.
BASE_PROMPT_NUMBERS = {6.0, 2.0, 24.0, 48.0}


@dataclass(frozen=True)
class Dataset:
    """The eval clock, the farmer's zone, the four scenario states, and the
    *facts* that cases refer to by placeholder (``{soil}`` in ``mentions_any``,
    ``{water}`` in ``forbid_numbers``…), so one case file grades any dataset."""

    name: str
    now: datetime.datetime
    zone: dict[str, Any]
    scenarios: dict[str, Any]  # state -> () -> {tool name: output}
    facts: dict[str, list]

    def prompt_numbers(self) -> set[float]:
        """Numbers the system prompt and request context hand the model."""
        n = self.now
        return BASE_PROMPT_NUMBERS | {
            float(n.year),
            float(n.month),
            float(n.day),
            float(n.hour),
        }


SYNTHETIC = Dataset(
    name="synthetic",
    now=NOW,
    zone=ZONE,
    scenarios=SCENARIOS,
    facts={
        "soil": ["18.4", "18,4"],
        "alert_threshold": ["20"],
        "water": [12.5, 45],
    },
)


def _decimal_spellings(value: float) -> list[str]:
    """How a model may write a reading: 29.95 → 29.95, 29,95, 29.9, 29,9, 30."""
    out = []
    for text in (f"{value:g}", f"{value:.1f}", f"{round(value)}"):
        out += [text, text.replace(".", ",")]
    return list(dict.fromkeys(out))


def load_real_dataset(path: Path = REAL_DATA_PATH) -> Dataset:
    """Pseudo-real farm data: tool outputs captured from the real assistant
    tools against a test farm's real readings (see the file's ``meta``)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    meta, states = data["meta"], data["scenarios"]
    advice = states["dry"]["get_irrigation_advice"]
    duration = advice.get("estimated_duration_min")
    water = [advice.get("estimated_water_m3")]
    if duration is not None:
        water += [round(duration, 2), round(duration)]
    fired = [
        a
        for a in states["alerts"]["get_active_alerts"]["alerts"]
        if a.get("last_triggered_at")
    ]
    return Dataset(
        name="real",
        now=datetime.datetime.fromisoformat(meta["eval_now"]),
        zone=dict(meta["zone"]),
        scenarios={
            state: (lambda outputs=outputs: copy.deepcopy(outputs))
            for state, outputs in states.items()
        },
        facts={
            "soil": _decimal_spellings(advice["soil_moisture_pct"]),
            "alert_threshold": [f"{a['threshold']:g}" for a in fired],
            "water": [w for w in water if w is not None],
        },
    )


DATASETS = {"synthetic": lambda: SYNTHETIC, "real": load_real_dataset}
DEFAULT_DATASET = "real"


def get_dataset(name: str | None = None) -> Dataset:
    return DATASETS[name or DEFAULT_DATASET]()


class ScenarioTools:
    """``run_tool`` for the orchestrator: canned answers, every call recorded.

    A dataset may key a tool output by one parameter (``get_sensor_trend|et0``);
    that wins over the tool's plain entry."""

    def __init__(self, scenario: str, dataset: Dataset = SYNTHETIC):
        self.outputs = dataset.scenarios[scenario]()
        self.calls: list[dict[str, Any]] = []

    def __call__(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        params = dict(params or {})
        keyed = f"{name}|{params.get('sensor_key')}"
        out = self.outputs.get(keyed) or self.outputs.get(
            name, {"error": f"No data for '{name}' in this farm."}
        )
        self.calls.append({"name": name, "input": params})
        return copy.deepcopy(out)


class RecordingProvider:
    """Wraps a provider to keep what the orchestrator drops: tool calls
    (including proposed mutating ones), token usage and latency."""

    def __init__(self, inner: LLMProvider):
        self.inner = inner
        self.last: LLMResult | None = None
        self.seconds = 0.0

    def complete(self, **kwargs) -> LLMResult:
        start = time.perf_counter()
        try:
            self.last = self.inner.complete(**kwargs)
            return self.last
        finally:
            self.seconds = time.perf_counter() - start


# ---------------------------------------------------------------------------
# Cases + grading
# ---------------------------------------------------------------------------
@dataclass
class Check:
    name: str
    ok: bool
    hard: bool
    detail: str = ""


@dataclass
class CaseResult:
    id: str
    scenario: str
    lang: str
    question: str
    reply: str
    tools_called: list[str]
    pending_action: str | None
    fallback_reason: str | None
    seconds: float
    usage: dict[str, int]
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks if c.hard)


_PLACEHOLDER = re.compile(r"^\{(\w+)\}$")


def _expand(values: list, facts: dict[str, list]) -> list:
    """``["{soil}", "sec"]`` → ``["29.95", "29,95", …, "sec"]`` for a dataset."""
    out: list = []
    for v in values:
        m = _PLACEHOLDER.match(v) if isinstance(v, str) else None
        out += facts.get(m.group(1), []) if m else [v]
    return out


def load_cases(
    path: Path = CASES_PATH, dataset: Dataset = SYNTHETIC
) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = []
    for group in data["groups"]:
        expect = dict(group["expect"])
        if "forbid_numbers" in expect:
            expect["forbid_numbers"] = [
                float(n) for n in _expand(expect["forbid_numbers"], dataset.facts)
            ]
        for lang, question in group["questions"].items():
            cases.append(
                {
                    "id": f"{group['id']}.{lang}",
                    "group": group["id"],
                    "scenario": group["scenario"],
                    "lang": lang,
                    "question": question,
                    "expect": expect,
                    "mentions_any": _expand(
                        group.get("mentions_any", []), dataset.facts
                    ),
                }
            )
    return cases


def _numbers(text: str) -> list[float]:
    out = []
    for raw in _NUMBER.findall(text.translate(_EASTERN_DIGITS)):
        try:
            out.append(float(re.sub(r"[ \u00a0\u202f]", "", raw).replace(",", ".")))
        except ValueError:
            continue
    return out


def _strip_arabizi(text: str) -> str:
    return _ARABIZI_WORD.sub(" ", text)


_ISO = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?"
)


def _ages(blob: str, now: datetime.datetime = NOW) -> set[float]:
    """Reading ages a good answer quotes ("20 minutes ago", "3 days old"),
    derived from every ``as_of``-style timestamp, plus its clock time."""
    out: set[float] = set()
    for raw in _ISO.findall(blob):
        try:
            at = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        secs = (now - at).total_seconds()
        out.update(
            {
                round(secs / 60),
                round(secs / 3600),
                round(secs / 3600, 1),
                round(secs / 86400),
                float(at.hour),
                float(at.minute),
                float(at.day),
            }
        )
    return out


def _grounded_pool(
    outputs: dict[str, Any], question: str, dataset: Dataset = SYNTHETIC
) -> set[float]:
    pool = dataset.prompt_numbers()
    blob = json.dumps(outputs, ensure_ascii=False)
    pool |= _ages(blob, dataset.now)
    blob = _ISO.sub(" ", blob)
    for n in _numbers(blob) + _numbers(_strip_arabizi(question)):
        pool.update({n, round(n), round(n, 1), abs(n)})
    return pool


def _close(n: float, pool: set[float]) -> bool:
    return any(abs(n - p) <= 0.051 for p in pool)


def script_of(text: str) -> tuple[int, int]:
    return len(_ARABIC.findall(text)), len(_LATIN.findall(text))


def grade(
    case: dict[str, Any],
    result: CaseResult,
    outputs: dict,
    dataset: Dataset = SYNTHETIC,
) -> list[Check]:
    exp = case["expect"]
    reply = result.reply or ""
    called = set(result.tools_called)
    checks: list[Check] = []

    if any_of := exp.get("tools_any"):
        hit = called & set(any_of)
        checks.append(
            Check(
                "tools_any",
                bool(hit),
                True,
                f"wanted one of {any_of}, got {sorted(called)}",
            )
        )
    if none_of := exp.get("tools_none"):
        bad = called & set(none_of)
        checks.append(
            Check("tools_none", not bad, True, f"called {sorted(bad)}" if bad else "")
        )

    if "pending_action" in exp:
        want = exp["pending_action"]
        checks.append(
            Check(
                "pending_action",
                result.pending_action == want,
                True,
                f"wanted {want}, got {result.pending_action}",
            )
        )

    want_script = {"ary_ar": "arabic", "msa": "arabic", "mix": "arabic"}.get(
        case["lang"], "latin"
    )
    ar, la = script_of(_strip_arabizi(reply) if want_script == "arabic" else reply)
    letters = ar + la
    if want_script == "arabic":
        ok = letters > 0 and ar / letters >= 0.6
    else:
        ok = letters > 0 and la / letters >= 0.8
    checks.append(Check("script", ok, True, f"{want_script}: arabic={ar} latin={la}"))

    pool = _grounded_pool(outputs, case["question"], dataset)
    reply_numbers = _numbers(_strip_arabizi(reply))
    invented = sorted(
        {
            n
            for n in reply_numbers
            if not (n.is_integer() and 0 <= n <= SMALL_INT_MAX) and not _close(n, pool)
        }
    )
    checks.append(
        Check(
            "grounded_numbers",
            not invented,
            True,
            f"not in data or question: {invented}" if invented else "",
        )
    )

    if forbid := exp.get("forbid_numbers"):
        hit = sorted(
            {n for n in reply_numbers if any(abs(n - f) <= 0.051 for f in forbid)}
        )
        checks.append(
            Check("forbid_numbers", not hit, True, f"used {hit}" if hit else "")
        )

    if exp.get("asks_question"):
        checks.append(
            Check(
                "asks_question", "?" in reply or "؟" in reply, True, "no question asked"
            )
        )

    if words := case.get("mentions_any"):
        low = reply.lower()
        checks.append(
            Check(
                "mentions_any",
                any(w.lower() in low for w in words),
                False,
                f"none of {words}",
            )
        )
    if case["lang"] in ("ary_ar", "mix"):
        found = [m for m in DARIJA_MARKERS if m in reply]
        checks.append(
            Check(
                "darija_markers",
                bool(found),
                False,
                "reads as MSA" if not found else "",
            )
        )
    return checks


def run_case(
    case: dict[str, Any], provider: LLMProvider, dataset: Dataset = SYNTHETIC
) -> CaseResult:
    tools = ScenarioTools(case["scenario"], dataset)
    recorder = RecordingProvider(provider)
    orchestrator = LLMOrchestrator(recorder, RuleBasedOrchestrator())
    response = orchestrator.respond(
        case["question"],
        run_tool=tools,
        zone=dataset.zone,
        language=None,  # auto: answer in the language of the question
        now=dataset.now,
    )
    llm = recorder.last
    called = [c.name for c in llm.tool_calls] if llm else []
    pending = getattr(response, "pending_action", None)
    result = CaseResult(
        id=case["id"],
        scenario=case["scenario"],
        lang=case["lang"],
        question=case["question"],
        reply=response.reply or "",
        tools_called=called,
        pending_action=pending["tool"] if pending else None,
        fallback_reason=getattr(response, "fallback_reason", None),
        seconds=round(recorder.seconds, 2),
        usage=dict(llm.usage) if llm else {},
    )
    if result.fallback_reason:
        result.checks = [
            Check("provider", False, True, f"provider failed: {result.fallback_reason}")
        ]
    else:
        result.checks = grade(case, result, tools.outputs, dataset)
    return result


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    by_lang: dict[str, list[CaseResult]] = {}
    by_group: dict[str, list[CaseResult]] = {}
    for r in results:
        by_lang.setdefault(r.lang, []).append(r)
        by_group.setdefault(r.id.rsplit(".", 1)[0], []).append(r)

    def rate(rs):
        return {"passed": sum(r.passed for r in rs), "total": len(rs)}

    soft: dict[str, dict[str, int]] = {}
    for r in results:
        for c in r.checks:
            if not c.hard:
                s = soft.setdefault(c.name, {"ok": 0, "total": 0})
                s["total"] += 1
                s["ok"] += int(c.ok)
    usage: dict[str, int] = {}
    for r in results:
        for k, v in r.usage.items():
            usage[k] = usage.get(k, 0) + int(v or 0)
    secs = sorted(r.seconds for r in results)
    return {
        "overall": rate(results),
        "by_language": {k: rate(v) for k, v in sorted(by_lang.items())},
        "by_group": {k: rate(v) for k, v in sorted(by_group.items())},
        "soft_checks": soft,
        "usage": usage,
        "latency_s": {
            "median": secs[len(secs) // 2] if secs else None,
            "max": secs[-1] if secs else None,
        },
    }


def to_markdown(results: list[CaseResult], summary: dict[str, Any], meta: dict) -> str:
    o = summary["overall"]
    lines = [
        f"# Assistant eval — {meta.get('provider')} / {meta.get('model')}",
        "",
        f"Run {meta.get('started_at')} · {o['passed']}/{o['total']} cases pass "
        f"(hard checks) · median {summary['latency_s']['median']} s · usage "
        f"{summary['usage']}",
        "",
        "| Language | Pass |",
        "|---|---|",
        *[
            f"| {k} | {v['passed']}/{v['total']} |"
            for k, v in summary["by_language"].items()
        ],
        "",
        "| Situation | Pass |",
        "|---|---|",
        *[
            f"| {k} | {v['passed']}/{v['total']} |"
            for k, v in summary["by_group"].items()
        ],
        "",
        "Soft checks (not counted): "
        + ", ".join(
            f"{k} {v['ok']}/{v['total']}" for k, v in summary["soft_checks"].items()
        ),
        "",
        "## Cases",
    ]
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        lines += [
            "",
            f"### {mark} `{r.id}` ({r.seconds} s)",
            f"**Q:** {r.question}",
            "",
            f"**Tools:** {', '.join(r.tools_called) or '—'} · **pending:** {r.pending_action or '—'}",
            "",
            "**A:**",
            "",
            *[f"> {line}" for line in (r.reply or "(empty)").splitlines()],
            "",
            *[
                f"- {'✓' if c.ok else '✗'} {c.name}{'' if c.hard else ' (soft)'}"
                + (f" — {c.detail}" if c.detail and not c.ok else "")
                for c in r.checks
            ],
        ]
    return "\n".join(lines) + "\n"


def result_dict(r: CaseResult) -> dict[str, Any]:
    d = asdict(r)
    d["passed"] = r.passed
    return d


def catalog_tool_names() -> set[str]:
    return {t["name"] for t in registry.catalog()}
