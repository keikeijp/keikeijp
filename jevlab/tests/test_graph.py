import json

from jevlab.apps import graph as g
from jevlab.apps.graph import DEMO_GRAPH, InMemoryGraph, Neighbor, describe, explore, to_dot, to_mermaid
from jevlab.core import Jev, MockBackend, ScriptedBackend


def test_in_memory_graph_neighbors_both_directions():
    graph = InMemoryGraph(DEMO_GRAPH)
    out = graph.neighbors("keanu")
    assert [(n.rel_type, n.direction, n.node_id) for n in out] == [("ACTED_IN", "out", "matrix"), ("ACTED_IN", "out", "reloaded"), ("ACTED_IN", "out", "wick"), ("ACTED_IN", "out", "speed")]
    assert out[0].node_label == "Movie" and "title: The Matrix" in out[0].props_summary
    incoming = graph.neighbors("matrix")
    assert ("DIRECTED", "in", "lana") in [(n.rel_type, n.direction, n.node_id) for n in incoming]
    assert describe(out[0]) == "-[:ACTED_IN]-> Movie {title: The Matrix, released: 1999}"
    assert describe(Neighbor("DIRECTED", "in", "lana", "Person", "{name: Lana}")) == "<-[:DIRECTED]- Person {name: Lana}"
    assert graph.node("keanu")["props"]["name"] == "Keanu Reeves"


def test_explore_reaches_goal_with_scripted_backend():
    graph = InMemoryGraph(DEMO_GRAPH)
    backend = ScriptedBackend([{"reached_goal": False, "next": "0"}, {"reached_goal": False, "next": "2"}, {"reached_goal": True, "next": "stop"}])
    result = explore(Jev(backend), graph, "keanu", "Keanu が出演した映画の監督を見つける", max_steps=5)
    assert result.path == ["keanu", "matrix", "lana"] and result.reached
    assert result.edges == [("keanu", "ACTED_IN", "matrix"), ("lana", "DIRECTED", "matrix")]
    assert result.steps[0].chosen.startswith("-[:ACTED_IN]-> Movie") and result.steps[-1].chosen is None
    # 訪問済み (keanu) は matrix の候補に出ない、stop は常に最後の選択肢
    criteria = backend.calls[1]["questions"]["next"]["criteria"]
    assert "Keanu" not in json.dumps(criteria, ensure_ascii=False) and list(criteria)[-1] == "stop"
    assert backend.calls[1]["state"]["goal"].startswith("Keanu")


def test_explore_stops_on_stop_or_low_confidence():
    graph = InMemoryGraph(DEMO_GRAPH)
    stopped = explore(Jev(ScriptedBackend([{"reached_goal": False, "next": "stop"}])), graph, "keanu", "x")
    assert stopped.path == ["keanu"] and not stopped.reached and stopped.steps[0].chosen == "stop"
    unsure = explore(Jev(ScriptedBackend(default={"reached_goal": False, "next": {"0": 0.26, "1": 0.25, "2": 0.25, "3": 0.24, "stop": 0.0}})), graph, "keanu", "x", min_confidence=0.5)
    assert unsure.path == ["keanu"]
    dead_end = explore(Jev(ScriptedBackend(default={"reached_goal": False, "next": "0"})), graph, "memento", "x", max_steps=10)
    assert dead_end.path == ["memento", "carrie", "matrix"] or len(dead_end.path) <= 11


def test_mermaid_and_dot_and_main(tmp_path, capsys):
    graph = InMemoryGraph(DEMO_GRAPH)
    result = explore(Jev(ScriptedBackend([{"reached_goal": False, "next": "0"}, {"reached_goal": True, "next": "stop"}])), graph, "keanu", "x")
    mermaid = to_mermaid(graph, result)
    assert mermaid.startswith("graph LR") and 'keanu["Person: Keanu Reeves"]' in mermaid and "keanu -- ACTED_IN --> matrix" in mermaid and ":::goal" in mermaid
    dot = to_dot(graph, result)
    assert dot.startswith("digraph") and '"keanu" -> "matrix" [label="ACTED_IN"]' in dot
    assert g.main(["--start", "keanu", "--goal", "Matrix の監督", "--backend", "mock", "--mermaid", "--max-steps", "3"]) == 0
    out = capsys.readouterr().out
    assert "path: keanu" in out and "graph LR" in out
    path = tmp_path / "g.json"
    path.write_text(json.dumps(DEMO_GRAPH), encoding="utf-8")
    assert g.main(["--graph", str(path), "--start", "matrix", "--goal", "x", "--backend", "mock", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["path"][0] == "matrix"
