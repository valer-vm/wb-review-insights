"""Тесты reuse.py: результаты прошлых запусков при неизменных отзывах."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import reuse
import schema
from collect import select_reviews

FIXTURES = Path(__file__).parent / "fixtures"
NM = 368686013
NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


@pytest.fixture
def csv():
    fb = json.loads((FIXTURES / "feedbacks_sample.json").read_text(encoding="utf-8"))["feedbacks"]
    return select_reviews(fb, neg_limit=150, pos_limit=100, months=0, now=NOW)


@pytest.fixture
def result(csv):
    data = json.loads((FIXTURES / f"agent_{NM}.json").read_text(encoding="utf-8"))
    schema.finalize_result(data, csv)
    return data


def summary_for(result):
    """Простая валидная сводка: каждая тема — своя группа."""
    def groups(section):
        return [{"theme": t["theme"], "description": t["description"],
                 "sources": [{"article": NM, "theme": t["theme"]}],
                 "examples": [{"article": NM, "quote": t["examples"][0]["quote"], "rating": t["examples"][0]["rating"]}]}
                for t in result[section]]
    return {"complaints": groups("complaints"), "praise": groups("praise"),
            "recommendations": {"improve_product": [], "highlight_in_card": []}, "chat_summary": "итог"}


def make_run(list_dir, name, csv, result=None, summary=None, meta=None):
    run = list_dir / name
    (run / "data").mkdir(parents=True)
    (run / "results").mkdir()
    csv.to_csv(run / "data" / f"{NM}.csv", index=False)
    status = [{"input": str(NM), "article": NM, "status": "ok"}]
    (run / "data" / "_status.json").write_text(json.dumps(status), encoding="utf-8")
    (run / "data" / f"{NM}.meta.json").write_text(
        json.dumps(meta or {"article": NM, "name": "Шапка", "brand": "BODO"}, ensure_ascii=False), encoding="utf-8")
    for fname, data in ((f"{NM}.json", result), ("summary.json", summary)):
        if data is not None:
            (run / "results" / fname).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return run


@pytest.fixture
def list_dir(tmp_path):
    return tmp_path / "wb-insights" / "шапки"


def test_reuses_result_and_summary(list_dir, csv, result):
    make_run(list_dir, "2026-10-01_100000", csv, result, summary_for(result))
    run = make_run(list_dir, "2026-10-15_100000", csv, meta={"article": NM, "name": "Новое имя", "brand": "BODO"})
    todo, summary_from = reuse.reuse(list_dir, run, log=lambda s: None)
    assert todo == [] and summary_from.name == "2026-10-01_100000"
    copied = json.loads((run / "results" / f"{NM}.json").read_text(encoding="utf-8"))
    assert copied["name"] == "Новое имя" and copied["complaints"] == result["complaints"]
    assert (run / "results" / "summary.json").exists()


def test_changed_reviews_need_analysis(list_dir, csv, result):
    make_run(list_dir, "2026-10-01_100000", csv, result, summary_for(result))
    changed = csv.copy()
    changed.loc[0, "text"] = "Новый отзыв"
    run = make_run(list_dir, "2026-10-15_100000", changed)
    assert reuse.reuse(list_dir, run, log=lambda s: None) == ([NM], None)
    assert not (run / "results" / f"{NM}.json").exists()


def test_old_format_result_is_not_reused(list_dir, csv):
    old = json.loads((FIXTURES / "results" / f"{NM}.json").read_text(encoding="utf-8"))
    make_run(list_dir, "2026-10-01_100000", csv.drop(columns="id"), old)
    run = make_run(list_dir, "2026-10-15_100000", csv)
    assert reuse.reuse(list_dir, run, log=lambda s: None) == ([NM], None)


def test_old_csv_without_id_matches(tmp_path, csv):
    csv.to_csv(tmp_path / "new.csv", index=False)
    csv.drop(columns="id").to_csv(tmp_path / "old.csv", index=False)
    assert reuse.fingerprint(tmp_path / "new.csv") == reuse.fingerprint(tmp_path / "old.csv")


def test_newest_matching_run_wins_and_later_runs_ignored(list_dir, csv, result):
    make_run(list_dir, "2026-10-01_100000", csv, result)
    make_run(list_dir, "2026-10-05_100000", csv, result | {"brand": "из 05"})
    run = make_run(list_dir, "2026-10-10_100000", csv)
    make_run(list_dir, "2026-10-20_100000", csv, result | {"brand": "из будущего"})
    todo, summary_from = reuse.reuse(list_dir, run, log=lambda s: None)
    assert todo == [] and summary_from is None   # у прошлого запуска нет сводки
    assert json.loads((run / "results" / f"{NM}.json").read_text(encoding="utf-8"))["brand"] == "BODO"


def test_invalid_previous_result_skipped(list_dir, csv, result):
    bad = json.loads(json.dumps(result))
    bad["complaints"][0]["mentions"] = 99
    make_run(list_dir, "2026-10-01_100000", csv, bad)
    run = make_run(list_dir, "2026-10-15_100000", csv)
    assert reuse.reuse(list_dir, run, log=lambda s: None) == ([NM], None)


def test_cli(list_dir, csv, result, capsys):
    make_run(list_dir, "2026-10-01_100000", csv, result, summary_for(result))
    run = make_run(list_dir, "2026-10-15_100000", csv)
    assert reuse.main([str(list_dir), str(run)]) == 0
    out = capsys.readouterr().out
    assert "Взято из прошлых запусков: 1 из 1" in out
    assert "Нужен анализ: нет" in out and "Сводка: взята из 2026-10-01_100000" in out
    assert reuse.main([str(list_dir), str(list_dir / "nope")]) == 2
