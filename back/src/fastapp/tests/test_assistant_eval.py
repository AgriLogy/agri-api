"""The assistant eval harness (#461): case file integrity and the graders.

No database, no network: a scripted provider stands in for the model, so these
tests prove the *checks* catch what they claim to (an invented number, the
wrong script, an executed action, a missing lookup) before anyone pays for a
real run.
"""

from __future__ import annotations

import json

import pytest

from fastapp.assistant.eval import harness
from fastapp.assistant.eval.__main__ import main as eval_main
from fastapp.assistant.providers import LLMResult, ProviderError, ToolCallRecord


class Scripted:
    def __init__(self, text, calls=(), error=None):
        self.text, self.calls, self.error = text, list(calls), error

    def complete(self, *, system, context, history, message, tools, execute):
        if self.error:
            raise self.error
        records = [ToolCallRecord(n, i, execute(n, i)) for n, i in self.calls]
        return LLMResult(
            text=self.text,
            tool_calls=records,
            model="scripted",
            usage={"input_tokens": 100, "output_tokens": 20},
        )


def _case(case_id: str) -> dict:
    return next(c for c in harness.load_cases() if c["id"] == case_id)


def _checks(result) -> dict[str, bool]:
    return {c.name: c.ok for c in result.checks}


class TestCaseFile:
    def test_every_group_has_all_languages_and_a_known_scenario(self):
        cases = harness.load_cases()
        groups = {c["group"] for c in cases}
        assert len(cases) == len(groups) * len(harness.LANGS)
        for c in cases:
            assert c["scenario"] in harness.SCENARIOS
            assert c["lang"] in harness.LANGS
            assert c["question"].strip()

    def test_expected_tools_exist_in_the_catalog(self):
        names = harness.catalog_tool_names()
        for c in harness.load_cases():
            exp = c["expect"]
            for key in ("tools_any", "tools_none"):
                assert set(exp.get(key, [])) <= names, (c["id"], key)
            if exp.get("pending_action"):
                assert exp["pending_action"] in names

    def test_scenarios_answer_every_read_tool_they_name(self):
        names = harness.catalog_tool_names()
        for name, build in harness.SCENARIOS.items():
            assert set(build()) <= names, name


class TestGraders:
    def test_good_french_answer_passes(self):
        reply = (
            "Oui. L'humidité du sol de la zone 1 est à 18.4 % (sous le seuil de "
            "25 %), mesurée il y a 20 minutes. Irriguez 12.5 m³, soit environ 45 min."
        )
        r = harness.run_case(
            _case("irrigate_now.fr"),
            Scripted(reply, [("get_irrigation_advice", {"zone_id": 6})]),
        )
        assert r.passed, r.checks
        assert r.tools_called == ["get_irrigation_advice"]
        assert r.usage == {"input_tokens": 100, "output_tokens": 20}

    def test_reading_age_counts_as_grounded(self):
        r = harness.run_case(
            _case("stale_readings.fr"),
            Scripted(
                "Ces mesures datent d'il y a 72 heures (3 jours) : elles sont "
                "trop anciennes. Quel est l'état du sol aujourd'hui ?",
                [("get_soil_status", {})],
            ),
        )
        assert _checks(r)["grounded_numbers"], r.checks

    def test_invented_number_and_no_lookup_fail(self):
        r = harness.run_case(
            _case("irrigate_now.fr"),
            Scripted("Oui, donnez 30 m³ d'eau pendant 90 minutes."),
        )
        checks = _checks(r)
        assert not r.passed
        assert checks["tools_any"] is False
        assert checks["grounded_numbers"] is False

    def test_thousands_with_spaces_are_one_number(self):
        nums = harness._numbers("Zone de 144 638 m², 150 m³ en 6 h (2026).")
        assert nums == [144638.0, 150.0, 6.0, 2026.0]
        assert harness._numbers("144\u202f638,5 m²") == [144638.5]

    def test_arabizi_digits_are_letters_not_numbers(self):
        reply = "Iyeh, khassek tsa9i daba: l'humidité dyal trab 18.4% w hiya t7t 25%."
        r = harness.run_case(
            _case("irrigate_now.ary_lat"),
            Scripted(reply, [("get_soil_status", {})]),
        )
        assert _checks(r)["grounded_numbers"], r.checks
        assert _checks(r)["script"]

    def test_wrong_script_fails(self):
        r = harness.run_case(
            _case("irrigate_now.ary_ar"),
            Scripted(
                "Oui, il faut irriguer : humidité 18.4 %.", [("get_soil_status", {})]
            ),
        )
        assert _checks(r)["script"] is False

    def test_darija_vs_msa_is_a_soft_check(self):
        msa = "نعم، يجب السقي اليوم لأن رطوبة التربة 18.4٪ أقل من الحد 25٪."
        r = harness.run_case(
            _case("irrigate_now.ary_ar"), Scripted(msa, [("get_soil_status", {})])
        )
        assert r.passed
        assert _checks(r)["darija_markers"] is False

    def test_stale_data_must_not_yield_the_quantity(self):
        reply = "Les mesures datent de 3 jours. Irriguez 12.5 m³."
        r = harness.run_case(
            _case("stale_readings.fr"), Scripted(reply, [("get_soil_status", {})])
        )
        assert _checks(r)["forbid_numbers"] is False
        assert _checks(r)["mentions_any"] is True

    def test_missing_inputs_need_a_question(self):
        r = harness.run_case(
            _case("missing_inputs.fr"),
            Scripted("Je n'ai pas de mesures pour vos oliviers."),
        )
        assert _checks(r)["asks_question"] is False
        r = harness.run_case(
            _case("missing_inputs.fr"),
            Scripted("Je n'ai pas de mesures. Quel est le stade de vos oliviers ?"),
        )
        assert r.passed

    def test_actions_are_only_proposed(self):
        r = harness.run_case(
            _case("create_alert.fr"),
            Scripted(
                "Je vous propose l'alerte ; confirmez-la dans l'application.",
                [("create_alert", {"sensor_key": "soil_moisture_low", "value": 20})],
            ),
        )
        assert r.pending_action == "create_alert"
        assert r.passed, r.checks

        r = harness.run_case(
            _case("open_valve.fr"),
            Scripted(
                "Je crée une alerte.",
                [("create_alert", {"sensor_key": "soil_moisture_low", "value": 20})],
            ),
        )
        assert _checks(r)["pending_action"] is False
        assert _checks(r)["tools_none"] is False

    def test_provider_failure_fails_the_case(self):
        r = harness.run_case(
            _case("active_alerts.fr"),
            Scripted("", error=ProviderError("auth", "bad key")),
        )
        assert r.fallback_reason == "auth"
        assert not r.passed

    def test_summary_and_report(self):
        results = [
            harness.run_case(
                _case("open_valve.fr"),
                Scripted("Ouvrez la page Vannes et pompes pour piloter la vanne."),
            ),
            harness.run_case(_case("open_valve.msa"), Scripted("Ouvrez la page.")),
        ]
        summary = harness.summarize(results)
        assert summary["overall"] == {"passed": 1, "total": 2}
        assert summary["by_language"]["msa"] == {"passed": 0, "total": 1}
        assert summary["usage"] == {"input_tokens": 200, "output_tokens": 40}
        md = harness.to_markdown(results, summary, {"provider": "x", "model": "y"})
        assert "1/2 cases pass" in md and "FAIL `open_valve.msa`" in md


