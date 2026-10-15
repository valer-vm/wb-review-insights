"""Тесты collect.py на сохранённых ответах WB — без обращения к сайту."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
import requests

import collect
from collect import CollectError, Options, WbClient, parse_link, select_reviews

FIXTURES = Path(__file__).parent / "fixtures"
FEEDBACKS = json.loads((FIXTURES / "feedbacks_sample.json").read_text(encoding="utf-8"))
CARD = json.loads((FIXTURES / "card_sample.json").read_text(encoding="utf-8"))
NM, IMT = CARD["nm_id"], CARD["imt_id"]  # 368686013, 11819787
NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """routes: подстрока URL -> список ответов (по очереди) или один ответ.
    Ответ — FakeResponse или исключение. Неизвестный URL -> 404."""

    def __init__(self, routes):
        self.routes = {k: list(v) if isinstance(v, list) else [v] for k, v in routes.items()}
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        for key, queue in self.routes.items():
            if key in url:
                item = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(item, Exception):
                    raise item
                return item
        return FakeResponse(404)


def make_client(routes):
    return WbClient(session=FakeSession(routes), sleep=lambda s: None)


def ok_routes():
    return {
        f"basket-21.wbbasket.ru/vol3686/part368686/{NM}/": FakeResponse(200, CARD),
        f"feedbacks1.wb.ru/feedbacks/v2/{IMT}": FakeResponse(200, FEEDBACKS),
    }


# ---------------------------------------------------------------- ссылки

@pytest.mark.parametrize("raw, nm", [
    ("https://www.wildberries.ru/catalog/368686013/detail.aspx", 368686013),
    ("https://www.wildberries.ru/catalog/368686013/detail.aspx?size=123&targetUrl=GP", 368686013),
    ("wildberries.ru/catalog/12345/detail.aspx", 12345),
    ("https://global.wildberries.kz/catalog/777/detail.aspx", 777),
    ("https://www.wildberries.ru/catalog/0/search.aspx?nm=4242", 4242),
    ("  368686013  ", 368686013),
])
def test_parse_link(raw, nm):
    assert parse_link(raw) == nm


@pytest.mark.parametrize("raw", ["https://ozon.ru/product/123", "шапка", "https://www.wildberries.ru/"])
def test_parse_link_rejects_garbage(raw):
    with pytest.raises(CollectError, match="не похоже на ссылку"):
        parse_link(raw)


def test_read_links_skips_comments_and_blanks(tmp_path):
    f = tmp_path / "links.txt"
    f.write_text("﻿# конкуренты\n\nhttps://www.wildberries.ru/catalog/1/detail.aspx\n  2  \n", encoding="utf-8")
    assert collect.read_links(f) == ["https://www.wildberries.ru/catalog/1/detail.aspx", "2"]


def test_read_links_missing_file(tmp_path):
    with pytest.raises(CollectError, match="не найден"):
        collect.read_links(tmp_path / "nope.txt")


# ---------------------------------------------------------------- отбор

def select(**kw):
    params = dict(neg_limit=150, pos_limit=100, months=0, now=NOW) | kw
    return select_reviews(FEEDBACKS["feedbacks"], **params)


def test_select_columns_and_buckets():
    df = select()
    assert list(df.columns) == collect.CSV_COLUMNS
    neg, pos = df[df.bucket == "neg"], df[df.bucket == "pos"]
    # негатив: все 1–3★ и 4–5★ с заполненными «Недостатками»
    assert (neg.rating <= 3).sum() == 8
    assert ((neg.rating >= 4) & (neg.cons != "")).sum() == 4
    assert (pos.rating >= 4).all() and (pos.cons == "").all()


def test_select_skips_empty_reviews():
    df = select()
    assert not ((df.text == "") & (df.pros == "") & (df.cons == "")).any()
    non_empty = [f for f in FEEDBACKS["feedbacks"] if any(f[k].strip() for k in ("text", "pros", "cons"))]
    assert len(df) == len(non_empty)


def test_select_sorted_newest_first_within_bucket():
    df = select()
    for _, g in df.groupby("bucket"):
        assert list(g.date) == sorted(g.date, reverse=True)


def test_select_limits_take_newest():
    full = select()
    df = select(neg_limit=3, pos_limit=2)
    assert (df.bucket == "neg").sum() == 3 and (df.bucket == "pos").sum() == 2
    assert list(df[df.bucket == "neg"].date) == list(full[full.bucket == "neg"].date[:3])


def test_select_months_filter():
    assert (select(months=0).date < "2025-10-15").sum() == 2
    df = select(months=12)
    assert (df.date >= "2025-10-15").all()
    assert len(df) == len(select(months=0)) - 2


def test_select_truncates_long_text_and_squashes_spaces():
    fb = [{"createdDate": "2026-10-01T00:00:00Z", "productValuation": 1,
           "text": "а  \n б " + "x" * 2000, "pros": None, "cons": ""}]
    row = select_reviews(fb, neg_limit=5, pos_limit=5, months=0, now=NOW).iloc[0]
    assert row.text.startswith("а б x")
    assert len(row.text) == collect.MAX_FIELD_CHARS + 1 and row.text.endswith("…")
    assert row.pros == ""


def test_select_empty_input():
    df = select_reviews([], neg_limit=5, pos_limit=5, months=12, now=NOW)
    assert df.empty and list(df.columns) == collect.CSV_COLUMNS


# ---------------------------------------------------------------- сеть

def test_basket_guess():
    assert collect.basket_guess(3686) == 21
    assert collect.basket_guess(0) == 1
    assert collect.basket_guess(10**6) is None


def test_fetch_card_uses_guessed_basket_first():
    client = make_client(ok_routes())
    assert client.fetch_card(NM)["imt_id"] == IMT
    assert len(client.session.calls) == 1


def test_fetch_card_falls_back_to_search():
    client = make_client({
        "basket-21.": FakeResponse(404),
        "basket-03.": requests.ConnectionError("dns"),
        f"basket-07.wbbasket.ru/vol3686/part368686/{NM}/": FakeResponse(200, CARD),
    })
    assert client.fetch_card(NM)["imt_id"] == IMT


def test_fetch_card_not_found():
    with pytest.raises(CollectError, match="не найден"):
        make_client({}).fetch_card(NM)


def test_feedbacks_retry_on_5xx_then_ok():
    url = f"feedbacks1.wb.ru/feedbacks/v2/{IMT}"
    client = make_client({url: [FakeResponse(503), FakeResponse(429), FakeResponse(200, FEEDBACKS)]})
    assert len(client.fetch_feedbacks(IMT)["feedbacks"]) == len(FEEDBACKS["feedbacks"])
    assert len(client.session.calls) == 3


def test_feedbacks_fall_back_to_next_host():
    client = make_client({
        "feedbacks1": FakeResponse(404),
        "feedbacks2": requests.ConnectionError("down"),
        f"feedback-view-01.wb.ru/feedbacks/v2/{IMT}": FakeResponse(200, FEEDBACKS),
    })
    assert client.fetch_feedbacks(IMT)["feedbackCount"] == FEEDBACKS["feedbackCount"]


STUB = {"feedbackCount": 0, "feedbacks": None, "valuation": "", "valuationSum": 0}


def test_empty_stub_is_not_final():
    # сервер, который не хранит карточку, отвечает 200 и пустой заглушкой
    client = make_client({
        "feedbacks1": FakeResponse(404),
        "feedbacks2": FakeResponse(404),
        "feedback-view-02": FakeResponse(200, STUB),
        f"feedback-view-01.wb.ru/feedbacks/v2/{IMT}": FakeResponse(200, FEEDBACKS),
    })
    assert len(client.fetch_feedbacks(IMT)["feedbacks"]) == len(FEEDBACKS["feedbacks"])


def test_really_no_feedbacks():
    client = make_client({"feedback": FakeResponse(200, STUB)})
    data = client.fetch_feedbacks(IMT)
    assert data["feedbackCount"] == 0 and len(client.session.calls) == len(collect.FEEDBACK_HOSTS)


def test_site_down_gives_clear_error():
    client = make_client({"feedback": requests.ConnectionError("down")})
    with pytest.raises(CollectError, match="недоступен"):
        client.fetch_feedbacks(IMT)


def test_forbidden_mentions_devtools():
    client = make_client({"wbbasket": FakeResponse(403)})
    with pytest.raises(CollectError, match="403.*README"):
        client.get_json("https://basket-21.wbbasket.ru/x")


def test_non_json_response():
    client = make_client({"feedback": FakeResponse(200, ValueError("html"))})
    with pytest.raises(CollectError, match="не JSON"):
        client.get_json(f"https://feedback-view-02.wb.ru/feedbacks/v2/{IMT}")


def test_pauses_between_requests():
    slept = []
    client = WbClient(session=FakeSession(ok_routes()), sleep=slept.append, pause=(1.0, 2.0))
    client.fetch_card(NM)
    client.fetch_feedbacks(IMT)
    assert len(slept) == 1 and 1.0 <= slept[0] <= 2.0


# ---------------------------------------------------------------- целиком

def test_collect_writes_csv_meta_status(tmp_path):
    links = [
        f"https://www.wildberries.ru/catalog/{NM}/detail.aspx",
        "https://ozon.ru/product/1",          # неверная ссылка
        "999",                                 # товара нет
        f"{NM}",                               # та же карточка повторно
    ]
    results = collect.collect(links, tmp_path, Options(months=0), client=make_client(ok_routes()), log=lambda s: None)

    assert [r["status"] for r in results] == ["ok", "error", "error", "duplicate"]
    assert "не похоже на ссылку" in results[1]["message"]
    assert "не найден" in results[2]["message"]

    df = pd.read_csv(tmp_path / f"{NM}.csv", keep_default_na=False)
    assert list(df.columns) == collect.CSV_COLUMNS
    assert set(df.bucket) == {"neg", "pos"}

    meta = json.loads((tmp_path / f"{NM}.meta.json").read_text(encoding="utf-8"))
    assert meta["imt_id"] == IMT and meta["brand"] == "BODO"
    assert meta["collected"] == {"neg": int((df.bucket == "neg").sum()), "pos": int((df.bucket == "pos").sum())}

    status = json.loads((tmp_path / "_status.json").read_text(encoding="utf-8"))
    assert status == results


def test_collect_continues_after_network_failure(tmp_path):
    routes = ok_routes() | {"vol0/part0/1/": FakeResponse(200, {"imt_id": 5, "imt_name": "x"}),
                            "feedbacks/v2/5": requests.ConnectionError("down")}
    results = collect.collect(["1", str(NM)], tmp_path, Options(months=0), client=make_client(routes), log=lambda s: None)
    assert [r["status"] for r in results] == ["error", "ok"]
    assert "недоступен" in results[0]["message"]


def test_main_exit_codes(tmp_path, monkeypatch, capsys):
    assert collect.main([str(tmp_path / "missing.txt")]) == 2
    empty = tmp_path / "empty.txt"
    empty.write_text("# только комментарий\n", encoding="utf-8")
    assert collect.main([str(empty)]) == 2

    links = tmp_path / "links.txt"
    links.write_text(f"{NM}\n", encoding="utf-8")
    monkeypatch.setattr(collect, "WbClient", lambda: make_client(ok_routes()))
    assert collect.main([str(links), "--out", str(tmp_path / "data"), "--months", "0"]) == 0
    assert "собрано 1 из 1" in capsys.readouterr().out
