import json
import sqlite3

from jevlab.apps import pg
from jevlab.core import FunctionBackend, Jev, MockBackend, ScriptedBackend

ROWS = [
    {"id": 1, "text": "二重に課金された。返金してほしい", "priority": "high"},
    {"id": 2, "text": "ログインするとクラッシュする", "priority": "low"},
    {"id": 3, "text": "領収書の宛名を変更したい", "priority": "low"},
]


def _rule_backend():
    def rule(state, questions):
        row = state["row"]
        out = {}
        for name, q in questions.items():
            if q["type"] == "noul":
                out[name] = "返金" in row["text"]
            elif q["type"] == "choice":
                out[name] = "返金" if "返金" in row["text"] else "不具合" if "クラッシュ" in row["text"] else "その他"
            else:
                out[name] = 4 if row["priority"] == "high" else 1
        return out

    return FunctionBackend(rule)


def test_python_layer_where_classify_rank():
    jev = Jev(_rule_backend())
    assert [r["id"] for r in pg.jev_where(ROWS, "返金を求めている", jev)] == [1]
    assert pg.jev_classify(ROWS, ["返金", "不具合", "その他"], jev) == ["返金", "不具合", "その他"]
    ranked = pg.jev_rank(ROWS, "緊急度が高い", jev)
    assert ranked[0][0]["id"] == 1 and ranked[0][1] >= 0.9 and all(s <= 1.0 for _, s in ranked)
    assert pg.jev_classify(ROWS, ["返金", "不具合"], Jev(ScriptedBackend(default={"q": {"返金": 0.5, "不具合": 0.5}})), min_confidence=0.9) == [None, None, None]


def test_sqlite_cache_avoids_second_call():
    backend = MockBackend()
    jev = Jev(backend)
    cache = pg.JevCache(":memory:")
    first = pg.jev_where_mask(ROWS, "返金", jev, cache)
    calls = len(backend.calls)
    assert calls == 3 and cache.misses == 3
    second = pg.jev_where_mask(ROWS + [{"id": 4, "text": "new"}], "返金", jev, cache)
    assert second[:3] == first and len(backend.calls) == calls + 1 and cache.hits == 3
    pg.jev_where_mask(ROWS, "別の条件", jev, cache)  # 質問が違えばキャッシュは効かない
    assert len(backend.calls) == calls + 4


def test_sql_rewriter_is_pure_and_handles_layouts():
    r = pg.rewrite_query("SELECT * FROM tickets WHERE jev('怒っている客')")
    assert r.sql == "SELECT * FROM tickets" and r.conditions == ["怒っている客"] and r.rank is None and r.needs_jev
    r = pg.rewrite_query("SELECT * FROM t WHERE a = 1 AND jev('x') ORDER BY jev_rank('緊急') DESC LIMIT 5")
    assert r.sql == "SELECT * FROM t WHERE a = 1 LIMIT 5" and r.conditions == ["x"] and r.rank == "緊急" and r.rank_desc
    r = pg.rewrite_query("SELECT * FROM t WHERE jev('x') AND b = 2 AND jev('it''s') order by jev_rank('r') asc;")
    assert r.sql == "SELECT * FROM t WHERE b = 2;" and r.conditions == ["x", "it's"] and r.rank == "r" and not r.rank_desc
    r = pg.rewrite_query("SELECT * FROM t WHERE jev('x') GROUP BY a")
    assert r.sql == "SELECT * FROM t GROUP BY a"
    plain = pg.rewrite_query("SELECT * FROM t WHERE a = 1")
    assert plain.sql == "SELECT * FROM t WHERE a = 1" and not plain.needs_jev


def test_query_with_dbapi_connection_post_filters():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE tickets (id INTEGER, text TEXT, priority TEXT)")
    conn.executemany("INSERT INTO tickets VALUES (?, ?, ?)", [(r["id"], r["text"], r["priority"]) for r in ROWS])
    jev = Jev(_rule_backend())
    rows = pg.query(conn, "SELECT * FROM tickets WHERE id > 0 AND jev('返金を求めている')", jev, pg.JevCache(":memory:"))
    assert [r["id"] for r in rows] == [1]
    rows = pg.query(conn, "SELECT id, priority FROM tickets ORDER BY jev_rank('緊急度') DESC", jev)
    assert rows[0]["id"] == 1 and len(rows) == 3
    assert len(pg.query(conn, "SELECT * FROM tickets", jev)) == 3


def test_install_sql_contains_functions_and_main(tmp_path, capsys):
    sql = pg.install_sql()
    for name in ("plpython3u", "FUNCTION public.jev_where(row jsonb, condition text) RETURNS boolean", "FUNCTION public.jev_classify(row jsonb, categories text[]) RETURNS text", "FUNCTION public.jev_score(row jsonb, criterion text) RETURNS float", "import jevlab"):
        assert name in sql
    assert pg.main(["install-sql", "--schema", "app"]) == 0 and "app.jev_where" in capsys.readouterr().out
    path = tmp_path / "rows.json"
    path.write_text(json.dumps(ROWS, ensure_ascii=False), encoding="utf-8")
    assert pg.main(["--backend", "mock", "--cache", ":memory:", "--threshold", "0", "where", "--json", str(path), "--condition", "返金してほしい"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 3
    assert pg.main(["--backend", "mock", "--cache", "", "classify", "--json", str(path), "--categories", "返金,不具合,その他"]) == 0
    assert all("category" in item for item in json.loads(capsys.readouterr().out))
    assert pg.main(["rewrite", "--sql", "SELECT 1 FROM t WHERE jev('c')"]) == 0
    assert json.loads(capsys.readouterr().out)["conditions"] == ["c"]
