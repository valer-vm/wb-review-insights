# /// script
# requires-python = ">=3.11"
# dependencies = ["pandas>=2.1"]
# ///
"""Проверка JSON, которые пишут субагенты, и подсчёт сводных чисел.

  uv run scripts/schema.py result  results/<артикул>.json --csv data/<артикул>.csv [--no-fix]
  uv run scripts/schema.py summary results/summary.json [--results results]

Код выхода 0 — всё в порядке, 1 — ошибки (печатаются по одной на строку,
их можно целиком передать субагенту для исправления).

С --csv проверка result сначала сама дописывает и правит в файле то, что
считается по CSV (см. finalize_result): mentions, рейтинги примеров, порядок
и число тем. Субагента повторяют только из-за ошибок, которые так не исправить.

Формат results/<артикул>.json (пишет review-analyst):
{
  "article": 368686013, "name": "...", "brand": "...",
  "reviews_analyzed": {"negative": 51, "positive": 100},
  "complaints": [Theme, ...],   # по убыванию mentions
  "praise":     [Theme, ...]
}
Theme = {"theme": "Маломерит", "description": "...",
         "reviews": [3, 17, 42],   # id отзывов из CSV, пишет субагент
         "mentions": 3,            # = число reviews, дописывает finalize_result
         "examples": [{"id": 17, "quote": "дословная цитата", "rating": 4}, ...]}  # 1–3 примера
Старые результаты (до 0.3.0) — без reviews и id: mentions там посчитаны субагентом.

Формат results/summary.json (пишет insights-writer):
{
  "complaints": [Group, ...], "praise": [Group, ...],
  "recommendations": {"improve_product": [Rec, ...], "highlight_in_card": [Rec, ...]},
  "chat_summary": "текст для чата"
}
Group = {"theme": "...", "description": "...",
         "sources": [{"article": 368686013, "theme": "тема из results/<артикул>.json"}, ...],
         "examples": [{"article": 368686013, "quote": "...", "rating": 2}, ...]}  # 1–3
Rec   = {"title": "...", "details": "...", "based_on": ["тема Group", ...]}
Числа (упоминания, число конкурентов) субагент не пишет — их считает summary_stats().
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

MAX_EXAMPLES = 3
MAX_THEMES = 15
MAX_THEME_LEN = 80
# Тема с одним упоминанием — только если в группе меньше SMALL_GROUP отзывов.
SMALL_GROUP = 15
BUCKET = {"complaints": ("neg", "negative"), "praise": ("pos", "positive")}
SECTIONS = ("complaints", "praise")
REC_KINDS = ("improve_product", "highlight_in_card")
SPLIT_RE = re.compile(r"\s*(?:…|\.\.\.)\s*")


def norm(s: str) -> str:
    """Для сравнения цитат: регистр, ё/е, кавычки и пробелы не важны."""
    s = str(s).casefold().replace("ё", "е")
    s = re.sub(r"[«»“”„\"']", "", s)
    return re.sub(r"\s+", " ", s).strip()


def quote_found(quote: str, haystack: list[str]) -> bool:
    """Цитата (или её куски, разделённые «…») дословно есть в одном из текстов."""
    pieces = [norm(p) for p in SPLIT_RE.split(quote) if norm(p)]
    return bool(pieces) and any(all(p in h for p in pieces) for h in haystack)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _str(v) -> bool:
    return isinstance(v, str) and v.strip() != ""


# ---------------------------------------------------------------- result


def csv_reviews(csv: pd.DataFrame) -> dict[int, dict]:
    """id отзыва -> оценка, группа и текст для поиска цитат.
    В CSV до 0.3.0 колонки id нет — тогда id это номер строки с 1."""
    csv = csv.fillna("")
    ids = csv["id"] if "id" in csv.columns else range(1, len(csv) + 1)
    # Покупатели путают поля, поэтому цитату ищем сразу во всех полях отзыва.
    return {int(i): {"rating": int(r.rating), "bucket": r.bucket,
                     "text": norm(" ".join(map(str, (r.text, r.pros, r.cons))))}
            for i, r in zip(ids, csv.itertuples())}


def _real_counts(reviews: dict[int, dict]) -> dict[str, int]:
    return {name: sum(r["bucket"] == b for r in reviews.values()) for b, name in BUCKET.values()}


def _locate(e: dict, reviews: dict[int, dict]) -> int | None:
    """id отзыва, где есть цитата примера: сначала указанный, потом любой."""
    i = e.get("id")
    if _is_int(i) and i in reviews and quote_found(e["quote"], [reviews[i]["text"]]):
        return i
    return next((k for k, r in reviews.items() if quote_found(e["quote"], [r["text"]])), None)


def finalize_result(data, csv: pd.DataFrame) -> list[str]:
    """Дописывает в result то, что считается по CSV, и возвращает список правок.
    mentions = число reviews, рейтинг и id примеров — по отзыву с цитатой,
    темы с малым числом упоминаний убираются, остальные сортируются и
    обрезаются до MAX_THEMES. Что так не исправить, оставляет validate_result."""
    if not isinstance(data, dict):
        return []
    reviews = csv_reviews(csv)
    notes: list[str] = []
    real = _real_counts(reviews)
    if data.get("reviews_analyzed") != real:
        data["reviews_analyzed"] = real
        notes.append(f"reviews_analyzed взято из CSV: {real}")
    for section in SECTIONS:
        themes = data.get(section)
        if not isinstance(themes, list):
            continue
        for t in themes:
            if not (isinstance(t, dict) and isinstance(t.get("reviews"), list)):
                continue  # старый формат или ошибка — скажет validate_result
            ids = list(dict.fromkeys(x for x in t["reviews"] if _is_int(x) and x in reviews))
            if len(ids) != len(t["reviews"]):
                notes.append(f"{section} «{t.get('theme')}»: убраны повторы и несуществующие id отзывов")
            for e in t.get("examples") if isinstance(t.get("examples"), list) else []:
                if not (isinstance(e, dict) and _str(e.get("quote"))):
                    continue
                i = _locate(e, reviews)
                if i is None:
                    continue
                if e.get("id") != i:
                    if "id" in e:
                        notes.append(f"{section} «{t.get('theme')}»: у цитаты исправлен id отзыва {e['id']} → {i}")
                    e["id"] = i
                e["rating"] = reviews[i]["rating"]
                if i not in ids:
                    ids.append(i)
                    notes.append(f"{section} «{t.get('theme')}»: отзыв {i} с цитатой добавлен в reviews")
            t["reviews"] = ids
            t["mentions"] = len(ids)

        min_mentions = 1 if real[BUCKET[section][1]] < SMALL_GROUP else 2
        kept = [t for t in themes
                if not (isinstance(t, dict) and isinstance(t.get("reviews"), list) and t["mentions"] < min_mentions)]
        for t in themes:
            if t not in kept:
                notes.append(f"{section} «{t.get('theme')}»: убрана — упоминаний {t['mentions']}, нужно от {min_mentions}")
        if all(isinstance(t, dict) and _is_int(t.get("mentions")) for t in kept):
            ordered = sorted(kept, key=lambda t: -t["mentions"])
            if ordered != kept:
                notes.append(f"{section}: темы отсортированы по убыванию mentions")
            kept = ordered
        if len(kept) > MAX_THEMES:
            notes.append(f"{section}: оставлены {MAX_THEMES} самых частых тем из {len(kept)}")
            kept = kept[:MAX_THEMES]
        data[section] = kept
    return notes


def validate_result(data, csv: pd.DataFrame | None = None) -> list[str]:
    errs: list[str] = []
    if not isinstance(data, dict):
        return ["корень JSON должен быть объектом"]
    if not _is_int(data.get("article")):
        errs.append("article: нужен артикул числом")
    for k in ("name", "brand"):
        if not isinstance(data.get(k), str):
            errs.append(f"{k}: нужна строка (можно пустую)")
    ra = data.get("reviews_analyzed")
    if not (isinstance(ra, dict) and all(_is_int(ra.get(k)) and ra[k] >= 0 for k in ("negative", "positive"))):
        errs.append("reviews_analyzed: нужен объект {negative: число, positive: число}")
        ra = None

    reviews = None
    # В CSV с колонкой id каждая тема обязана ссылаться на отзывы.
    need_ids = csv is not None and "id" in csv.columns
    if csv is not None:
        reviews = csv_reviews(csv)
        if ra:
            for k, v in _real_counts(reviews).items():
                if ra[k] != v:
                    errs.append(f"reviews_analyzed.{k} = {ra[k]}, а в CSV {v}")
    texts = [r["text"] for r in reviews.values()] if reviews is not None else None

    for section in SECTIONS:
        themes = data.get(section)
        if not isinstance(themes, list):
            errs.append(f"{section}: нужен список (можно пустой)")
            continue
        if len(themes) > MAX_THEMES:
            errs.append(f"{section}: не больше {MAX_THEMES} тем, а их {len(themes)}")
        names = set()
        limit = ra["negative"] + ra["positive"] if ra else None
        for i, t in enumerate(themes):
            where = f"{section}[{i}]"
            if not isinstance(t, dict):
                errs.append(f"{where}: нужен объект")
                continue
            name = t.get("theme")
            if not _str(name) or len(name) > MAX_THEME_LEN:
                errs.append(f"{where}.theme: нужна короткая непустая строка (до {MAX_THEME_LEN} символов)")
            elif norm(name) in names:
                errs.append(f"{where}.theme «{name}» повторяется — объедините темы")
            else:
                names.add(norm(name))
            if not _str(t.get("description")):
                errs.append(f"{where}.description: нужна непустая строка")
            ids = t.get("reviews")
            if ids is None and need_ids:
                errs.append(f"{where}.reviews: нужен список id отзывов из колонки id CSV")
            elif ids is not None:
                if not (isinstance(ids, list) and ids and all(_is_int(x) for x in ids)):
                    errs.append(f"{where}.reviews: нужен непустой список id отзывов (числа)")
                    ids = None
                elif reviews is not None and (unknown := [x for x in ids if x not in reviews]):
                    errs.append(f"{where}.reviews: в CSV нет отзывов с id {unknown[:5]}")
                elif len(set(ids)) != len(ids):
                    errs.append(f"{where}.reviews: id отзывов повторяются")
            m = t.get("mentions")
            if not (_is_int(m) and m >= 1):
                errs.append(f"{where}.mentions: нужно целое число ≥ 1")
            elif limit is not None and m > limit:
                errs.append(f"{where}.mentions = {m} больше числа отзывов ({limit})")
            elif isinstance(ids, list) and m != len(set(ids)):
                errs.append(f"{where}.mentions = {m}, а отзывов в reviews {len(set(ids))}")
            ex = t.get("examples")
            if not (isinstance(ex, list) and 1 <= len(ex) <= MAX_EXAMPLES):
                errs.append(f"{where}.examples: нужно от 1 до {MAX_EXAMPLES} примеров")
                continue
            for j, e in enumerate(ex):
                w = f"{where}.examples[{j}]"
                if not (isinstance(e, dict) and _str(e.get("quote"))):
                    errs.append(f"{w}: нужен объект {{id, quote: строка}}")
                elif texts is not None and not quote_found(e["quote"], texts):
                    errs.append(f"{w}: цитаты «{e['quote'][:60]}» нет в отзывах — нужна дословная цитата")
                elif not (_is_int(e.get("rating")) and 1 <= e["rating"] <= 5):
                    errs.append(f"{w}.rating: 1–5 — оценка отзыва с цитатой")
                elif reviews is not None and "id" in e and not (
                        e["id"] in reviews and quote_found(e["quote"], [reviews[e["id"]]["text"]])):
                    errs.append(f"{w}: цитаты нет в отзыве id {e['id']}")
        mentions = [t.get("mentions") for t in themes if isinstance(t, dict) and _is_int(t.get("mentions"))]
        if mentions != sorted(mentions, reverse=True):
            errs.append(f"{section}: отсортируйте темы по убыванию mentions")
    return errs


# ---------------------------------------------------------------- summary


def load_results(results_dir: Path) -> dict[int, dict]:
    out = {}
    for p in sorted(results_dir.glob("*.json")):
        if p.name == "summary.json":
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        out[int(data["article"])] = data
    return out


def validate_summary(summary, results: dict[int, dict]) -> list[str]:
    errs: list[str] = []
    if not isinstance(summary, dict):
        return ["корень JSON должен быть объектом"]
    group_names: set[str] = set()
    for section in SECTIONS:
        groups = summary.get(section)
        if not isinstance(groups, list):
            errs.append(f"{section}: нужен список")
            continue
        # каждая тема каждого конкурента должна попасть ровно в одну группу
        expected = {(a, norm(t["theme"])) for a, r in results.items() for t in r.get(section, [])}
        used: dict[tuple, str] = {}
        for i, g in enumerate(groups):
            where = f"{section}[{i}]"
            if not isinstance(g, dict):
                errs.append(f"{where}: нужен объект")
                continue
            if not _str(g.get("theme")):
                errs.append(f"{where}.theme: нужна непустая строка")
            else:
                group_names.add(norm(g["theme"]))
            if not _str(g.get("description")):
                errs.append(f"{where}.description: нужна непустая строка")
            src = g.get("sources")
            if not (isinstance(src, list) and src):
                errs.append(f"{where}.sources: нужен непустой список {{article, theme}}")
                src = []
            for s in src:
                key = (s.get("article"), norm(s.get("theme", ""))) if isinstance(s, dict) else None
                if key not in expected:
                    errs.append(f"{where}.sources: темы {s} нет в results/ (тема пишется точно как у конкурента)")
                elif key in used:
                    errs.append(f"{where}.sources: тема {s} уже входит в группу «{used[key]}»")
                else:
                    used[key] = g.get("theme", "")
            ex = g.get("examples")
            if not (isinstance(ex, list) and 1 <= len(ex) <= MAX_EXAMPLES):
                errs.append(f"{where}.examples: нужно от 1 до {MAX_EXAMPLES} примеров")
                continue
            for j, e in enumerate(ex):
                w = f"{where}.examples[{j}]"
                if not (isinstance(e, dict) and _str(e.get("quote")) and _is_int(e.get("article"))):
                    errs.append(f"{w}: нужен объект {{article, quote, rating}}")
                    continue
                quotes = [norm(x["quote"]) for t in results.get(e["article"], {}).get(section, []) for x in t["examples"]]
                if not quote_found(e["quote"], quotes):
                    errs.append(f"{w}: цитата не взята из примеров results/{e['article']}.json")
        for a, t in sorted(expected - used.keys(), key=str):
            errs.append(f"{section}: тема «{t}» конкурента {a} не попала ни в одну группу")

    recs = summary.get("recommendations")
    if not isinstance(recs, dict):
        errs.append("recommendations: нужен объект {improve_product: [...], highlight_in_card: [...]}")
    else:
        for kind in REC_KINDS:
            items = recs.get(kind)
            if not isinstance(items, list):
                errs.append(f"recommendations.{kind}: нужен список")
                continue
            for i, r in enumerate(items):
                where = f"recommendations.{kind}[{i}]"
                if not (isinstance(r, dict) and _str(r.get("title")) and _str(r.get("details"))):
                    errs.append(f"{where}: нужны непустые title и details")
                    continue
                for b in r.get("based_on") or []:
                    if norm(b) not in group_names:
                        errs.append(f"{where}.based_on: «{b}» — нет такой темы в complaints/praise")
    if not _str(summary.get("chat_summary")):
        errs.append("chat_summary: нужен непустой текст")
    return errs


def summary_stats(group: dict, results: dict[int, dict], section: str) -> dict:
    """Упоминания и охват группы — по исходным темам конкурентов."""
    by_key = {(a, norm(t["theme"])): t for a, r in results.items() for t in r.get(section, [])}
    mentions, articles = 0, []
    for s in group.get("sources", []):
        t = by_key.get((s["article"], norm(s["theme"])))
        if t:
            mentions += t["mentions"]
            if s["article"] not in articles:
                articles.append(s["article"])
    return {"mentions": mentions, "competitors": len(articles), "articles": articles,
            "niche_wide": len(articles) >= 2 and len(articles) * 2 >= len(results)}


# ---------------------------------------------------------------- CLI


def _report(errs: list[str], path: Path, fixed: list[str] = ()) -> int:
    if fixed:
        print(f"{path}: исправлено автоматически — {len(fixed)}")
        print("\n".join(f"  · {n}" for n in fixed))
    if errs:
        print(f"{path}: найдено ошибок — {len(errs)}")
        print("\n".join(f"- {e}" for e in errs))
        return 1
    print(f"{path}: OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Проверка JSON от субагентов")
    sub = p.add_subparsers(dest="kind", required=True)
    r = sub.add_parser("result")
    r.add_argument("path", type=Path)
    r.add_argument("--csv", type=Path)
    r.add_argument("--no-fix", action="store_true", help="только проверить, файл не менять")
    s = sub.add_parser("summary")
    s.add_argument("path", type=Path)
    s.add_argument("--results", type=Path)
    a = p.parse_args(argv)
    try:
        data = json.loads(a.path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _report(["файл не создан"], a.path)
    except json.JSONDecodeError as e:
        return _report([f"невалидный JSON: {e}"], a.path)
    if a.kind == "result":
        csv = pd.read_csv(a.csv, keep_default_na=False) if a.csv else None
        fixed = []
        if csv is not None and not a.no_fix:
            fixed = finalize_result(data, csv)
            if fixed:
                a.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return _report(validate_result(data, csv), a.path, fixed)
    return _report(validate_summary(data, load_results(a.results or a.path.parent)), a.path)


if __name__ == "__main__":
    sys.exit(main())
