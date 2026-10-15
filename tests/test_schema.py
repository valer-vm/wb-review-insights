"""Тесты проверки JSON, которые пишут субагенты."""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import schema
from collect import select_reviews

FIXTURES = Path(__file__).parent / "fixtures"
RESULTS_DIR = FIXTURES / "results"
NM = 368686013


def load(name):
    return json.loads((RESULTS_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def csv():
    fb = json.loads((FIXTURES / "feedbacks_sample.json").read_text(encoding="utf-8"))["feedbacks"]
    return select_reviews(fb, neg_limit=150, pos_limit=100, months=0, now=datetime(2026, 10, 15, tzinfo=timezone.utc))


@pytest.fixture
def old_csv(csv):
    """CSV до 0.3.0 — без колонки id; к нему относятся результаты из fixtures/results."""
    return csv.drop(columns="id")


@pytest.fixture
def agent():
    """Результат в том виде, как его пишет review-analyst: reviews и id, без mentions и rating."""
    return json.loads((FIXTURES / f"agent_{NM}.json").read_text(encoding="utf-8"))


@pytest.fixture
def result():
    return load(f"{NM}.json")


@pytest.fixture
def results():
    return schema.load_results(RESULTS_DIR)


@pytest.fixture
def summary():
    return load("summary.json")


# ---------------------------------------------------------------- цитаты

@pytest.mark.parametrize("quote", [
    "Очень тугая, уши больно",
    "очень   ТУГАЯ, уши больно",          # регистр и пробелы
    "«Очень тугая» … хотя по размеру",    # кавычки и пропуск
    "Очень тугая... по размеру",
])
def test_quote_found(quote):
    assert schema.quote_found(quote, [schema.norm("Очень тугая, уши больно, хотя по размеру")])


@pytest.mark.parametrize("quote", ["Очень широкая", "", "…", "по размеру … Очень тугая x"])
def test_quote_not_found(quote):
    assert not schema.quote_found(quote, [schema.norm("Очень тугая, уши больно, хотя по размеру")])


def test_norm_yo():
    assert schema.norm("Тёплая") == schema.norm("теплая")


# ---------------------------------------------------------------- result

def test_valid_result(result, old_csv):
    assert schema.validate_result(result, old_csv) == []
    assert schema.validate_result(load("111222333.json")) == []


def test_result_invented_quote(result, old_csv):
    result["complaints"][0]["examples"][0]["quote"] = "Шапка развалилась через день"
    errs = schema.validate_result(result, old_csv)
    assert len(errs) == 1 and "нет в отзывах" in errs[0]


def test_result_quote_from_any_field(result, old_csv):
    # жалоба из отзыва на 4★ без «Недостатков» (bucket = pos) — допустима
    result["complaints"][2]["examples"][0]["quote"] = "Грубая ткань, стоит колом"
    assert schema.validate_result(result, old_csv) == []


def test_result_counts_must_match_csv(result, old_csv):
    result["reviews_analyzed"]["negative"] = 99
    assert any("в CSV 12" in e for e in schema.validate_result(result, old_csv))


def test_result_mentions_over_limit(result, old_csv):
    result["complaints"][0]["mentions"] = 25
    errs = schema.validate_result(result, old_csv)
    assert any("больше числа отзывов (24)" in e for e in errs)


def test_result_sorting_and_duplicates(result):
    result["complaints"].reverse()
    result["praise"].append(copy.deepcopy(result["praise"][0]) | {"mentions": 1})
    errs = schema.validate_result(result)
    assert any("по убыванию" in e for e in errs)
    assert any("повторяется" in e for e in errs)


@pytest.mark.parametrize("mutate, expected", [
    (lambda r: r.update(article="368686013"), "article"),
    (lambda r: r.pop("reviews_analyzed"), "reviews_analyzed"),
    (lambda r: r.update(complaints=None), "complaints: нужен список"),
    (lambda r: r["complaints"][0].update(examples=[]), "examples"),
    (lambda r: r["complaints"][0].update(examples=[{"quote": "x", "rating": 9}] * 1), "rating: 1–5"),
    (lambda r: r["complaints"][0].update(mentions=0), "mentions"),
    (lambda r: r["complaints"][0].update(theme="x" * 81), "theme"),
    (lambda r: r["praise"][0].update(description=""), "description"),
])
def test_result_structure_errors(result, mutate, expected):
    mutate(result)
    assert any(expected in e for e in schema.validate_result(result))


def test_result_not_object():
    assert schema.validate_result([]) == ["корень JSON должен быть объектом"]


def test_empty_lists_are_fine():
    data = {"article": 1, "name": "", "brand": "", "reviews_analyzed": {"negative": 0, "positive": 0},
            "complaints": [], "praise": []}
    assert schema.validate_result(data) == []


def test_old_result_needs_reviews_with_new_csv(result, csv):
    errs = schema.validate_result(result, csv)
    assert errs and all("reviews" in e for e in errs)


# ---------------------------------------------------------------- finalize_result

def test_finalize_counts_sorts_and_fills(agent, csv):
    notes = schema.finalize_result(agent, csv)
    assert [t["mentions"] for t in agent["complaints"]] == [4, 3, 2]
    assert [t["mentions"] for t in agent["praise"]] == [4, 3]
    assert agent["complaints"][0]["examples"][1] == {"id": 9, "quote": "После первой же стирки,краска отошла от ткани",
                                                     "rating": 1}
    assert any("отсортированы" in n for n in notes)
    assert schema.validate_result(agent, csv) == []
    assert schema.finalize_result(agent, csv) == []  # повторный прогон ничего не меняет


def test_finalize_fixes_example_id_and_adds_review(agent, csv):
    theme = agent["complaints"][1]           # «Маломерит, давит», reviews [3, 8, 10]
    theme["examples"][0]["id"] = 5           # цитата на самом деле из отзыва 3
    theme["reviews"] = [8, 10]
    notes = schema.finalize_result(agent, csv)
    assert theme["examples"][0]["id"] == 3 and theme["examples"][0]["rating"] == 4
    assert sorted(theme["reviews"]) == [3, 8, 10] and theme["mentions"] == 3
    assert any("5 → 3" in n for n in notes) and any("добавлен" in n for n in notes)
    assert schema.validate_result(agent, csv) == []


def test_finalize_drops_unknown_and_duplicate_ids(agent, csv):
    agent["praise"][0]["reviews"] = [17, 17, 20, 999, 21]
    schema.finalize_result(agent, csv)
    t = next(t for t in agent["praise"] if t["theme"] == "Подходит на большую голову")
    assert t["reviews"] == [17, 20, 21] and t["mentions"] == 3


def test_finalize_drops_rare_themes_in_big_group(agent, csv, monkeypatch):
    monkeypatch.setattr(schema, "SMALL_GROUP", 5)    # в группе 12 отзывов — нужно от 2 упоминаний
    agent["complaints"][0]["reviews"] = [4]
    notes = schema.finalize_result(agent, csv)
    assert "Грубая ткань" not in [t["theme"] for t in agent["complaints"]]
    assert any("убрана" in n for n in notes)


def test_finalize_keeps_rare_themes_in_small_group(agent, csv):
    agent["complaints"][0]["reviews"] = [4]
    schema.finalize_result(agent, csv)
    assert "Грубая ткань" in [t["theme"] for t in agent["complaints"]]


def test_finalize_trims_themes(agent, csv, monkeypatch):
    monkeypatch.setattr(schema, "MAX_THEMES", 2)
    schema.finalize_result(agent, csv)
    assert [t["theme"] for t in agent["complaints"]] == ["Выгорает и теряет цвет после стирок", "Маломерит, давит"]


def test_finalize_fixes_reviews_analyzed(agent, csv):
    agent["reviews_analyzed"] = {"negative": 99, "positive": 1}
    schema.finalize_result(agent, csv)
    assert agent["reviews_analyzed"] == {"negative": 12, "positive": 12}


def test_finalize_leaves_invented_quote_to_validate(agent, csv):
    agent["praise"][0]["examples"][0]["quote"] = "Шапка развалилась через день"
    schema.finalize_result(agent, csv)
    errs = schema.validate_result(agent, csv)
    assert len(errs) == 1 and "нет в отзывах" in errs[0]


@pytest.mark.parametrize("mutate, expected", [
    (lambda t: t.update(reviews=[]), "reviews: нужен непустой список"),
    (lambda t: t.update(reviews=[3, 999]), "нет отзывов с id [999]"),
    (lambda t: t.update(reviews=[3, 3]), "повторяются"),
    (lambda t: t.update(mentions=7), "а отзывов в reviews"),
    (lambda t: t["examples"][0].update(id=5), "нет в отзыве id 5"),
])
def test_validate_reviews_errors(agent, csv, mutate, expected):
    schema.finalize_result(agent, csv)
    mutate(next(t for t in agent["complaints"] if t["theme"] == "Маломерит, давит"))
    assert any(expected in e for e in schema.validate_result(agent, csv))


def test_cli_fixes_agent_file(tmp_path, csv, capsys):
    csv_path = tmp_path / f"{NM}.csv"
    csv.to_csv(csv_path, index=False)
    res = tmp_path / f"{NM}.json"
    res.write_text((FIXTURES / f"agent_{NM}.json").read_text(encoding="utf-8"), encoding="utf-8")
    assert schema.main(["result", str(res), "--csv", str(csv_path), "--no-fix"]) == 1
    assert schema.main(["result", str(res), "--csv", str(csv_path)]) == 0
    assert "исправлено автоматически" in capsys.readouterr().out
    data = json.loads(res.read_text(encoding="utf-8"))
    assert [t["mentions"] for t in data["complaints"]] == [4, 3, 2]


# ---------------------------------------------------------------- summary

def test_valid_summary(summary, results):
    assert sorted(results) == [111222333, NM]
    assert schema.validate_summary(summary, results) == []


def test_summary_missing_theme(summary, results):
    summary["complaints"].pop()  # «Грубая ткань» никуда не попала
    errs = schema.validate_summary(summary, results)
    assert any("«грубая ткань»" in e and "ни в одну группу" in e for e in errs)


def test_summary_theme_used_twice(summary, results):
    summary["complaints"][2]["sources"].append({"article": NM, "theme": "Маломерит, давит"})
    assert any("уже входит в группу" in e for e in schema.validate_summary(summary, results))


def test_summary_unknown_source(summary, results):
    summary["praise"][1]["sources"][0]["theme"] = "Красивый цвет"
    errs = schema.validate_summary(summary, results)
    assert any("нет в results" in e for e in errs)


def test_summary_quote_must_come_from_results(summary, results):
    summary["complaints"][0]["examples"][0]["quote"] = "Ужасно маломерит"
    assert any("не взята из примеров" in e for e in schema.validate_summary(summary, results))


def test_summary_based_on_must_exist(summary, results):
    summary["recommendations"]["improve_product"][0]["based_on"] = ["Колется"]
    assert any("«Колется»" in e for e in schema.validate_summary(summary, results))


def test_summary_requires_chat_and_recs(summary, results):
    del summary["chat_summary"]
    summary["recommendations"] = []
    errs = schema.validate_summary(summary, results)
    assert any("chat_summary" in e for e in errs) and any("recommendations" in e for e in errs)


def test_summary_stats(summary, results):
    stats = schema.summary_stats(summary["complaints"][0], results, "complaints")
    assert stats == {"mentions": 3 + 6, "competitors": 2, "articles": [NM, 111222333], "niche_wide": True}
    single = schema.summary_stats(summary["complaints"][2], results, "complaints")
    assert single["competitors"] == 1 and not single["niche_wide"]


# ---------------------------------------------------------------- CLI

def test_cli(tmp_path, old_csv, capsys):
    csv_path = tmp_path / f"{NM}.csv"
    old_csv.to_csv(csv_path, index=False)
    res = tmp_path / f"{NM}.json"
    res.write_text((RESULTS_DIR / f"{NM}.json").read_text(encoding="utf-8"), encoding="utf-8")
    assert schema.main(["result", str(res), "--csv", str(csv_path)]) == 0
    assert schema.main(["summary", str(RESULTS_DIR / "summary.json")]) == 0

    assert schema.main(["result", str(tmp_path / "nope.json")]) == 1
    bad = tmp_path / "bad.json"
    bad.write_text("{не json", encoding="utf-8")
    assert schema.main(["result", str(bad)]) == 1
    out = capsys.readouterr().out
    assert "файл не создан" in out and "невалидный JSON" in out
