# /// script
# requires-python = ">=3.11"
# dependencies = ["pandas>=2.1"]
# ///
"""Повторное использование результатов прошлых запусков того же списка.

  uv run scripts/reuse.py wb-insights/носки wb-insights/носки/<дата>

Для каждого собранного товара нового запуска ищет прошлый запуск, где
отзывы этого товара были точно такими же (те же отзывы, оценки и тексты;
номер id не важен), а результат — валидный и с id отзывов (формат 0.3.0+).
Такой результат копируется в <запуск>/results/ — субагент для товара не нужен.
Если все товары взяты из одного прошлого запуска и его summary.json подходит
к ним без изменений, копируется и сводка.

Последние строки вывода — для команды:
  Нужен анализ: 411427116 10932708     (или «Нужен анализ: нет»)
  Сводка: взята из <дата>              (или «Сводка: нужна новая»)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

import schema


def fingerprint(csv_path: Path) -> str | None:
    """Хеш отзывов CSV без колонки id — у CSV до 0.3.0 её нет."""
    try:
        df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    except (FileNotFoundError, pd.errors.EmptyDataError):
        return None
    return hashlib.sha256(df.drop(columns="id", errors="ignore").to_csv(index=False).encode()).hexdigest()


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _has_ids(result: dict) -> bool:
    """Результат 0.3.0+: mentions посчитаны скриптом по id отзывов."""
    return all("reviews" in t for s in schema.SECTIONS for t in result[s])


def ok_articles(run: Path) -> list[int]:
    status = _read_json(run / "data" / "_status.json") or []
    return [s["article"] for s in status if isinstance(s, dict) and s.get("status") == "ok"]


def find_previous(list_dir: Path, run: Path, article: int, fp: str) -> tuple[Path, dict] | None:
    """Самый новый прошлый запуск с теми же отзывами товара и годным результатом."""
    csv = pd.read_csv(run / "data" / f"{article}.csv", keep_default_na=False)
    for prev in sorted((p for p in list_dir.iterdir() if p.is_dir() and p.name < run.name), reverse=True):
        if fingerprint(prev / "data" / f"{article}.csv") != fp:
            continue
        result = _read_json(prev / "results" / f"{article}.json")
        if result is None or schema.validate_result(result, csv) or not _has_ids(result):
            continue
        return prev, result
    return None


def reuse(list_dir: Path, run: Path, log=print) -> tuple[list[int], Path | None]:
    """Копирует подходящие результаты; возвращает (что анализировать, откуда взята сводка)."""
    articles = ok_articles(run)
    todo, sources = [], {}
    for a in articles:
        fp = fingerprint(run / "data" / f"{a}.csv")
        found = find_previous(list_dir, run, a, fp) if fp else None
        if not found:
            todo.append(a)
            continue
        prev, result = found
        meta = _read_json(run / "data" / f"{a}.meta.json") or {}
        for k in ("name", "brand"):  # продавец мог переименовать карточку
            if isinstance(meta.get(k), str):
                result[k] = meta[k]
        (run / "results" / f"{a}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        sources[a] = prev
        log(f"  {a}: отзывы не изменились — результат взят из {prev.name}")

    summary_from = None
    prevs = set(sources.values())
    if articles and not todo and len(prevs) == 1:
        prev = prevs.pop()
        summary = _read_json(prev / "results" / "summary.json")
        results = schema.load_results(run / "results")
        # сводка годится, только если в прошлом запуске были ровно эти товары
        if summary is not None and set(schema.load_results(prev / "results")) == set(results) \
                and not schema.validate_summary(summary, results):
            (run / "results" / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            summary_from = prev
    return todo, summary_from


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Взять результаты прошлых запусков, если отзывы не изменились")
    p.add_argument("list_dir", type=Path, help="папка списка, например wb-insights/носки")
    p.add_argument("run", type=Path, help="папка нового запуска")
    a = p.parse_args(argv)
    if not (a.run / "data" / "_status.json").exists():
        print(f"Ошибка: нет {a.run / 'data' / '_status.json'} — сначала соберите отзывы", file=sys.stderr)
        return 2
    (a.run / "results").mkdir(parents=True, exist_ok=True)
    todo, summary_from = reuse(a.list_dir, a.run)
    total = len(ok_articles(a.run))
    print(f"Взято из прошлых запусков: {total - len(todo)} из {total}")
    print("Нужен анализ: " + (" ".join(map(str, todo)) or "нет"))
    print(f"Сводка: взята из {summary_from.name}" if summary_from else "Сводка: нужна новая")
    return 0


if __name__ == "__main__":
    sys.exit(main())
