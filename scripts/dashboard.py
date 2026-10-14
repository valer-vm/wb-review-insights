# /// script
# requires-python = ">=3.11"
# dependencies = ["pandas>=2.1", "openpyxl>=3.1"]
# ///
"""HTML-дашборд по запускам одного списка конкурентов.

  uv run scripts/dashboard.py wb-insights/шапки

Читает все запуски wb-insights/<список>/<дата>/, строит блоки по последнему
и динамику по сравнимым (тот же набор артикулов). Настройки — общий файл
wb-insights/dashboard.toml (создаётся с настройками по умолчанию).
Результат — один файл wb-insights/<список>/dashboard.html без внешних библиотек.
"""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import report
import schema

BLOCKS = ("summary", "complaints", "praise", "matrix", "competitors", "recommendations", "quotes", "history")
TEMPLATE = Path(__file__).with_name("dashboard_template.html")
CONFIG_NAME = "dashboard.toml"

DEFAULT_CONFIG_TOML = """\
# Настройки дашборда wb-review-insights — общие для всех списков.
# После правки пересоберите дашборд: /wb-review-insights:wb-dashboard <имя списка>

[blocks]
# Порядок блоков на странице. Доступные блоки:
#   summary         — карточки-итоги
#   complaints      — топ жалоб
#   praise          — топ похвалы
#   matrix          — матрица «тема × конкурент»
#   competitors     — карточки конкурентов
#   recommendations — рекомендации
#   quotes          — лента цитат с поиском
#   history         — динамика между запусками
order = ["summary", "complaints", "praise", "matrix", "competitors", "recommendations", "quotes", "history"]
# Блоки, которые не показывать, например ["praise", "matrix"]
hidden = []

[top]
# Сколько тем показывать в графиках и матрице
complaints = 10
praise = 10

[filters]
# Значения фильтров при открытии страницы
min_mentions = 1
niche_only = false

[history]
# Сколько последних сравнимых запусков показывать
max_runs = 12
# "per100" — упоминаний на 100 негативных отзывов (не зависит от --neg-limit),
# "absolute" — просто число упоминаний
metric = "per100"

[aliases]
# Ручные синонимы тем, если агенты назвали одну тему по-разному в разных запусках.
# Слева — как показывать, справа — другие названия той же темы.
# "Маломерит" = ["Размер меньше заявленного", "Тугая, давит"]
"""


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------- настройки


def load_config(root: Path, log=print) -> dict:
    """Читает root/dashboard.toml (создаёт, если нет) и дополняет значениями по умолчанию."""
    path = root / CONFIG_NAME
    if not path.exists():
        root.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
        log(f"Создан файл настроек: {path}")
    defaults = tomllib.loads(DEFAULT_CONFIG_TOML)
    try:
        user = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"ошибка в {path}: {e}")
    cfg = {k: dict(v) for k, v in defaults.items()}
    for section, values in user.items():
        if section == "aliases":
            cfg["aliases"] = dict(values)
        elif section in cfg and isinstance(values, dict):
            cfg[section].update(values)
        else:
            log(f"{path.name}: неизвестный раздел [{section}] — пропущен")

    order = [b for b in cfg["blocks"]["order"] if b in BLOCKS]
    unknown = [b for b in cfg["blocks"]["order"] + cfg["blocks"]["hidden"] if b not in BLOCKS]
    if unknown:
        log(f"{path.name}: неизвестные блоки {unknown} — пропущены. Доступны: {', '.join(BLOCKS)}")
    cfg["blocks"]["visible"] = [b for b in dict.fromkeys(order) if b not in cfg["blocks"]["hidden"]]

    for section, key in (("top", "complaints"), ("top", "praise"), ("filters", "min_mentions"), ("history", "max_runs")):
        v = cfg[section][key]
        if not (isinstance(v, int) and not isinstance(v, bool) and v >= 1):
            raise ConfigError(f"{path.name}: [{section}] {key} должно быть целым числом ≥ 1, а не {v!r}")
    if cfg["history"]["metric"] not in ("per100", "absolute"):
        raise ConfigError(f'{path.name}: [history] metric — "per100" или "absolute"')
    if not isinstance(cfg["filters"]["niche_only"], bool):
        raise ConfigError(f"{path.name}: [filters] niche_only — true или false")
    for canon, names in cfg["aliases"].items():
        if not (isinstance(names, list) and all(isinstance(n, str) for n in names)):
            raise ConfigError(f'{path.name}: [aliases] "{canon}" — нужен список строк')
    return cfg


class Aliases:
    def __init__(self, aliases: dict[str, list[str]]):
        self._map = {}
        for canon, names in aliases.items():
            for n in [canon, *names]:
                self._map[schema.norm(n)] = canon

    def __call__(self, theme: str) -> str:
        return self._map.get(schema.norm(theme), theme)


# ---------------------------------------------------------------- запуски


@dataclass
class Run:
    path: Path
    articles: frozenset[int]

    @property
    def label(self) -> str:
        """2026-10-15_120000 -> 15.10.2026 12:00"""
        try:
            return datetime.strptime(self.path.name[:17], "%Y-%m-%d_%H%M%S").strftime("%d.%m.%Y %H:%M")
        except ValueError:
            return self.path.name


