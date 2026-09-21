# 13. pg-jev — PostgreSQL の行を自然言語条件で絞り込み / 分類 / 順位付け

## 目的

`WHERE jev('怒っている客')` のように SQL の中に自然言語の条件を書き、行ごとに Jev で判定する。

## 元ネタ

- GitHub: `realZachi/pg-jev`

## 2 層構成

### (a) 純 Python 層 (dict の行)

```python
from jevlab.apps.pg import jev_where, jev_classify, jev_rank, jev_score, JevCache
cache = JevCache()                      # ~/.cache/jevlab/pg_jev.sqlite, (行ハッシュ, 質問) をキーに再課金を防ぐ
jev_where(rows, "返金を求めている", cache=cache)
jev_classify(rows, ["返金", "不具合", "その他"], cache=cache)
jev_rank(rows, "緊急度が高い", cache=cache)   # [(row, 0..1), ...] 降順
```

行は `decide_many` で 32 件ずつ並列判定する。

### (b) SQL 層

1. **PL/Python インストーラ**: `install-sql` が `jev_where(row jsonb, condition text) returns boolean` /
   `jev_classify(row jsonb, categories text[]) returns text` / `jev_score(row jsonb, criterion text) returns float` を
   `plpython3u` で定義する SQL を出力する。DB サーバの Python から `import jevlab` でき、API キーが環境にあることが前提。
2. **クライアント側書き換え** (plpython が使えない場合): `rewrite_query(sql)` が `WHERE ... AND jev('...')` と
   `ORDER BY jev_rank('...')` を素の SQL に書き換え、取り除いた条件を `query(conn, sql)` がクライアント側で後処理する。
   `rewrite_query` は純関数なので DB なしでテストできる。

## 使い方

```bash
python -m jevlab.apps.pg where --json rows.json --condition "返金を求めている" --backend mock
python -m jevlab.apps.pg classify --json rows.json --categories "返金,不具合,その他"
python -m jevlab.apps.pg rank --json rows.json --criterion "緊急度が高い"
python -m jevlab.apps.pg install-sql --schema public > pg_jev.sql && psql -f pg_jev.sql
python -m jevlab.apps.pg rewrite --sql "SELECT * FROM t WHERE a=1 AND jev('x') ORDER BY jev_rank('y')"
python -m jevlab.apps.pg query --dsn postgres://user:pw@host/db --sql "SELECT * FROM tickets WHERE jev('怒っている客') ORDER BY jev_rank('緊急')"
```

`--cache` でキャッシュファイルを指定 (`''` で無効、`:memory:` で一時)。

## 必要な追加依存

- `query` サブコマンドのみ `psycopg` (`pip install 'jevlab[pg]'`)。`import` は関数内で遅延するので他の機能は依存なし。
- PL/Python 関数を使う場合は PostgreSQL 側に `plpython3u` 拡張と `jevlab` のインストール。

## 課金・外部送信の注意

**行の内容 (全列) が Jev の外部 API に送信される。** 個人情報や秘密を含む列は `SELECT` で落としてから渡すこと。
行数 x 条件数だけ判定が走る。sqlite キャッシュにより同じ (行, 質問) は再課金されない。

## 制限

- 疑似構文は `jev('...')` (AND 結合) と `ORDER BY jev_rank('...') [ASC|DESC]` のみ。OR やサブクエリ内の `jev()` は非対応。
- クライアント側後処理は `LIMIT` の前に絞り込めない (SQL 側の LIMIT が先に効く)。
