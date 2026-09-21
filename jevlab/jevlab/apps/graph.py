"""15. neo4jev (jexp/neo4jev の再実装): グラフ探索で「次にたどる関係」を Jev に選ばせる。

各ノードで、隣接関係を "-[:ACTED_IN]-> Movie {title: Matrix}" の形で Choice の選択肢にし、
`next` (どの関係をたどるか) と `reached_goal` (今のノードで目的を満たしたか) を 1 回の decide で聞く。
訪問済みノードは候補から外し、reached_goal が閾値以上なら停止する。

- `Graph` プロトコル: `neighbors(node_id)` → [(rel_type, direction, node_id, node_label, props_summary)], `node(node_id)`
- `InMemoryGraph` (JSON) / `Neo4jGraph` (neo4j ドライバを遅延 import、Cypher)
- `explore(jev, graph, start, goal, max_steps)` → 経路 + 各ステップの判断
- `to_mermaid` / `to_dot` で可視化

    python -m jevlab.apps.graph --start keanu --goal "Keanu が出演した映画の監督を見つける" --backend mock --mermaid
    python -m jevlab.apps.graph --graph mygraph.json --start n1 --goal "..." --json
    python -m jevlab.apps.graph --neo4j-uri bolt://localhost:7687 --neo4j-user neo4j --neo4j-password ... --start "Keanu Reeves" --id-property name --goal "..."
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, NamedTuple, Protocol

from jevlab.core import Choice, Jev, Noul

MAX_CHOICES = 20
STOP = "stop"


class Neighbor(NamedTuple):
    rel_type: str
    direction: str  # "out" (n)-[r]->(m) / "in" (n)<-[r]-(m)
    node_id: str
    node_label: str
    props_summary: str


class Graph(Protocol):
    def neighbors(self, node_id: str) -> list[Neighbor]: ...

    def node(self, node_id: str) -> dict[str, Any]: ...


def summarize_props(props: Mapping[str, Any], max_keys: int = 3, max_len: int = 40) -> str:
    """主要な props を `{key: value, ...}` に短くまとめる。"""
    parts = []
    for key in list(props)[:max_keys]:
        value = props[key]
        text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        parts.append(f"{key}: {text[:max_len]}")
    return "{" + ", ".join(parts) + "}"


def describe(neighbor: Neighbor) -> str:
    """Choice の説明文: -[:REL]-> Label {props} / <-[:REL]- Label {props}"""
    if neighbor.direction == "out":
        return f"-[:{neighbor.rel_type}]-> {neighbor.node_label} {neighbor.props_summary}"
    return f"<-[:{neighbor.rel_type}]- {neighbor.node_label} {neighbor.props_summary}"


# ---------------------------------------------------------------------------
# グラフ実装
# ---------------------------------------------------------------------------


class InMemoryGraph:
    """{"nodes": [{"id", "label", "props"}], "edges": [{"from", "to", "type", "props"?}]} を持つメモリ内グラフ。"""

    def __init__(self, data: Mapping[str, Any]):
        self.nodes: dict[str, dict[str, Any]] = {}
        for node in data.get("nodes", []):
            self.nodes[str(node["id"])] = {"id": str(node["id"]), "label": str(node.get("label", "Node")), "props": dict(node.get("props", {}))}
        self.edges: list[dict[str, Any]] = [{"from": str(e["from"]), "to": str(e["to"]), "type": str(e.get("type", "RELATED")), "props": dict(e.get("props", {}))} for e in data.get("edges", [])]

    @classmethod
    def from_json(cls, path: str | os.PathLike[str]) -> "InMemoryGraph":
        with open(path, encoding="utf-8") as handle:
            return cls(json.load(handle))

    def node(self, node_id: str) -> dict[str, Any]:
        if node_id not in self.nodes:
            raise KeyError(f"ノード {node_id!r} がありません")
        return self.nodes[node_id]

    def neighbors(self, node_id: str) -> list[Neighbor]:
        result = []
        for edge in self.edges:
            if edge["from"] == node_id and edge["to"] in self.nodes:
                other = self.nodes[edge["to"]]
                result.append(Neighbor(edge["type"], "out", other["id"], other["label"], summarize_props(other["props"])))
            elif edge["to"] == node_id and edge["from"] in self.nodes:
                other = self.nodes[edge["from"]]
                result.append(Neighbor(edge["type"], "in", other["id"], other["label"], summarize_props(other["props"])))
        return result


class Neo4jGraph:
    """neo4j ドライバ経由 (遅延 import)。ノードは `id_property` の値で識別する (既定 'id'; 'name' などにも変えられる)。"""

    def __init__(self, uri: str, user: str, password: str, database: str | None = None, id_property: str = "id"):
        try:
            import neo4j  # type: ignore
        except ImportError as error:  # pragma: no cover - 依存なし環境
            raise RuntimeError("neo4j ドライバがありません: pip install 'jevlab[graph]'") from error
        self.driver = neo4j.GraphDatabase.driver(uri, auth=(user, password))
        self.database = database
        self.id_property = id_property

    def _run(self, cypher: str, **params: Any) -> list[Any]:
        with self.driver.session(database=self.database) as session:
            return [record for record in session.run(cypher, **params)]

    def node(self, node_id: str) -> dict[str, Any]:
        records = self._run("MATCH (n) WHERE n[$prop] = $id RETURN labels(n) AS labels, properties(n) AS props LIMIT 1", prop=self.id_property, id=node_id)
        if not records:
            raise KeyError(f"ノード {node_id!r} がありません")
        record = records[0]
        return {"id": node_id, "label": ":".join(record["labels"]), "props": dict(record["props"])}

    def neighbors(self, node_id: str) -> list[Neighbor]:
        records = self._run(
            "MATCH (n)-[r]-(m) WHERE n[$prop] = $id RETURN type(r) AS type, startNode(r) = n AS out, m[$prop] AS id, labels(m) AS labels, properties(m) AS props",
            prop=self.id_property,
            id=node_id,
        )
        return [Neighbor(r["type"], "out" if r["out"] else "in", str(r["id"]), ":".join(r["labels"]), summarize_props(dict(r["props"]))) for r in records if r["id"] is not None]


# ---------------------------------------------------------------------------
# 探索
# ---------------------------------------------------------------------------


@dataclass
class Step:
    node_id: str
    label: str
    reached_goal: float
    chosen: str | None  # 選んだ関係の説明 (stop / None は停止)
    next_id: str | None
    confidence: float
    options: list[str] = field(default_factory=list)


@dataclass
class ExploreResult:
    start: str
    goal: str
    path: list[str]
    steps: list[Step]
    reached: bool
    edges: list[tuple[str, str, str]] = field(default_factory=list)  # (from, rel, to) 経路上の辺

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start, "goal": self.goal, "path": self.path, "reached": self.reached, "steps": [asdict(s) for s in self.steps], "edges": [list(e) for e in self.edges]}


def explore(jev: Jev, graph: Graph, start: str, goal: str, max_steps: int = 8, goal_threshold: float = 0.8, min_confidence: float = 0.3) -> ExploreResult:
    """start から goal に向かって Jev が 1 ホップずつ選ぶ。訪問済みは候補から外す。"""
    current = start
    visited = {start}
    path = [start]
    steps: list[Step] = []
    edges: list[tuple[str, str, str]] = []
    reached = False
    for _ in range(max_steps):
        node = graph.node(current)
        candidates = [n for n in graph.neighbors(current) if n.node_id not in visited][:MAX_CHOICES]
        options = {str(i): describe(n) for i, n in enumerate(candidates)}
        options[STOP] = "これ以上たどらず終了する"
        state = {
            "goal": goal,
            "current": {"id": current, "label": node["label"], "props": node["props"]},
            "path": path,
            "relationships": [describe(n) for n in candidates],
        }
        decision = jev.decide(state, {"reached_goal": Noul("現在のノードで目的 (goal) は達成されたか?"), "next": Choice(options, "目的に近づくために次にたどる関係はどれか?")})
        reached_prob = decision.noul("reached_goal").noul
        answer = decision.choice("next")
        if reached_prob >= goal_threshold:
            steps.append(Step(current, node["label"], round(reached_prob, 3), None, None, round(answer.confidence, 3), list(options.values())))
            reached = True
            break
        if not candidates or answer.choice == STOP or not answer.choice.isdigit() or answer.confidence < min_confidence:
            steps.append(Step(current, node["label"], round(reached_prob, 3), STOP, None, round(answer.confidence, 3), list(options.values())))
            break
        chosen = candidates[int(answer.choice)]
        steps.append(Step(current, node["label"], round(reached_prob, 3), describe(chosen), chosen.node_id, round(answer.confidence, 3), list(options.values())))
        edges.append((current, chosen.rel_type, chosen.node_id) if chosen.direction == "out" else (chosen.node_id, chosen.rel_type, current))
        visited.add(chosen.node_id)
        path.append(chosen.node_id)
        current = chosen.node_id
    return ExploreResult(start, goal, path, steps, reached, edges)


# ---------------------------------------------------------------------------
# 可視化
# ---------------------------------------------------------------------------


def _node_caption(graph: Graph, node_id: str) -> str:
    try:
        node = graph.node(node_id)
    except KeyError:
        return node_id
    props = node.get("props", {})
    name = props.get("name") or props.get("title") or node_id
    return f"{node['label']}: {name}"


def _safe(text: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in text)


def to_mermaid(graph: Graph, result: ExploreResult) -> str:
    lines = ["graph LR"]
    for node_id in result.path:
        marker = ":::goal" if result.reached and node_id == result.path[-1] else ""
        lines.append(f'  {_safe(node_id)}["{_node_caption(graph, node_id)}"]{marker}')
    for src, rel, dst in result.edges:
        lines.append(f"  {_safe(src)} -- {rel} --> {_safe(dst)}")
    lines.append("  classDef goal fill:#d1fae5,stroke:#059669;")
    return "\n".join(lines)


def to_dot(graph: Graph, result: ExploreResult) -> str:
    lines = ["digraph jev {", "  rankdir=LR;"]
    for node_id in result.path:
        extra = ', style=filled, fillcolor="#d1fae5"' if result.reached and node_id == result.path[-1] else ""
        lines.append(f'  "{node_id}" [label="{_node_caption(graph, node_id)}"{extra}];')
    for src, rel, dst in result.edges:
        lines.append(f'  "{src}" -> "{dst}" [label="{rel}"];')
    lines.append("}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# デモグラフ (映画 / 人物)
# ---------------------------------------------------------------------------

DEMO_GRAPH: dict[str, Any] = {
    "nodes": [
        {"id": "keanu", "label": "Person", "props": {"name": "Keanu Reeves", "born": 1964}},
        {"id": "carrie", "label": "Person", "props": {"name": "Carrie-Anne Moss", "born": 1967}},
        {"id": "laurence", "label": "Person", "props": {"name": "Laurence Fishburne", "born": 1961}},
        {"id": "lana", "label": "Person", "props": {"name": "Lana Wachowski", "born": 1965}},
        {"id": "lilly", "label": "Person", "props": {"name": "Lilly Wachowski", "born": 1967}},
        {"id": "chad", "label": "Person", "props": {"name": "Chad Stahelski", "born": 1968}},
        {"id": "matrix", "label": "Movie", "props": {"title": "The Matrix", "released": 1999}},
        {"id": "reloaded", "label": "Movie", "props": {"title": "The Matrix Reloaded", "released": 2003}},
        {"id": "wick", "label": "Movie", "props": {"title": "John Wick", "released": 2014}},
        {"id": "speed", "label": "Movie", "props": {"title": "Speed", "released": 1994}},
        {"id": "memento", "label": "Movie", "props": {"title": "Memento", "released": 2000}},
    ],
    "edges": [
        {"from": "keanu", "to": "matrix", "type": "ACTED_IN", "props": {"role": "Neo"}},
        {"from": "keanu", "to": "reloaded", "type": "ACTED_IN"},
        {"from": "keanu", "to": "wick", "type": "ACTED_IN"},
        {"from": "keanu", "to": "speed", "type": "ACTED_IN"},
        {"from": "carrie", "to": "matrix", "type": "ACTED_IN"},
        {"from": "carrie", "to": "memento", "type": "ACTED_IN"},
        {"from": "laurence", "to": "matrix", "type": "ACTED_IN"},
        {"from": "lana", "to": "matrix", "type": "DIRECTED"},
        {"from": "lilly", "to": "matrix", "type": "DIRECTED"},
        {"from": "lana", "to": "reloaded", "type": "DIRECTED"},
        {"from": "chad", "to": "wick", "type": "DIRECTED"},
        {"from": "chad", "to": "matrix", "type": "STUNT_DOUBLE_IN"},
    ],
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab graph", description=__doc__.splitlines()[0])
    parser.add_argument("--graph", default=None, help="グラフ JSON (省略時は内蔵の映画デモ)")
    parser.add_argument("--start", required=True, help="開始ノード id")
    parser.add_argument("--goal", required=True, help="探索の目的 (自然言語)")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--goal-threshold", type=float, default=0.8)
    parser.add_argument("--mermaid", action="store_true", help="Mermaid 文字列を出力")
    parser.add_argument("--dot", action="store_true", help="Graphviz DOT を出力")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--neo4j-uri", default=None, help="指定すると Neo4j を使う (bolt://...)")
    parser.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", ""))
    parser.add_argument("--neo4j-database", default=None)
    parser.add_argument("--id-property", default="id", help="Neo4j でノード識別に使うプロパティ名")
    parser.add_argument("--backend", default=None, help="Jev バックエンド名 (typesafe / openrouter / mock)")
    args = parser.parse_args(argv)

    graph: Graph
    if args.neo4j_uri:
        graph = Neo4jGraph(args.neo4j_uri, args.neo4j_user, args.neo4j_password, args.neo4j_database, args.id_property)
    elif args.graph:
        graph = InMemoryGraph.from_json(args.graph)
    else:
        graph = InMemoryGraph(DEMO_GRAPH)
    result = explore(Jev(args.backend), graph, args.start, args.goal, args.max_steps, args.goal_threshold)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"path: {' -> '.join(result.path)}  reached={result.reached}")
        for step in result.steps:
            print(f"  {step.node_id} ({step.label}) goal={step.reached_goal:.2f} -> {step.chosen or 'stop'} [conf={step.confidence:.2f}]")
    if args.mermaid:
        print(to_mermaid(graph, result))
    if args.dot:
        print(to_dot(graph, result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
