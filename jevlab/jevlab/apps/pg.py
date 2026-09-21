"""13. pg-jev (realZachi/pg-jev の再実装): PostgreSQL の行を自然言語条件で絞り込み / 分類 / 順位付け。

2 層構成:
(a) 純 Python 層 — dict の行に対して `jev_where` / `jev_classify` / `jev_rank` / `jev_score`。
    decide_many でバッチ並列、sqlite キャッシュ (行ハッシュ, 質問) で再課金を避ける。
(b) SQL 層 —
    - `install_sql()` : PL/Python (plpython3u) の `jev_where(row jsonb, condition)` / `jev_classify` / `jev_score` を
      定義する pg_jev.sql を生成する (DB サーバ側で `import jevlab` できる必要がある)
    - `rewrite_query(sql)` : `WHERE jev('...')` / `ORDER BY jev_rank('...')` の疑似構文を素の SQL + クライアント側
      後処理に書き換える純関数。plpython が使えない環境向け (`query(conn, sql)` が psycopg で実行)

注意: 行の内容は Jev の外部 API に送られる。個人情報や秘密を含む列は事前に落とすこと。

    python -m jevlab.apps.pg where --json rows.json --condition "返金を求めている" --backend mock
    python -m jevlab.apps.pg classify --json rows.json --categories "返金,不具合,その他"
    python -m jevlab.apps.pg rank --json rows.json --criterion "緊急度が高い"
    python -m jevlab.apps.pg install-sql > pg_jev.sql
    python -m jevlab.apps.pg query --dsn postgres://... --sql "SELECT * FROM tickets WHERE jev('怒っている客') ORDER BY jev_rank('緊急')"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from jevlab.core import Choice, Jev, Noul, Score, questions_to_wire

DEFAULT_CACHE = os.path.join(os.path.expanduser("~"), ".cache", "jevlab", "pg_jev.sqlite")
SCORE_RUBRIC = ["全く当てはまらない", "少し当てはまる", "当てはまる", "強く当てはまる", "完全に当てはまる"]
BATCH_SIZE = 32


# ---------------------------------------------------------------------------
# キャッシュ (sqlite)
# ---------------------------------------------------------------------------


def row_hash(row: Any) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


class JevCache:
    """(行ハッシュ, 質問 wire) → 簡略回答 の sqlite キャッシュ。path=':memory:' でテスト用。"""

    def __init__(self, path: str = DEFAULT_CACHE):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("CREATE TABLE IF NOT EXISTS jev_cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, created REAL DEFAULT (julianday('now')))")
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(rhash: str, question_wire: Mapping[str, Any]) -> str:
        return hashlib.sha256((rhash + json.dumps(question_wire, sort_keys=True, ensure_ascii=False)).encode("utf-8")).hexdigest()

    def get(self, key: str) -> Any | None:
        cur = self.conn.execute("SELECT value FROM jev_cache WHERE key = ?", (key,))
        found = cur.fetchone()
        if found is None:
            self.misses += 1
            return None
        self.hits += 1
        return json.loads(found[0])

    def put_many(self, items: Iterable[tuple[str, Any]]) -> None:
        self.conn.executemany("INSERT OR REPLACE INTO jev_cache (key, value) VALUES (?, ?)", [(k, json.dumps(v, ensure_ascii=False)) for k, v in items])
        self.conn.commit()


# ---------------------------------------------------------------------------
# Python 層
# ---------------------------------------------------------------------------


def _evaluate(jev: Jev, rows: Sequence[Mapping[str, Any]], question: Any, extract, cache: JevCache | None, batch_size: int = BATCH_SIZE) -> list[Any]:
    """各行に 1 質問を投げ、extract(answer) の結果を行順に返す。キャッシュ済みの行は API を呼ばない。"""
    wire = questions_to_wire({"q": question})["q"]
    results: list[Any] = [None] * len(rows)
    todo: list[int] = []
    keys: list[str] = []
    for index, row in enumerate(rows):
        key = JevCache.key(row_hash(row), wire)
        keys.append(key)
        cached = cache.get(key) if cache is not None else None
        if cached is not None:
            results[index] = cached
        else:
            todo.append(index)
    for start in range(0, len(todo), batch_size):
        chunk = todo[start : start + batch_size]
        decisions = jev.decide_many(({"row": rows[i]}, {"q": question}) for i in chunk)
        fresh = []
        for i, decision in zip(chunk, decisions):
            results[i] = extract(decision)
            fresh.append((keys[i], results[i]))
        if cache is not None:
            cache.put_many(fresh)
    return results


def jev_where_mask(rows: Sequence[Mapping[str, Any]], condition: str, jev: Jev | None = None, cache: JevCache | None = None, threshold: float = 0.5) -> list[float]:
    """各行が condition を満たす確率 (0..1)。"""
    jev = jev or Jev()
    return _evaluate(jev, rows, Noul(f"この行は次の条件を満たすか? 条件: {condition}"), lambda d: d.noul("q").noul, cache)


def jev_where(rows: Sequence[Mapping[str, Any]], condition: str, jev: Jev | None = None, cache: JevCache | None = None, threshold: float = 0.5) -> list[Mapping[str, Any]]:
    probs = jev_where_mask(rows, condition, jev, cache, threshold)
    return [row for row, p in zip(rows, probs) if p >= threshold]


def jev_classify(rows: Sequence[Mapping[str, Any]], categories: Sequence[str] | Mapping[str, Any], jev: Jev | None = None, cache: JevCache | None = None, min_confidence: float = 0.0) -> list[str | None]:
    """各行を categories の 1 つに分類。confidence 未満は None。"""
    jev = jev or Jev()
    question = Choice(dict(categories) if isinstance(categories, Mapping) else {c: None for c in categories}, "この行はどのカテゴリに属するか?")

    def extract(decision):
        answer = decision.choice("q")
        return answer.choice if answer.confidence >= min_confidence else None

    return _evaluate(jev, rows, question, extract, cache)


def jev_score(rows: Sequence[Mapping[str, Any]], criterion: str, jev: Jev | None = None, cache: JevCache | None = None) -> list[float]:
    """各行の criterion への当てはまり (0..1)。"""
    jev = jev or Jev()
    return _evaluate(jev, rows, Score(SCORE_RUBRIC, f"この行は次の基準にどれくらい当てはまるか? 基準: {criterion}"), lambda d: d.score("q").normalized(), cache)


def jev_rank(rows: Sequence[Mapping[str, Any]], criterion: str, jev: Jev | None = None, cache: JevCache | None = None) -> list[tuple[Mapping[str, Any], float]]:
    scores = jev_score(rows, criterion, jev, cache)
    return sorted(zip(rows, scores), key=lambda pair: pair[1], reverse=True)


# ---------------------------------------------------------------------------
# SQL 層 (b-1): PL/Python インストーラ
# ---------------------------------------------------------------------------

_PLPY_PREAMBLE = """
    import json
    import jevlab.apps.pg as pg
    if "jev" not in GD:
        GD["jev"] = pg.Jev()
        GD["cache"] = pg.JevCache()
    rows = [json.loads(row) if isinstance(row, str) else row]
