"""Тесты dashboard.py: настройки, поиск сравнимых запусков, динамика, HTML."""
import copy
import json
import re
from pathlib import Path

import pytest

import dashboard
from dashboard import ConfigError

RESULTS_DIR = Path(__file__).parent / "fixtures" / "results"
NM, OTHER = 368686013, 111222333


def fixture(name):
    return json.loads((RESULTS_DIR / name).read_text(encoding="utf-8"))


def make_run(list_dir: Path, name: str, articles=(NM, OTHER), tweak=None, summary=True):
    """Папка запуска из фикстур. tweak(results, summary) может поменять данные."""
    run = list_dir / name
    (run / "data").mkdir(parents=True)
    (run / "results").mkdir()
    results = {a: fixture(f"{a}.json") for a in (NM, OTHER)}
    summ = fixture("summary.json")
    if tweak:
        tweak(results, summ)
    status = []
    for a in articles:
        if a in results:
            (run / "results" / f"{a}.json").write_text(json.dumps(results[a], ensure_ascii=False), encoding="utf-8")
            meta = {"article": a, "brand": results[a]["brand"], "rating": "4.5", "total_feedbacks": 100}
            (run / "data" / f"{a}.meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            status.append({"input": str(a), "article": a, "status": "ok"})
        else:
            status.append({"input": str(a), "article": a, "status": "error", "message": "не найден"})
    (run / "data" / "_status.json").write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    if summary:
        (run / "results" / "summary.json").write_text(json.dumps(summ, ensure_ascii=False), encoding="utf-8")
    return run


def set_mentions(results, art, theme, value):
    for t in results[art]["complaints"]:
        if t["theme"] == theme:
            t["mentions"] = value
    results[art]["complaints"].sort(key=lambda t: -t["mentions"])


@pytest.fixture
def root(tmp_path):
    return tmp_path / "wb-insights"


@pytest.fixture
def cfg(root):
    return dashboard.load_config(root, log=lambda s: None)


# ---------------------------------------------------------------- настройки

def test_config_created_with_defaults(root):
    logs = []
    cfg = dashboard.load_config(root, log=logs.append)
    assert (root / "dashboard.toml").exists() and "Создан файл настроек" in logs[0]
    assert cfg["blocks"]["visible"] == list(dashboard.BLOCKS)
    assert cfg["history"]["metric"] == "per100" and cfg["aliases"] == {}


def test_config_user_values(root):
    root.mkdir()
    (root / "dashboard.toml").write_text("""
[blocks]
order = ["history", "complaints", "nonsense"]
hidden = ["complaints"]
[top]
praise = 3
[history]
metric = "absolute"
[aliases]
"Маломерит" = ["Размер меньше заявленного"]
""", encoding="utf-8")
    logs = []
    cfg = dashboard.load_config(root, log=logs.append)
    assert cfg["blocks"]["visible"] == ["history"]
    assert cfg["top"] == {"complaints": 10, "praise": 3}
    assert cfg["history"]["metric"] == "absolute"
    assert any("nonsense" in m for m in logs)


@pytest.mark.parametrize("toml, msg", [
    ("[top]\ncomplaints = 0", "complaints"),
    ('[history]\nmetric = "percent"', "metric"),
    ('[filters]\nniche_only = "да"', "niche_only"),
    ('[aliases]\n"А" = "Б"', "aliases"),
    ("[top\n", "ошибка в"),
])
def test_config_errors(root, toml, msg):
    root.mkdir()
    (root / "dashboard.toml").write_text(toml, encoding="utf-8")
    with pytest.raises(ConfigError, match=msg):
        dashboard.load_config(root, log=lambda s: None)


def test_aliases():
    al = dashboard.Aliases({"Маломерит": ["Размер меньше заявленного", "Тугая"]})
    assert al("размер  меньше заявленного") == "Маломерит"
    assert al("Маломерит") == "Маломерит"
    assert al("Линяет") == "Линяет"


# ---------------------------------------------------------------- запуски

def test_find_and_compare_runs(root):
    lst = root / "шапки"
    make_run(lst, "2026-08-01_100000", articles=(NM,))            # другой набор
    make_run(lst, "2026-09-01_100000")
    (lst / "2026-09-15_100000").mkdir()                            # пустая папка — не запуск
    make_run(lst, "2026-10-01_100000")
    runs = dashboard.find_runs(lst)
    assert [r.path.name for r in runs] == ["2026-08-01_100000", "2026-09-01_100000", "2026-10-01_100000"]
    comp, skipped = dashboard.comparable(runs, max_runs=12)
    assert [r.path.name for r in comp] == ["2026-09-01_100000", "2026-10-01_100000"] and skipped == 1
    assert dashboard.comparable(runs, max_runs=1)[0][0].path.name == "2026-10-01_100000"
    assert runs[-1].label == "01.10.2026 10:00"


def test_failed_article_still_part_of_set(root):
    # товар, который не собрался, всё равно входит в набор: список тот же
    lst = root / "шапки"
    make_run(lst, "2026-09-01_100000", articles=(NM, OTHER, 555))
    make_run(lst, "2026-10-01_100000", articles=(NM, OTHER, 555))
    comp, skipped = dashboard.comparable(dashboard.find_runs(lst), 12)
    assert len(comp) == 2 and skipped == 0


# ---------------------------------------------------------------- снимок

def test_snapshot(root):
    run = make_run(root / "шапки", "2026-10-01_100000", articles=(NM, OTHER, 555))
    snap = dashboard.snapshot(dashboard.find_runs(root / "шапки")[0], dashboard.Aliases({}))
    first = snap["complaints"][0]
    assert first["theme"] == "Маломерит"
    assert first["per_article"] == {str(NM): 3, str(OTHER): 6}
    assert first["mentions"] == 9 and first["competitors"] == 2
    assert snap["total_negative"] == 12 + 20
    assert [c["article"] for c in snap["competitors"]] == [NM, OTHER]
    assert snap["competitors"][1]["top_complaints"][0] == {"theme": "Размер меньше заявленного", "mentions": 6}
    assert snap["failed"] == [{"article": 555, "input": "555", "message": "не найден"}]
    # цитаты отнесены к группам сводки
    q = next(x for x in snap["quotes"] if x["quote"].startswith("берите на размер"))
    assert q["theme"] == "Маломерит" and q["section"] == "complaints" and q["article"] == OTHER
    assert snap["recommendations"]["improve_product"][0]["title"] == "Проверить лекала по размерам"
    assert run.exists()


def test_snapshot_without_summary_uses_aliases(root):
    make_run(root / "шапки", "2026-10-01_100000", summary=False)
    al = dashboard.Aliases({"Маломерит": ["Маломерит, давит", "Размер меньше заявленного"]})
    snap = dashboard.snapshot(dashboard.find_runs(root / "шапки")[0], al)
    g = next(g for g in snap["complaints"] if g["theme"] == "Маломерит")
    assert g["mentions"] == 9 and g["competitors"] == 2
    assert snap["recommendations"] is None and snap["chat_summary"] is None


# ---------------------------------------------------------------- динамика

@pytest.fixture
def history_runs(root):
    lst = root / "шапки"

    def older(results, summary):
        set_mentions(results, NM, "Маломерит, давит", 1)
        # в старом запуске не было темы «Грубая ткань»
        results[NM]["complaints"] = [t for t in results[NM]["complaints"] if t["theme"] != "Грубая ткань"]
        summary["complaints"] = [g for g in summary["complaints"] if g["theme"] != "Грубая ткань"]

    def middle(results, summary):
        older(results, summary)
        set_mentions(results, NM, "Маломерит, давит", 2)
        # в среднем запуске была тема, которой потом не стало
        results[OTHER]["complaints"].append({"theme": "Нет бирки", "description": "x", "mentions": 1,
                                             "examples": [{"quote": "без бирки", "rating": 2}]})
        summary["complaints"].append({"theme": "Нет бирки", "description": "x",
                                      "sources": [{"article": OTHER, "theme": "Нет бирки"}],
                                      "examples": [{"article": OTHER, "quote": "без бирки", "rating": 2}]})

    make_run(lst, "2026-08-01_100000", tweak=older)
    make_run(lst, "2026-09-01_100000", tweak=middle)
    make_run(lst, "2026-10-01_100000")
    return lst


def test_history_absolute(history_runs, cfg):
    cfg["history"]["metric"] = "absolute"
    h = dashboard.build_data(history_runs, cfg, log=lambda s: None)["history"]
    assert h["runs"] == ["01.08.2026 10:00", "01.09.2026 10:00", "01.10.2026 10:00"]
    s = {x["theme"]: x for x in h["series"]}
    assert s["Маломерит"]["values"] == [7, 8, 9] and s["Маломерит"]["state"] == "up" and s["Маломерит"]["delta"] == 1
    assert s["Грубая ткань"]["values"] == [None, None, 1] and s["Грубая ткань"]["state"] == "new"
    assert s["Нет бирки"]["values"] == [None, 1, None] and s["Нет бирки"]["state"] == "gone"
    assert h["series"][-1]["theme"] == "Нет бирки"  # ушедшие — в конце
    assert s["Теряет цвет после стирки"]["state"] == "same"


def test_history_per100(history_runs, cfg):
    h = dashboard.build_data(history_runs, cfg, log=lambda s: None)["history"]
    s = {x["theme"]: x for x in h["series"]}
    assert s["Маломерит"]["values"][-1] == round(100 * 9 / 32, 1)


def test_history_single_run(root, cfg):
    make_run(root / "шапки", "2026-10-01_100000")
    h = dashboard.build_data(root / "шапки", cfg, log=lambda s: None)["history"]
    assert len(h["runs"]) == 1 and all(x["state"] == "single" for x in h["series"])


def test_history_respects_aliases(root, cfg):
    lst = root / "шапки"

    def renamed(results, summary):
        summary["complaints"][0]["theme"] = "Размер меньше заявленного"
        for r in summary["recommendations"].values():
            for x in r:
                x["based_on"] = ["Размер меньше заявленного" if b == "Маломерит" else b for b in x["based_on"]]

    make_run(lst, "2026-09-01_100000", tweak=renamed)
    make_run(lst, "2026-10-01_100000")
    s = {x["theme"]: x for x in dashboard.build_data(lst, cfg, log=lambda s: None)["history"]["series"]}
    assert "Маломерит" in s and s["Маломерит"]["state"] == "new"   # без синонима — разные темы
    cfg["aliases"] = {"Маломерит": ["Размер меньше заявленного"]}
    s = {x["theme"]: x for x in dashboard.build_data(lst, cfg, log=lambda s: None)["history"]["series"]}
    assert s["Маломерит"]["state"] == "same" and "Размер меньше заявленного" not in s


# ---------------------------------------------------------------- HTML и CLI

def test_render_embeds_data(history_runs, cfg):
    cfg["blocks"]["visible"] = ["complaints", "quotes"]
    data = dashboard.build_data(history_runs, cfg, log=lambda s: None)
    data["chat_summary"] = "опасно </script><script>alert(1)</script>"
    html = dashboard.render(data)
    assert "<title>Отзывы конкурентов — шапки</title>" in html
    assert "</script><script>alert" not in html
    payload = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1)
    parsed = json.loads(payload)
    assert parsed["config"]["blocks"] == ["complaints", "quotes"]
    assert parsed["chat_summary"].startswith("опасно </script>")


