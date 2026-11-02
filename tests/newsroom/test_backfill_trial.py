"""Backfill trial: matching on the real starting lists, text evidence, comparison and the script (fake model, no cost)."""
import csv
import json
import sys

import pytest

from src.newsroom import backfill_trial as bt
from src.newsroom import resolve as rs
from src.newsroom.seed import build_plan


@pytest.fixture(scope="module")
def index():
    return bt.ListIndex.from_plan(build_plan())


def test_split_cell_and_label_names():
    assert bt.split_cell("Aldar,  Emaar ,Not stated") == ["Aldar", "Emaar"]
    assert bt.split_cell(None) == [] and bt.split_cell("Not applicable") == []
    row = {"Company / Developer": "Aldar", "Project": "Yas Island", "Location": "Abu Dhabi, Dubai"}
    assert bt.label_names(row) == (["Aldar", "Yas Island"], ["Abu Dhabi", "Dubai"])


def test_resolution_follows_the_live_rules(index):
    emaar = index.resolve("organisation", "Emaar")
    assert (emaar.outcome, emaar.entity_name, emaar.matched_alias) == (rs.ACTIVE, "Emaar Properties", "Emaar")
    assert index.resolve("organisation", "Wasl").outcome == rs.AMBIGUOUS
    assert index.resolve("organisation", "Wasl Group").outcome == rs.STAGED
    assert index.resolve("organisation", "waslgroup").outcome == rs.IGNORED
    assert index.resolve("organisation", "www.emaar.com").outcome == rs.HANDLE
    # A community given as a project is found in the place list; a company given as a place in the company list
    yas = index.resolve("organisation", "Yas Island")
    assert (yas.outcome, yas.field, yas.entity_name, yas.given_as) == (rs.ACTIVE, "location", "Yas Island", "organisation")
    kezad = index.resolve("location", "KEZAD")
    assert (kezad.outcome, kezad.field, kezad.given_as) == (rs.ACTIVE, "organisation", "location")
    assert index.resolve("organisation", "Emaar").given_as is None
    assert index.resolve("organisation", "Nobody Ltd").outcome == rs.UNKNOWN


def test_auto_links_only_switched_on_and_once_per_entity(index):
    results = bt.resolve_item(index, ["Emaar", "Emaar Properties", "Wasl Group", "Nobody Ltd"], ["Dubai", "dubai"])
    assert len(results) == 5  # "dubai" is the same name as "Dubai"
    links = bt.auto_links(results)
    assert sorted(r.entity_name for r in links.values()) == ["Dubai", "Emaar Properties"]


def test_name_in_text():
    text = "Arada acquired an 80% stake in Thameside West. DAMAC said sales rose. Emaarland is not Emaar."
    assert bt.name_in_text("Stake", text)[0] == bt.OTHER_CASE
    assert bt.name_in_text("ADQ", text) == (bt.ABSENT, "")
    status, snippet = bt.name_in_text("DAMAC", text)
    assert status == bt.EXACT_CASE and "DAMAC said" in snippet
    assert bt.name_in_text("Emaar", text)[0] == bt.EXACT_CASE  # whole word only: "Emaarland" is skipped
    assert bt.name_in_text("Emaarl", text)[0] == bt.ABSENT


def test_compare_and_sample():
    a, b, c = (bt.NameResult("organisation", n, rs.ACTIVE, n) for n in "abc")
    comparison = bt.compare({"a": a, "b": b}, {"b": b, "c": c})
    assert (comparison.agree, comparison.model_only, comparison.labels_only) == (["b"], ["a"], ["c"])
    ids = [str(i) for i in range(100)]
    assert bt.sample_ids(ids, 10, 8) == bt.sample_ids(list(reversed(ids)), 10, 8)
    assert len(bt.sample_ids(ids, 10, 8)) == 10 and bt.sample_ids(ids, None, 8) == sorted(ids)


ARTICLES = [
    {"_id": "a1", "url": "https://x.test/1", "status": "approved",
     "final_output": {"title": "Emaar launches in Dubai", "summary": "Emaar launches a tower.", "content": "Emaar said..."}},
    {"_id": "a2", "url": "https://x.test/2", "status": "approved",
     "final_output": {"title": "Arada buys a stake", "summary": "Arada acquired an 80% stake.", "content": "In Sharjah."}},
]
LABELS = [
    {"Article ID": "a1", "Article Title": "Emaar launches in Dubai", "Company / Developer": "Emaar", "Project": "", "Location": "Dubai"},
    {"Article ID": "a2", "Article Title": "Arada buys a stake", "Company / Developer": "Arada, Stake", "Project": "", "Location": "Sharjah"},
]


@pytest.fixture
def trial_files(tmp_path):
    articles = tmp_path / "articles.json"
    articles.write_text(json.dumps(ARTICLES))
    labels = tmp_path / "labels.csv"
    with open(labels, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(LABELS[0].keys()))
        writer.writeheader()
        writer.writerows(LABELS)
    return articles, labels, tmp_path / "out"


def run_script(monkeypatch, articles, labels, out, *extra):
    from src.scripts import newsroom_backfill_trial as script
    monkeypatch.setattr(sys, "argv", ["trial", "--articles", str(articles), "--labels", str(labels), "--out", str(out), *extra])
    assert script.main() == 0
    return json.loads((out / "summary.json").read_text())


def test_labels_pass_flags_names_not_in_the_text(monkeypatch, trial_files):
    summary = run_script(monkeypatch, *trial_files)["labels_pass"]
    assert summary["articles_with_a_link"] == 2
    # Emaar, Dubai, Arada, Stake, Sharjah: only "Stake" is not written that way in its article
    assert summary["links"] == 5 and summary["links_name_only_in_other_case"] == 1
    entities = {r["entity"]: r for r in csv.DictReader(open(trial_files[2] / "labels_entities.csv"))}
    assert entities["Stake"]["only_other_case"] == "1"


def test_model_pass_uses_the_cache_and_compares(monkeypatch, trial_files):
    from src.configs.settings import settings
    import src.graph.nodes.extract_country as node

    calls = []

    async def fake_extract_country(state):
        calls.append(state.news_article.title)
        names = {"a1": {"companies": ["Emaar"], "projects": [], "places": ["Dubai"]},
                 "a2": {"companies": ["Arada", "Stake"], "projects": [], "places": []}}
        article_id = "a1" if "Emaar" in state.news_article.title else "a2"
        assert "NAMES" in state.active_prompts.country_extraction_system  # the init_db prompt with name rules
        return state.model_copy(update={"newsroom_names": names[article_id]})

    monkeypatch.setattr(node, "extract_country", fake_extract_country)
    monkeypatch.setattr(settings, "NEWSROOM_ENABLED", "true")

    summary = run_script(monkeypatch, *trial_files, "--model-sample", "2")
    assert summary["model_run"]["called"] == 2 and len(calls) == 2
    assert summary["model_pass"]["links_both_agree"] == 4      # Emaar, Dubai, Arada, Stake
    assert summary["model_pass"]["links_only_labels"] == 1     # Sharjah
    assert summary["model_pass"]["model_links_confirmed_by_labels"] == 1.0

    again = run_script(monkeypatch, *trial_files, "--model-sample", "2")
    assert again["model_run"]["already_cached"] == 2 and len(calls) == 2  # nothing paid twice


def test_model_pass_needs_the_newsroom_switch(monkeypatch, trial_files):
    from src.configs.settings import settings
    monkeypatch.setattr(settings, "NEWSROOM_ENABLED", "")
    with pytest.raises(SystemExit):
        run_script(monkeypatch, *trial_files, "--model-sample", "1")