class TestCli:
    def test_dry_run_never_calls_the_provider(self, monkeypatch, capsys):
        monkeypatch.setenv("EVAL_TEST_KEY", "k")

        def boom(*_a, **_k):  # pragma: no cover - must not run
            raise AssertionError("provider built during a dry run")

        monkeypatch.setattr("fastapp.assistant.eval.__main__.build_provider", boom)
        rc = eval_main(
            [
                "--provider",
                "groq",
                "--api-key-env",
                "EVAL_TEST_KEY",
                "--price-in",
                "0.15",
                "--price-out",
                "0.6",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "35 cases" in out and "Dry run" in out and "$" in out

    def test_missing_key_is_refused(self, monkeypatch):
        monkeypatch.delenv("EVAL_TEST_KEY", raising=False)
        with pytest.raises(SystemExit):
            eval_main(["--provider", "groq", "--api-key-env", "EVAL_TEST_KEY"])

    def test_run_writes_reports(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EVAL_TEST_KEY", "k")
        monkeypatch.setattr(
            "fastapp.assistant.eval.__main__.build_provider",
            lambda cfg: Scripted("Ouvrez la page Vannes et pompes."),
        )
        rc = eval_main(
            [
                "--provider",
                "groq",
                "--api-key-env",
                "EVAL_TEST_KEY",
                "--only",
                "open_valve",
                "--lang",
                "fr",
                "--out",
                str(tmp_path),
                "--yes",
            ]
        )
        assert rc == 0
        assert len(list(tmp_path.glob("assistant-eval-*.md"))) == 1
        assert len(list(tmp_path.glob("assistant-eval-*.json"))) == 1


class TestRealDataset:
    """The pseudo-real dataset: real tool outputs over a test farm's readings."""

    @pytest.fixture(scope="class")
    def real(self):
        return harness.get_dataset("real")

    def _case(self, real, case_id: str) -> dict:
        return next(c for c in harness.load_cases(dataset=real) if c["id"] == case_id)

    def test_is_the_default_and_covers_every_scenario(self, real):
        assert harness.DEFAULT_DATASET == "real"
        names = harness.catalog_tool_names()
        scenarios = {c["scenario"] for c in harness.load_cases()}
        assert scenarios <= set(real.scenarios)
        for state, build in real.scenarios.items():
            outputs = build()
            assert {k.split("|")[0] for k in outputs} <= names, state

    def test_states_mean_what_the_cases_test(self, real):
        dry = real.scenarios["dry"]()["get_irrigation_advice"]
        assert dry["recommendation"] == "irrigate"
        assert dry["estimated_water_m3"]
        stale = real.scenarios["stale"]()["get_irrigation_advice"]
        age = real.now - harness.datetime.datetime.fromisoformat(
            stale["last_update_timestamp"]
        )
        assert age.days >= 3
        empty = real.scenarios["empty"]()["get_irrigation_advice"]
        # No readings, no crop: the balance runs on the farm's weather alone,
        # so there is no soil reading, no crop period and no water quantity.
        assert empty["soil_moisture_pct"] is None
        assert empty["kc_period_name"] is None
        assert empty["estimated_water_m3"] is None
        alerts = real.scenarios["alerts"]()["get_active_alerts"]["alerts"]
        assert any(a["last_triggered_at"] for a in alerts)

    def test_placeholders_expand_to_this_farm(self, real):
        dry = real.scenarios["dry"]()["get_irrigation_advice"]
        irrigate = self._case(real, "irrigate_now.fr")
        assert f"{dry['soil_moisture_pct']:g}" in irrigate["mentions_any"]
        assert "{soil}" not in irrigate["mentions_any"]
        stale = self._case(real, "stale_readings.fr")
        assert dry["estimated_water_m3"] in stale["expect"]["forbid_numbers"]
        alerts = self._case(real, "active_alerts.fr")
        assert "{alert_threshold}" not in alerts["mentions_any"]

    def test_grounded_answer_passes_and_invented_figure_fails(self, real):
        dry = real.scenarios["dry"]()["get_irrigation_advice"]
        soil, water = dry["soil_moisture_pct"], dry["estimated_water_m3"]
        good = (
            f"Oui : l'humidité du sol est à {soil} %, sous le seuil de "
            f"{dry['critical_moisture_threshold']:g} %, mesurée il y a 20 minutes. "
            f"Le calcul donne {water:g} m³."
        )
        call = [("get_irrigation_advice", {})]
        r = harness.run_case(
            self._case(real, "irrigate_now.fr"), Scripted(good, call), real
        )
        assert r.passed, r.checks
        r = harness.run_case(
            self._case(real, "irrigate_now.fr"),
            Scripted(good + " Réglez la pression à 1.5 bar.", call),
            real,
        )
        assert _checks(r)["grounded_numbers"] is False

    def test_stale_quantity_is_forbidden(self, real):
        water = real.scenarios["dry"]()["get_irrigation_advice"]["estimated_water_m3"]
        r = harness.run_case(
            self._case(real, "stale_readings.fr"),
            Scripted(
                f"Les mesures datent de 3 jours. Irriguez {water:g} m³.",
                [("get_soil_status", {})],
            ),
            real,
        )
        assert _checks(r)["forbid_numbers"] is False

    def test_trend_answers_the_requested_sensor(self, real):
        tools = harness.ScenarioTools("dry", real)
        et0 = tools("get_sensor_trend", {"sensor_key": "et0", "hours": 24})
        assert et0["key"] == "et0"
        unknown = tools("get_sensor_trend", {"sensor_key": "nope"})
        assert "error" in unknown
        assert tools.calls[0] == {
            "name": "get_sensor_trend",
            "input": {"sensor_key": "et0", "hours": 24},
        }


class TestPacing:
    def test_rate_limited_case_is_retried(self, monkeypatch, tmp_path):
        from fastapp.assistant.eval import __main__ as cli

        attempts = []

        class Flaky:
            def complete(self, **kw):
                attempts.append(1)
                if len(attempts) == 1:
                    raise ProviderError("rate_limit", "slow down")
                return Scripted("Ouvrez la page Vannes et pompes.").complete(**kw)

        sleeps = []
        monkeypatch.setattr(cli.time, "sleep", sleeps.append)
        monkeypatch.setenv("EVAL_TEST_KEY", "k")
        monkeypatch.setattr(cli, "build_provider", lambda cfg: Flaky())
        rc = cli.main(
            [
                "--provider",
                "groq",
                "--api-key-env",
                "EVAL_TEST_KEY",
                "--only",
                "open_valve",
                "--lang",
                "fr",
                "msa",
                "--pace",
                "5",
                "--out",
                str(tmp_path),
                "--yes",
            ]
        )
        report = json.loads(next(tmp_path.glob("assistant-eval-*.json")).read_text())
        assert report["meta"]["data"] == "real"
        assert [r["fallback_reason"] for r in report["results"]] == [None, None]
        assert len(attempts) == 3  # fr: limited then ok; msa: ok
        assert sleeps == [5, 5]  # one retry wait + one gap between cases
        assert rc in (0, 1)