"""


def install_sql(schema: str = "public") -> str:
    """plpython3u の jev_where / jev_classify / jev_score を作る SQL を返す。DB サーバ側に jevlab と API キーが必要。"""
    return f"""-- pg_jev.sql: Jev を PostgreSQL 関数として使う (PL/Python)
-- 前提: CREATE EXTENSION plpython3u; サーバの python から `import jevlab` でき、TYPESAFE_API_KEY 等が環境にあること。
-- 注意: 行の内容は外部 API に送信される。

CREATE EXTENSION IF NOT EXISTS plpython3u;

CREATE OR REPLACE FUNCTION {schema}.jev_where(row jsonb, condition text) RETURNS boolean
LANGUAGE plpython3u IMMUTABLE AS $$
{_PLPY_PREAMBLE}
    return pg.jev_where_mask(rows, condition, GD["jev"], GD["cache"])[0] >= 0.5
$$;

CREATE OR REPLACE FUNCTION {schema}.jev_classify(row jsonb, categories text[]) RETURNS text
LANGUAGE plpython3u IMMUTABLE AS $$
{_PLPY_PREAMBLE}
    return pg.jev_classify(rows, list(categories), GD["jev"], GD["cache"])[0]
$$;

CREATE OR REPLACE FUNCTION {schema}.jev_score(row jsonb, criterion text) RETURNS float
LANGUAGE plpython3u IMMUTABLE AS $$
{_PLPY_PREAMBLE}
    return pg.jev_score(rows, criterion, GD["jev"], GD["cache"])[0]
