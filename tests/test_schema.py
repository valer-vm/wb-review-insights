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

def test_valid_result(result, csv):
    assert schema.validate_result(result, csv) == []
    assert schema.validate_result(load("111222333.json")) == []


def test_result_invented_quote(result, csv):
    result["complaints"][0]["examples"][0]["quote"] = "Шапка развалилась через день"
    errs = schema.validate_result(result, csv)
    assert len(errs) == 1 and "нет в отзывах" in errs[0]


def test_result_quote_from_any_field(result, csv):
    # жалоба из отзыва на 4★ без «Недостатков» (bucket = pos) — допустима
    result["complaints"][2]["examples"][0]["quote"] = "Грубая ткань, стоит колом"
    assert schema.validate_result(result, csv) == []


def test_result_counts_must_match_csv(result, csv):
    result["reviews_analyzed"]["negative"] = 99
    assert any("в CSV 12" in e for e in schema.validate_result(result, csv))


def test_result_mentions_over_limit(result, csv):
    result["complaints"][0]["mentions"] = 25
    errs = schema.validate_result(result, csv)
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

def test_cli(tmp_path, csv, capsys):
    csv_path = tmp_path / f"{NM}.csv"
    csv.to_csv(csv_path, index=False)
    assert schema.main(["result", str(RESULTS_DIR / f"{NM}.json"), "--csv", str(csv_path)]) == 0
    assert schema.main(["summary", str(RESULTS_DIR / "summary.json")]) == 0

    assert schema.main(["result", str(tmp_path / "nope.json")]) == 1
    bad = tmp_path / "bad.json"
    bad.write_text("{не json", encoding="utf-8")
    assert schema.main(["result", str(bad)]) == 1
    out = capsys.readouterr().out
    assert "файл не создан" in out and "невалидный JSON" in out