def test_main(history_runs, root, capsys):
    assert dashboard.main([str(history_runs)]) == 0
    out = capsys.readouterr().out
    assert (history_runs / "dashboard.html").exists() and (root / "dashboard.toml").exists()
    assert "сравнимых запусков: 3" in out

    assert dashboard.main([str(root / "варежки")]) == 2
    assert "Есть списки: шапки" in capsys.readouterr().err

    (root / "dashboard.toml").write_text('[history]\nmetric = "x"', encoding="utf-8")
    assert dashboard.main([str(history_runs)]) == 1


def test_no_runs(root, cfg):
    (root / "пусто").mkdir(parents=True)
    with pytest.raises(ConfigError, match="нет ни одного запуска"):
        dashboard.build_data(root / "пусто", cfg)


# ---------------------------------------------------------------- словарь тем

def test_vocabulary(history_runs, cfg, capsys):
    current = make_run(history_runs, "2026-11-01_100000")
    v = dashboard.vocabulary(history_runs, current, cfg)
    assert v["run"] == "2026-10-01_100000"                     # последний прошлый, не текущий
    assert v["complaints"] == ["Маломерит", "Теряет цвет после стирки", "Грубая ткань"]
    assert v["praise"] == ["Тёплая и плотная", "Подходит на большую голову"]

    cfg["aliases"] = {"Размер": ["Маломерит"]}                # синонимы применяются
    assert dashboard.vocabulary(history_runs, current, cfg)["complaints"][0] == "Размер"

    assert dashboard.main([str(history_runs), "--vocab-for", str(current)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Словарь тем из прошлого запуска 2026-10-01_100000")
    assert "Жалобы: Маломерит; Теряет цвет после стирки; Грубая ткань" in out
    assert not (history_runs / "dashboard.html").exists()


def test_vocabulary_needs_same_articles(root, cfg, capsys):
    lst = root / "шапки"
    make_run(lst, "2026-09-01_100000", articles=(NM,))
    current = make_run(lst, "2026-10-01_100000")
    assert dashboard.vocabulary(lst, current, cfg) is None
    assert dashboard.vocabulary_text(None).startswith("Прошлых сравнимых запусков нет")


def test_vocabulary_skips_runs_without_summary(root, cfg):
    lst = root / "шапки"
    make_run(lst, "2026-08-01_100000")
    make_run(lst, "2026-09-01_100000", summary=False)
    current = make_run(lst, "2026-10-01_100000")
    assert dashboard.vocabulary(lst, current, cfg)["run"] == "2026-08-01_100000"
