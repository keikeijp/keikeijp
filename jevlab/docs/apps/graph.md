# 15. neo4jev — グラフ探索で次にたどる関係を Jev に選ばせる

## 目的

グラフ (Neo4j またはメモリ内 JSON) を、開始ノードから目的 (自然言語) に向かって 1 ホップずつ探索する。
各ノードで隣接関係を `-[:ACTED_IN]-> Movie {title: The Matrix}` / `<-[:DIRECTED]- Person {name: ...}` の形で
Choice の選択肢にし、`next` (次にたどる関係) と `reached_goal` (今のノードで目的達成か) を 1 回の decide で聞く。

## 元ネタ

- GitHub: `jexp/neo4jev`

## 使い方

```bash
# 内蔵デモグラフ (映画 / 人物)
python -m jevlab.apps.graph --start keanu --goal "Keanu が出演した映画の監督を見つける" --backend mock --mermaid
python -m jevlab.apps.graph --graph mygraph.json --start n1 --goal "..." --json --dot

# Neo4j (neo4j ドライバが必要)
python -m jevlab.apps.graph --neo4j-uri bolt://localhost:7687 --neo4j-user neo4j --neo4j-password secret \
    --start "Keanu Reeves" --id-property name --goal "..."
```

グラフ JSON の形: `{"nodes": [{"id", "label", "props"}], "edges": [{"from", "to", "type"}]}`。

探索の停止条件: `reached_goal >= --goal-threshold` (既定 0.8)、`stop` を選んだ、未訪問の隣接がない、confidence < 0.3、`--max-steps` (既定 8)。
訪問済みノードは候補から外す。

出力: 経路 + 各ステップの判断 (JSON)、`--mermaid` で Mermaid、`--dot` で Graphviz DOT。

## 必要な追加依存

- Neo4j を使う場合のみ `neo4j` (`pip install 'jevlab[graph]'`)。関数内で遅延 import。

## 課金・外部送信の注意

1 ステップ = Jev 1 回。state には goal、現在ノードの label / props、経路、隣接関係の説明 (最大 20 件、props は先頭 3 キー x 40 文字) が入る。
ノードの props に秘密情報がある場合は Graph 実装側で落とすこと。

## 制限

- 1 ホップずつの貪欲探索でバックトラックしない。行き止まりなら停止する。
- Neo4j はノード識別に `--id-property` の値を使う (既定 `id`)。
