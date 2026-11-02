"""The read-only country check script, with a fake model (no cost)."""
import asyncio
import importlib.util
from pathlib import Path

import pytest

from src.configs.settings import settings
from src.models.CountryExtractionModel import CountryAndNamesExtractionModel, CountryExtractionModel

SCRIPT = Path(__file__).resolve().parents[1] / "src" / "scripts" / "newsroom_country_check.py"


@pytest.fixture
def check():
    spec = importlib.util.spec_from_file_location("newsroom_country_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeLLM:
    """Countries by title; the "new" run (names model) says Saudi Arabia for the article titled 'Shifted'."""
    def __init__(self, seen):
        self.seen = seen

    def with_structured_output(self, output_model):
        seen = self.seen

        class Structured:
            async def ainvoke(self, messages):
                system, user = messages[0][1], messages[1][1]
                seen.append((output_model, "NAMES (only when" in system))
                if output_model is CountryAndNamesExtractionModel:
                    countries = ["Saudi Arabia"] if "Shifted" in user else ["United Arab Emirates"]
                    return output_model(countries=countries, companies=[], projects=[], places=[])
                return output_model(countries=["United Arab Emirates"])
        return Structured()


class Usage:
    total_cost = 0.0


def _articles():
    return [{"_id": f"a{n}", "url": f"https://example.com/{n}",
             "final_output": {"title": title, "summary": "s", "content": "c", "countries": ["United Arab Emirates"]}}
            for n, title in enumerate(["Emaar in Dubai", "Shifted", "Aldar in Abu Dhabi"])]


def test_old_and_new_runs_and_the_comparison(check, monkeypatch):
    seen = []
    monkeypatch.setattr(type(settings), "get_model", lambda self: FakeLLM(seen))
    monkeypatch.setattr(settings, "NEWSROOM_ENABLED", "false")
    articles = _articles()
    old = asyncio.run(check.run_phase("old", articles, Usage(), 1.0, 2))
    new = asyncio.run(check.run_phase("new", articles, Usage(), 1.0, 2))
    # old = rules 1-6 with the plain country model; new = rules 1-11 with the names model
    assert {s for s in seen[:3]} == {(CountryExtractionModel, False)}
    assert {s for s in seen[3:]} == {(CountryAndNamesExtractionModel, True)}
    rows = {r["title"]: r for r in check.compare(articles, old, new)}
    assert rows["Shifted"]["outcome"] == "DIFFERENT"
    assert rows["Shifted"]["new_countries"] == "Saudi Arabia"
    assert rows["Emaar in Dubai"]["outcome"] == "same"
    assert rows["Emaar in Dubai"]["stored_countries"] == "United Arab Emirates"


def test_the_money_cap_stops_new_calls(check, monkeypatch):
    seen = []
    monkeypatch.setattr(type(settings), "get_model", lambda self: FakeLLM(seen))
    monkeypatch.setattr(settings, "NEWSROOM_ENABLED", settings.NEWSROOM_ENABLED)  # restored afterwards
    spent = Usage()
    spent.total_cost = 0.5
    results = asyncio.run(check.run_phase("old", _articles(), spent, 0.20, 2))
    assert results == {} and seen == []
    rows = check.compare(_articles(), results, {})
    assert {r["outcome"] for r in rows} == {"not run (money cap reached)"}