def find_runs(list_dir: Path) -> list[Run]:
    """Запуски списка от старых к новым — только те, где есть хотя бы один результат."""
    runs = []
    for p in sorted(x for x in list_dir.iterdir() if x.is_dir()):
        status = report._read_json(p / "data" / "_status.json")
        if not isinstance(status, list) or not any((p / "results").glob("*.json")):
            continue
        arts = frozenset(s["article"] for s in status if isinstance(s.get("article"), int))
        runs.append(Run(p, arts))
    return runs


def comparable(runs: list[Run], max_runs: int) -> tuple[list[Run], int]:
    """Запуски с тем же набором артикулов, что у последнего; и сколько пропущено."""
    if not runs:
        return [], 0
    same = [r for r in runs if r.articles == runs[-1].articles]
    return same[-max_runs:], len(runs) - len(same)


# ---------------------------------------------------------------- снимок запуска


def snapshot(run: Run, aliases: Aliases, log=lambda s: None) -> dict:
    results, meta, summary, status = report.load_run(run.path, log)
    if not results:
        raise ConfigError(f"в {run.path} нет валидных результатов")
    sections = {}
    for section in schema.SECTIONS:
        if summary:
            raw_groups = [(g["theme"], g["description"], g["sources"], g["examples"]) for g in summary[section]]
        else:
            # без сводки: одинаковые по названию темы конкурентов — одна группа
            merged: dict[str, tuple] = {}
            for a, r in results.items():
                for t in r[section]:
                    key = schema.norm(aliases(t["theme"]))
                    g = merged.setdefault(key, (aliases(t["theme"]), t["description"], [], []))
                    g[2].append({"article": a, "theme": t["theme"]})
                    g[3].extend({"article": a, **e} for e in t["examples"][:1])
            raw_groups = list(merged.values())
        by_key = {(a, schema.norm(t["theme"])): t for a, r in results.items() for t in r[section]}
        groups = {}
        for theme, desc, sources, examples in raw_groups:
            name = aliases(theme)
            g = groups.setdefault(schema.norm(name), {"theme": name, "description": desc, "per_article": {},
                                                      "examples": [], "source_themes": []})
            for s in sources:
                t = by_key.get((s["article"], schema.norm(s["theme"])))
                if t:
                    key = str(s["article"])
                    g["per_article"][key] = g["per_article"].get(key, 0) + t["mentions"]
                    g["source_themes"].append([s["article"], t["theme"]])
            g["examples"] = (g["examples"] + examples)[:schema.MAX_EXAMPLES]
        for g in groups.values():
            g["mentions"] = sum(g["per_article"].values())
            g["competitors"] = sum(1 for v in g["per_article"].values() if v)
        sections[section] = sorted(groups.values(), key=lambda g: (-g["competitors"], -g["mentions"], g["theme"]))

    competitors = []
    order = [s["article"] for s in status if s.get("article") in results]
    for a in dict.fromkeys(order + list(results)):
        r, m = results[a], meta.get(a, {})
        competitors.append({
            "article": a, "brand": r["brand"], "name": r["name"],
            "url": m.get("url") or f"https://www.wildberries.ru/catalog/{a}/detail.aspx",
            "rating": m.get("rating"), "total_feedbacks": m.get("total_feedbacks"),
            "negative": r["reviews_analyzed"]["negative"], "positive": r["reviews_analyzed"]["positive"],
            "top_complaints": [{"theme": t["theme"], "mentions": t["mentions"]} for t in r["complaints"][:3]],
        })

    theme_of = {}  # (article, исходная тема) -> название группы
    for section, groups in sections.items():
        for g in groups:
            for a, t in g["source_themes"]:
                theme_of[(section, a, schema.norm(t))] = g["theme"]
    quotes = []
    for a, r in results.items():
        for section in schema.SECTIONS:
            for t in r[section]:
                for e in t["examples"]:
                    quotes.append({"article": a, "section": section, "rating": e["rating"], "quote": e["quote"],
                                   "theme": theme_of.get((section, a, schema.norm(t["theme"])), aliases(t["theme"]))})

    failed = [{"article": s.get("article"), "input": s.get("input"), "message": s.get("message") or "ошибка анализа"}
              for s in status if s["status"] != "ok" or s.get("article") not in results]
    return {
        "run": run.path.name, "label": run.label,
        "competitors": competitors,
        "complaints": sections["complaints"], "praise": sections["praise"],
        "recommendations": summary["recommendations"] if summary else None,
        "chat_summary": summary["chat_summary"] if summary else None,
        "quotes": quotes, "failed": failed,
        "total_negative": sum(c["negative"] for c in competitors),
        "total_positive": sum(c["positive"] for c in competitors),
    }


# ---------------------------------------------------------------- динамика


