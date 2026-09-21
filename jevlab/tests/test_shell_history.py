import json

from jevlab.apps import shell_history as sh
from jevlab.core import Jev, MockBackend, ScriptedBackend

HISTORY = """: 1700000000:0;git status
: 1700000001:0;git push origin main
: 1700000002:5;docker compose up -d \\
  --build
ls -la
: 1700000003:0;git pull --rebase
git push origin main
"""


def test_parse_extended_and_plain_history(tmp_path):
    entries = sh.parse_history(HISTORY)
    assert [e.command for e in entries] == ["git status", "git push origin main", "docker compose up -d \n  --build", "ls -la", "git pull --rebase", "git push origin main"]
    assert entries[0].timestamp == 1700000000 and entries[3].timestamp == 0
    path = tmp_path / "hist"
    path.write_text(HISTORY, encoding="utf-8")
    assert len(sh.load_history(path)) == 6
    assert sh.load_history(tmp_path / "missing") == []


def test_prefilter_candidates_rank_prefix_frequency_recency():
    history = sh.parse_history(HISTORY)
    cands = sh.candidates("git pu", history, cwd="/home/me/proj")
    assert cands[:2] == ["git push origin main", "git pull --rebase"]  # 頻度 2 の push が先
    assert "git status" not in cands and "ls -la" not in cands
    assert "git status" in sh.candidates("gs", history)  # 部分列一致
    assert sh.candidates("", history)[0] == "git push origin main"  # 空入力は新しさ + 頻度
    assert sh.candidates("git status", history) == ["git status"] or "git status" not in sh.candidates("git status", history)
    assert sh.match_score("git status", "git status") == 0.0
    assert sh.candidates("x", [], limit=5) == []


def test_pick_with_scripted_backend_and_threshold():
    cands = ["git push origin main", "git pull --rebase", "git push --force"]
    backend = ScriptedBackend([{"pick": "1"}, {"pick": {"0": 0.34, "1": 0.33, "2": 0.33}}])
    jev = Jev(backend)
    result = sh.pick(jev, "git pu", cands, {"cwd": "/tmp", "last_commands": ["ls"]})
    assert result.best == "git pull --rebase" and result.source == "jev" and result.ranked[0]["command"] == "git pull --rebase"
    assert backend.calls[0]["state"]["typed"] == "git pu"
    assert list(backend.calls[0]["questions"]["pick"]["criteria"].values()) == cands
    low = sh.pick(jev, "git pu", cands, {}, threshold=0.5)
    assert low.best is None and low.confidence < 0.5
    assert sh.pick(jev, "x", ["only"], {}).best == "only"
    assert sh.pick(jev, "x", [], {}).best is None


def test_pick_with_timeout_uses_cache(tmp_path):
    backend = ScriptedBackend([{"pick": "0"}])
    jev = Jev(backend)
    cache = sh.PickCache(tmp_path / "cache.json")
    ctx = {"cwd": "/tmp"}
    first = sh.pick_with_timeout(jev, "git", ["git status", "git log"], ctx, timeout=5, cache=cache)
    second = sh.pick_with_timeout(jev, "git", ["git status", "git log"], ctx, timeout=5, cache=sh.PickCache(tmp_path / "cache.json"))
    assert first.best == second.best == "git status" and second.source == "cache" and len(backend.calls) == 1


def test_main_pick_with_history_file_and_install(tmp_path, capsys):
    path = tmp_path / "zsh_history"
    path.write_text(HISTORY, encoding="utf-8")
    assert sh.main(["--backend", "mock", "pick", "--typed", "git pu", "--cwd", str(tmp_path), "--history", str(path), "--cache", "", "--json", "--threshold", "0"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["best"].startswith("git pu") and out["source"] == "jev"
    assert sh.main(["--backend", "mock", "pick", "--typed", "git pu", "--history", str(path), "--cache", "", "--plain", "--threshold", "0"]) == 0
    assert capsys.readouterr().out.strip().startswith("git pu")
    assert sh.main(["--backend", "mock", "candidates", "--typed", "git", "--history", str(path)]) == 0
    assert "git status" in capsys.readouterr().out
    assert sh.main(["--install"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("source ") and line.endswith("shell_history.zsh") and sh.ZSH_WIDGET_PATH.is_file()
    assert "jev-history-pick" in sh.ZSH_WIDGET_PATH.read_text(encoding="utf-8")
