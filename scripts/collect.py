# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.31", "pandas>=2.1"]
# ///
"""Сбор отзывов о товарах Wildberries через внутренние JSON-запросы сайта.

Для каждого артикула из файла со ссылками:
  1. card.json с CDN WB  -> imt_id (id карточки), название, бренд;
  2. feedbacks/v2/<imt_id> -> все отзывы карточки (все цвета и размеры).
Затем отзывы фильтруются по давности, делятся на негатив и позитив
и пишутся в data/<артикул>.csv. Итог по всем товарам — data/_status.json.

Запуск:  uv run scripts/collect.py links.txt [--neg-limit 150] [--pos-limit 100]
                                             [--months 12] [--out data]
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pandas as pd
import requests

# Если WB поменяет адреса — править здесь (см. README, раздел про DevTools).
# feedbacks1/2 отдают отзывы любой карточки; feedback-view-01/02 — половинки
# хранилища: на чужую карточку они отвечают пустой заглушкой (0 отзывов).
FEEDBACK_HOSTS = (
    "https://feedbacks1.wb.ru",
    "https://feedbacks2.wb.ru",
    "https://feedback-view-02.wb.ru",
    "https://feedback-view-01.wb.ru",
)
FEEDBACK_PATH = "/feedbacks/v2/{imt_id}"
CARD_URL = "https://basket-{basket:02d}.wbbasket.ru/vol{vol}/part{part}/{nm}/info/ru/card.json"
MAX_BASKET = 40

# Какой basket-XX хранит товар, зависит от vol = артикул // 100000.
# (верхняя граница vol включительно, номер basket). Дальше таблицы — перебор.
BASKET_RANGES = (
    (143, 1), (287, 2), (431, 3), (719, 4), (1007, 5), (1061, 6), (1115, 7),
    (1169, 8), (1313, 9), (1601, 10), (1655, 11), (1919, 12), (2045, 13),
    (2189, 14), (2405, 15), (2621, 16), (2837, 17), (3053, 18), (3269, 19),
    (3485, 20), (3701, 21), (3917, 22), (4133, 23), (4349, 24), (4565, 25),
)

HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "ru",
    "Origin": "https://www.wildberries.ru",
    "Referer": "https://www.wildberries.ru/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"
    ),
}

CSV_COLUMNS = ["rating", "text", "pros", "cons", "date", "color", "size", "nm_id", "bucket"]
MAX_FIELD_CHARS = 1000

LINK_RE = re.compile(r"wildberries\.(?:ru|by|kz|am|kg|uz)/catalog/(\d+)", re.IGNORECASE)
NM_PARAM_RE = re.compile(r"[?&]nm=(\d+)")


class CollectError(Exception):
    """Ошибка с понятным пользователю сообщением."""


# ---------------------------------------------------------------- ссылки


def parse_link(raw: str) -> int:
    """Достаёт артикул (nmId) из ссылки WB или из голого числа."""
    s = raw.strip()
    if s.isdigit():
        return int(s)
    m = NM_PARAM_RE.search(s) or LINK_RE.search(s)
    if m:
        return int(m.group(1))
    raise CollectError(
        f"не похоже на ссылку на товар WB: «{s}». "
        "Ожидается вида https://www.wildberries.ru/catalog/12345678/detail.aspx или просто артикул"
    )


def read_links(path: Path) -> list[str]:
    """Непустые строки файла без комментариев (#)."""
    if not path.exists():
        raise CollectError(f"файл со ссылками не найден: {path}")
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8-sig").splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("#")]


# ---------------------------------------------------------------- сеть


def basket_guess(vol: int) -> int | None:
    for max_vol, basket in BASKET_RANGES:
        if vol <= max_vol:
            return basket
    return None


@dataclass
class WbClient:
    session: requests.Session = field(default_factory=requests.Session)
    pause: tuple[float, float] = (1.0, 2.0)
    retries: int = 3
    timeout: float = 15.0
    sleep: Callable[[float], None] = time.sleep
    _first: bool = True

    def _wait(self) -> None:
        if not self._first:
            self.sleep(random.uniform(*self.pause))
        self._first = False

    def get_json(self, url: str, *, polite: bool = True, retries: int | None = None,
                 timeout: float | None = None) -> dict | None:
        """GET с повторами. None — если ресурса нет (404). Иначе CollectError."""
        retries = self.retries if retries is None else retries
        last = ""
        for attempt in range(retries + 1):
            if polite:
                self._wait()
            elif attempt:
                self.sleep(1.0)
            try:
                r = self.session.get(url, headers=HEADERS, timeout=timeout or self.timeout)
            except requests.RequestException as e:
                last = f"нет связи с {_host(url)} ({type(e).__name__})"
            else:
                if r.status_code == 404:
                    return None
                if r.status_code == 200:
                    try:
                        return r.json()
                    except ValueError:
                        raise CollectError(f"{_host(url)} вернул не JSON — возможно, WB поменял формат")
                if r.status_code == 403:
                    raise CollectError(
                        f"{_host(url)} отказал в доступе (403). Возможно, WB поменял запрос — "
                        "см. README, как найти актуальный в DevTools"
                    )
                last = f"{_host(url)} ответил кодом {r.status_code}"
                if r.status_code != 429 and r.status_code < 500:
                    raise CollectError(last)
            if attempt < retries:
                self.sleep(2.0 * 2**attempt)
        raise CollectError(f"{last}; сайт WB недоступен, попробуйте позже")

    def fetch_card(self, nm: int) -> dict:
        vol, part = nm // 100000, nm // 1000
        guess = basket_guess(vol)
        order = ([guess] if guess else []) + [b for b in range(1, MAX_BASKET + 1) if b != guess]
        for i, basket in enumerate(order):
            url = CARD_URL.format(basket=basket, vol=vol, part=part, nm=nm)
            if i == 0:
                card = self.get_json(url)
            else:
                # Перебор по CDN: без пауз и повторов, недоступный basket просто пропускаем.
                try:
                    card = self.get_json(url, polite=False, retries=0, timeout=5.0)
                except CollectError:
                    continue
            if card is not None:
                return card
        raise CollectError(f"товар {nm} не найден на WB (проверьте артикул или ссылку)")

    def fetch_feedbacks(self, imt_id: int) -> dict:
        """Первый ответ с отзывами. Пустой ответ может быть заглушкой сервера,
        который не хранит эту карточку, — тогда спрашиваем следующий."""
        errors, empty = [], None
        for host in FEEDBACK_HOSTS:
            try:
                data = self.get_json(host + FEEDBACK_PATH.format(imt_id=imt_id))
            except CollectError as e:
                errors.append(str(e))
                continue
            if data is None:
                errors.append(f"{_host(host)}: 404")
            elif data.get("feedbacks") or data.get("feedbackCount"):
                return data
            else:
                empty = empty or data
        if empty is not None:
            return empty  # ни на одном сервере отзывов нет — у карточки их правда 0
        raise CollectError("не удалось получить отзывы: " + "; ".join(errors))


def _host(url: str) -> str:
    return url.split("/")[2] if "//" in url else url


# ---------------------------------------------------------------- отбор


def _clean(s: str | None) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s[:MAX_FIELD_CHARS] + "…" if len(s) > MAX_FIELD_CHARS else s


def select_reviews(
    feedbacks: list[dict],
    *,
    neg_limit: int,
    pos_limit: int,
    months: int,
    now: datetime | None = None,
) -> pd.DataFrame:
    """Негатив = 1–3★ или 4–5★ с заполненными «Недостатками»; позитив — остальные 4–5★.
    Внутри каждой группы — самые свежие до лимита. Пустые отзывы пропускаются."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=round(months * 30.44)) if months > 0 else None
    rows = []
    for f in feedbacks or []:
        text, pros, cons = _clean(f.get("text")), _clean(f.get("pros")), _clean(f.get("cons"))
        if not (text or pros or cons):
            continue
        created = datetime.fromisoformat(f["createdDate"].replace("Z", "+00:00"))
        if since and created < since:
            continue
        rating = int(f.get("productValuation") or 0)
        rows.append({
            "rating": rating, "text": text, "pros": pros, "cons": cons,
            "date": created.date().isoformat(), "color": f.get("color") or "",
            "size": f.get("size") or "", "nm_id": f.get("nmId"),
            "bucket": "neg" if rating <= 3 or cons else "pos",
            "_ts": created,
        })
    df = pd.DataFrame(rows, columns=CSV_COLUMNS + ["_ts"])
    df = df.sort_values("_ts", ascending=False, kind="stable")
    parts = [df[df.bucket == "neg"].head(neg_limit), df[df.bucket == "pos"].head(pos_limit)]
    return pd.concat(parts).drop(columns="_ts").reset_index(drop=True)


# ---------------------------------------------------------------- сборка


@dataclass
class Options:
    neg_limit: int = 150
    pos_limit: int = 100
    months: int = 12


def collect_one(client: WbClient, nm: int, out_dir: Path, opts: Options, seen_imt: dict[int, int]) -> dict:
    card = client.fetch_card(nm)
    imt_id = int(card["imt_id"])
    if imt_id in seen_imt:
        return {"status": "duplicate", "imt_id": imt_id,
                "message": f"та же карточка, что и у {seen_imt[imt_id]} (другой цвет) — пропущено"}
    seen_imt[imt_id] = nm
    data = client.fetch_feedbacks(imt_id)
    df = select_reviews(data.get("feedbacks") or [], neg_limit=opts.neg_limit,
                        pos_limit=opts.pos_limit, months=opts.months)
    counts = {"neg": int((df.bucket == "neg").sum()), "pos": int((df.bucket == "pos").sum())}
    csv_path = out_dir / f"{nm}.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8")
    meta = {
        "article": nm,
        "imt_id": imt_id,
        "name": card.get("imt_name") or "",
        "brand": (card.get("selling") or {}).get("brand_name") or "",
        "subject": card.get("subj_name") or "",
        "url": f"https://www.wildberries.ru/catalog/{nm}/detail.aspx",
        "rating": data.get("valuation"),
        "total_feedbacks": data.get("feedbackCount"),
        "collected": counts,
        "months": opts.months,
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (out_dir / f"{nm}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    status = {"status": "ok", "imt_id": imt_id, "csv": str(csv_path), "name": meta["name"],
              "brand": meta["brand"], "collected": counts}
    if not len(df):
        status["status"] = "empty"
        status["message"] = "подходящих отзывов нет (проверьте --months)"
    return status


def collect(links: list[str], out_dir: Path, opts: Options, client: WbClient | None = None,
            log: Callable[[str], None] = print) -> list[dict]:
    client = client or WbClient()
    out_dir.mkdir(parents=True, exist_ok=True)
    seen_imt: dict[int, int] = {}
    results = []
    for i, raw in enumerate(links, 1):
        entry: dict = {"input": raw}
        try:
            nm = parse_link(raw)
            entry["article"] = nm
            log(f"[{i}/{len(links)}] {nm}: собираю…")
            entry.update(collect_one(client, nm, out_dir, opts, seen_imt))
        except CollectError as e:
            entry.update(status="error", message=str(e))
        log(f"[{i}/{len(links)}] {_describe(entry)}")
        results.append(entry)
    (out_dir / "_status.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return results


def _describe(e: dict) -> str:
    who = e.get("article", e["input"])
    if e["status"] == "ok":
        c = e["collected"]
        return f"{who}: OK — {c['neg']} негативных, {c['pos']} позитивных ({e['brand']} / {e['name']})"
    return f"{who}: {'ОШИБКА' if e['status'] == 'error' else 'пропущено'} — {e.get('message', '')}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Сбор отзывов о товарах Wildberries в CSV")
    p.add_argument("links", type=Path, help="файл со ссылками на товары, по одной на строку")
    p.add_argument("--out", type=Path, default=Path("data"), help="папка для CSV (по умолчанию data)")
    p.add_argument("--neg-limit", type=int, default=150, help="макс. негативных отзывов на товар (150)")
    p.add_argument("--pos-limit", type=int, default=100, help="макс. позитивных отзывов на товар (100)")
    p.add_argument("--months", type=int, default=12, help="только за последние N месяцев, 0 — все (12)")
    a = p.parse_args(argv)
    try:
        links = read_links(a.links)
    except CollectError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2
    if not links:
        print(f"Ошибка: в {a.links} нет ни одной ссылки", file=sys.stderr)
        return 2
    results = collect(links, a.out, Options(a.neg_limit, a.pos_limit, a.months))
    ok = sum(r["status"] == "ok" for r in results)
    print(f"\nГотово: собрано {ok} из {len(results)}. Статус: {a.out / '_status.json'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
