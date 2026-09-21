import io
import json

import pytest

from jevlab.apps import rules
from jevlab.core import Jev, ScriptedBackend


def test_parse_front_matter_variants():
    meta, body = rules.parse_front_matter('---\ndescription: "API rules"\npaths: [src/api/**/*.py, "tests/api/*"]\ntags:\n  - api\n  - python\nalways: false\n---\n\n# Body\ntext\n')
    assert meta == {"description": "API rules", "paths": ["src/api/**/*.py", "tests/api/*"], "tags": ["api", "python"], "always": False}
    assert body == "# Body\ntext"
    assert rules.parse_front_matter("no front matter\n") == ({}, "no front matter\n")
    assert rules.parse_front_matter("---\nunterminated\n") == ({}, "---\nunterminated\n")


def test_glob_match_semantics():
    assert rules.glob_match("src/api/**/*.py", "src/api/users.py")
    assert rules.glob_match("src/api/**/*.py", "src/api/v2/deep/users.py")
    assert not rules.glob_match("src/api/**/*.py", "src/web/users.py")
    assert rules.glob_match("*.md", "docs/deep/readme.md")  # ディレクトリなしパターンはファイル名で判定
    assert rules.glob_match("tests/*", "tests/test_x.py") and not rules.glob_match("tests/*", "tests/sub/test_x.py")
    assert rules.glob_match("**/*.{ts,tsx}", "app/x.tsx")


@pytest.fixture
def rules_dir(tmp_path):
    d = tmp_path / "rules"
    d.mkdir()
    (d / "api.md").write_text("---\ndescription: API conventions\npaths: [src/api/**]\n---\nUse FastAPI routers.\n", encoding="utf-8")
    (d / "testing.md").write_text("---\ndescription: How to write tests\ntags: [test]\n---\nUse pytest fixtures.\n", encoding="utf-8")
    (d / "security.md").write_text("---\ndescription: Security checklist\n---\nNever log secrets.\n", encoding="utf-8")
    (d / "base.md").write_text("---\nalways: true\n---\nBe concise.\n", encoding="utf-8")
    return d


def test_glob_selection_and_jev_selection(rules_dir):
    loaded = rules.load_rules(str(rules_dir))
    assert [r.name for r in loaded] == ["api", "base", "security", "testing"]
    backend = ScriptedBackend([{"relevant": 0.2}, {"relevant": 0.9}])  # security, testing の順
    sel = rules.select_rules(Jev(backend), loaded, "add a unit test for the login endpoint", ["src/api/login.py"])
    assert [r.name for r in sel.selected] == ["base", "api", "testing"]
    assert sel.reasons["base"] == "always" and sel.reasons["api"] == "glob" and sel.reasons["testing"].startswith("jev:")
    assert "security" in sel.skipped and sel.jev_calls == 2
    state = backend.calls[0]["state"]
    assert state["rule"]["name"] == "security" and state["edited_files"] == ["src/api/login.py"] and "request" in state
    assert "Use pytest fixtures." in sel.render() and "Never log secrets." not in sel.render()


def test_max_chars_cap_and_no_jev(rules_dir):
    loaded = rules.load_rules(str(rules_dir))
    sel = rules.select_rules(None, loaded, "anything", ["src/api/x.py"], max_chars=15)
    assert [r.name for r in sel.selected] == ["base"]  # api は上限超えで落ちる
    assert sel.skipped["api"].startswith("over max_chars") and sel.jev_calls == 0


def test_hook_output_shape_and_main(rules_dir, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": "write tests for the api", "cwd": str(rules_dir.parent)})))
    assert rules.main(["--hook", "--rules-dir", "rules", "--files", "src/api/a.py", "--backend", "mock"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"hookSpecificOutput"}
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "Use FastAPI routers." in out["hookSpecificOutput"]["additionalContext"]
    assert rules.hook_output(rules.Selection([], {}, {})) == {}


def test_install_hook_snippet(capsys):
    assert rules.main(["--install-hook", "--rules-dir", ".claude/rules"]) == 0
    snippet = json.loads(capsys.readouterr().out)
    cmd = snippet["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert "--hook" in cmd and ".claude/rules" in cmd