$$;

-- 使い方:
--   SELECT * FROM tickets t WHERE jev_where(to_jsonb(t), '返金を求めている');
--   SELECT jev_classify(to_jsonb(t), ARRAY['返金','不具合','その他']) AS kind, * FROM tickets t;
--   SELECT * FROM tickets t ORDER BY jev_score(to_jsonb(t), '緊急度が高い') DESC;
"""


# ---------------------------------------------------------------------------
# SQL 層 (b-2): 疑似構文の書き換え (純関数)
# ---------------------------------------------------------------------------

_JEV_PRED_RE = re.compile(r"\bjev\(\s*'((?:[^']|'')*)'\s*\)", re.I)
_JEV_RANK_RE = re.compile(r"\bORDER\s+BY\s+jev_rank\(\s*'((?:[^']|'')*)'\s*\)(\s+(?:ASC|DESC))?", re.I)
_TAIL_KEYWORDS = r"(?=\s+(?:GROUP\s+BY|ORDER\s+BY|LIMIT|OFFSET|HAVING|WINDOW|UNION|FETCH|FOR)\b|\s*;?\s*$)"


@dataclass
class RewrittenQuery:
    sql: str
    conditions: list[str] = field(default_factory=list)  # WHERE jev('...') の条件 (AND で結合)
    rank: str | None = None  # ORDER BY jev_rank('...')
    rank_desc: bool = True

    @property
    def needs_jev(self) -> bool:
        return bool(self.conditions) or self.rank is not None


def rewrite_query(sql: str) -> RewrittenQuery:
    """`WHERE a = 1 AND jev('怒っている') ORDER BY jev_rank('緊急') DESC` →
    `WHERE a = 1` + conditions=['怒っている'], rank='緊急'。plpython が無くてもクライアント側で後処理できる形にする。"""
    conditions: list[str] = []

    def replace(match: re.Match[str]) -> str:
        conditions.append(match.group(1).replace("''", "'"))
        return "TRUE"

    out = _JEV_PRED_RE.sub(replace, sql)
    rank = None
    rank_desc = True
    rank_match = _JEV_RANK_RE.search(out)
    if rank_match:
        rank = rank_match.group(1).replace("''", "'")
        rank_desc = (rank_match.group(2) or "DESC").strip().upper() != "ASC"
        out = out[: rank_match.start()] + out[rank_match.end() :]
    # 見た目の整理: WHERE TRUE AND x → WHERE x, x AND TRUE → x, WHERE TRUE (末尾) → 削除
    out = re.sub(r"\bWHERE\s+TRUE\s+AND\s+", "WHERE ", out, flags=re.I)
    out = re.sub(r"\s+AND\s+TRUE\b", "", out, flags=re.I)
    out = re.sub(r"\s*\bWHERE\s+TRUE\b" + _TAIL_KEYWORDS, "", out, flags=re.I)
    out = re.sub(r"\s+;", ";", re.sub(r"[ \t]+", " ", out))
    return RewrittenQuery(out.strip(), conditions, rank, rank_desc)


def apply_rewrite(rows: Sequence[Mapping[str, Any]], rewritten: RewrittenQuery, jev: Jev | None = None, cache: JevCache | None = None, threshold: float = 0.5) -> list[Mapping[str, Any]]:
    """書き換えで取り除いた jev 条件 / 順位付けをクライアント側で適用する。"""
    jev = jev or Jev()
    result = list(rows)
    for condition in rewritten.conditions:
        result = jev_where(result, condition, jev, cache, threshold)
    if rewritten.rank is not None and result:
        ranked = jev_rank(result, rewritten.rank, jev, cache)
        result = [row for row, _ in (ranked if rewritten.rank_desc else reversed(ranked))]
    return result


def connect(dsn: str):
    try:
        import psycopg  # type: ignore
    except ImportError as error:  # pragma: no cover - 依存なし環境
        raise RuntimeError("psycopg がありません: pip install 'jevlab[pg]' (psycopg[binary])") from error
    return psycopg.connect(dsn)


def query(conn: Any, sql: str, jev: Jev | None = None, cache: JevCache | None = None, threshold: float = 0.5, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    """psycopg 接続で疑似構文付き SQL を実行し、dict の行を返す。conn は DB-API 互換なら何でもよい (テストでは sqlite3)。"""
    rewritten = rewrite_query(sql)
    cur = conn.cursor()
    cur.execute(rewritten.sql, tuple(params or ()))
    columns = [d[0] for d in cur.description or []]
    rows = [dict(zip(columns, values)) for values in cur.fetchall()]
    return list(apply_rewrite(rows, rewritten, jev, cache, threshold)) if rewritten.needs_jev else rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_rows(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, Mapping):
        data = data.get("rows", [data])
    return [dict(row) for row in data]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab pg", description=__doc__.splitlines()[0])
    parser.add_argument("--backend", default=None, help="Jev バックエンド名 (typesafe / openrouter / mock)")
    parser.add_argument("--cache", default=DEFAULT_CACHE, help="sqlite キャッシュのパス ('' で無効, ':memory:' で一時)")
    parser.add_argument("--threshold", type=float, default=0.5)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, extra in (("where", "condition"), ("classify", "categories"), ("rank", "criterion")):
        p = sub.add_parser(name)
        p.add_argument("--json", required=True, help="行の JSON ファイル (配列 or {rows: [...]})")
        p.add_argument(f"--{extra}", required=True)
    p_sql = sub.add_parser("install-sql", help="PL/Python 関数を作る SQL を表示")
    p_sql.add_argument("--schema", default="public")
    p_q = sub.add_parser("query", help="psycopg で疑似構文付き SQL を実行")
    p_q.add_argument("--dsn", required=True)
    p_q.add_argument("--sql", required=True)
    p_rw = sub.add_parser("rewrite", help="疑似構文の書き換え結果だけを表示 (DB 不要)")
    p_rw.add_argument("--sql", required=True)
    args = parser.parse_args(argv)

    if args.command == "install-sql":
        print(install_sql(args.schema))
        return 0
    if args.command == "rewrite":
        rewritten = rewrite_query(args.sql)
        print(json.dumps({"sql": rewritten.sql, "conditions": rewritten.conditions, "rank": rewritten.rank, "rank_desc": rewritten.rank_desc}, ensure_ascii=False, indent=2))
        return 0
    jev = Jev(args.backend)
    cache = JevCache(args.cache) if args.cache else None
    if args.command == "query":
        rows = query(connect(args.dsn), args.sql, jev, cache, args.threshold)
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return 0
    rows = _load_rows(args.json)
    if args.command == "where":
        out: Any = jev_where(rows, args.condition, jev, cache, args.threshold)
    elif args.command == "classify":
        labels = jev_classify(rows, [c.strip() for c in args.categories.split(",") if c.strip()], jev, cache)
        out = [{"row": row, "category": label} for row, label in zip(rows, labels)]
    else:
        out = [{"row": row, "score": round(score, 4)} for row, score in jev_rank(rows, args.criterion, jev, cache)]
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