def history(snaps: list[dict], metric: str, top: int) -> dict:
    """Ряды по жалобам: значение в каждом сравнимом запуске (None — темы не было)."""
    def value(snap, g):
        if metric == "absolute":
            return g["mentions"]
        return round(100 * g["mentions"] / snap["total_negative"], 1) if snap["total_negative"] else 0.0

    tables = [{schema.norm(g["theme"]): (g["theme"], value(s, g)) for g in s["complaints"]} for s in snaps]
    latest, prev = tables[-1], tables[-2] if len(tables) > 1 else {}
    keys = [schema.norm(g["theme"]) for g in snaps[-1]["complaints"][:top]]
    keys += [k for k in prev if k not in latest]  # ушедшие темы — в конец
    series = []
    for k in keys:
        name = (latest.get(k) or prev.get(k))[0]
        values = [t[k][1] if k in t else None for t in tables]
        if len(tables) < 2:
            state, delta = "single", None
        elif k not in prev:
            state, delta = "new", None
        elif k not in latest:
            state, delta = "gone", None
        else:
            delta = round(values[-1] - values[-2], 1)
            state = "up" if delta > 0 else "down" if delta < 0 else "same"
        series.append({"theme": name, "values": values, "state": state, "delta": delta})
    return {"runs": [s["label"] for s in snaps], "metric": metric, "series": series}


# ---------------------------------------------------------------- словарь тем


def vocabulary(list_dir: Path, current: Path, cfg: dict) -> dict | None:
    """Названия групп из последнего прошлого сравнимого запуска (с учётом синонимов) —
    чтобы insights-writer называл те же темы так же. None — если такого запуска нет."""
    status = report._read_json(current / "data" / "_status.json") or []
    arts = frozenset(s["article"] for s in status if isinstance(s.get("article"), int))
    aliases = Aliases(cfg["aliases"])
    for run in reversed(find_runs(list_dir)):
        if run.path.resolve() == current.resolve() or run.articles != arts:
            continue
        summary = report.load_run(run.path, log=lambda s: None)[2]
        if summary:
            return {"run": run.path.name,
                    **{sec: list(dict.fromkeys(aliases(g["theme"]) for g in summary[sec])) for sec in schema.SECTIONS}}
    return None


def vocabulary_text(vocab: dict | None) -> str:
    if not vocab:
        return "Прошлых сравнимых запусков нет — словаря тем нет."
    lines = [f"Словарь тем из прошлого запуска {vocab['run']}:"]
    for sec, title in (("complaints", "Жалобы"), ("praise", "Похвала")):
        lines.append(f"{title}: " + ("; ".join(vocab[sec]) if vocab[sec] else "—"))
    return "\n".join(lines)


# ---------------------------------------------------------------- сборка


def build_data(list_dir: Path, cfg: dict, log=print) -> dict:
    runs = find_runs(list_dir)
    if not runs:
        raise ConfigError(f"в {list_dir} нет ни одного запуска с результатами")
    comp, skipped = comparable(runs, cfg["history"]["max_runs"])
    aliases = Aliases(cfg["aliases"])
    snaps = []
    for r in comp:
        try:
            snaps.append(snapshot(r, aliases))
        except ConfigError as e:
            log(f"Пропускаю запуск {r.path.name}: {e}")
    if not snaps:
        raise ConfigError("ни один запуск не удалось прочитать")
    data = snaps[-1] | {
        "list": list_dir.name,
        "generated_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "history": history(snaps, cfg["history"]["metric"], cfg["top"]["complaints"]) | {"skipped": skipped},
        "config": {"blocks": cfg["blocks"]["visible"], "top": cfg["top"], "filters": cfg["filters"]},
    }
    return data


def render(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8")
    return html.replace("__TITLE__", f"Отзывы конкурентов — {data['list']}").replace("__DATA__", payload)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="HTML-дашборд по запускам списка конкурентов")
    p.add_argument("list_dir", type=Path, help="папка списка, например wb-insights/шапки")
    p.add_argument("--config-dir", type=Path, help="где лежит dashboard.toml (по умолчанию — папка выше списка)")
    p.add_argument("--out", type=Path, help="куда писать html (по умолчанию <список>/dashboard.html)")
    p.add_argument("--vocab-for", type=Path, metavar="RUN",
                   help="не строить дашборд, а напечатать словарь тем для запуска RUN")
    a = p.parse_args(argv)
    if not a.list_dir.is_dir():
        root = a.list_dir.parent
        lists = sorted(d.name for d in root.iterdir() if d.is_dir()) if root.is_dir() else []
        print(f"Ошибка: папки {a.list_dir} нет." + (f" Есть списки: {', '.join(lists)}" if lists else ""), file=sys.stderr)
        return 2
    try:
        cfg = load_config(a.config_dir or a.list_dir.parent)
        if a.vocab_for:
            print(vocabulary_text(vocabulary(a.list_dir, a.vocab_for, cfg)))
            return 0
        data = build_data(a.list_dir, cfg)
    except ConfigError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    out = a.out or a.list_dir / "dashboard.html"
    out.write_text(render(data), encoding="utf-8")
    h = data["history"]
    print(f"Дашборд: {out}")
    print(f"Последний запуск: {data['label']}, конкурентов: {len(data['competitors'])}, "
          f"сравнимых запусков: {len(h['runs'])}" + (f" (с другим набором товаров пропущено: {h['skipped']})" if h["skipped"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
