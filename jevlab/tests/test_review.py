import json

from jevlab.apps import review
from jevlab.core import Jev, ScriptedBackend

SAMPLE_DIFF = """diff --git a/app/auth.py b/app/auth.py
index 1111111..2222222 100644
--- a/app/auth.py
+++ b/app/auth.py
@@ -10,7 +10,8 @@ def login(user, password):
     if not user:
         return None
-    query = "SELECT * FROM users WHERE name = '%s'" % user
+    query = "SELECT * FROM users WHERE name = '" + user + "'"
+    log.debug(query)
     row = db.execute(query)
     return row
@@ -40,3 +41,4 @@ def logout(session):
     session.clear()
+    audit("logout")
     return True
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,2 +1,2 @@
-# App
+# App (v2)
 docs
"""


def test_parse_unified_diff():
    hunks = review.parse_unified_diff(SAMPLE_DIFF)
    assert [h.file for h in hunks] == ["app/auth.py", "app/auth.py", "README.md"]
    first = hunks[0]
    assert (first.old_start, first.old_count, first.new_start, first.new_count) == (10, 7, 10, 8)
    assert first.header == "def login(user, password):"
    assert [n for n, _ in first.added] == [12, 13]
    assert [n for n, _ in first.removed] == [12]
    assert first.new_range == (12, 13)
    assert hunks[2].added == [(1, "# App (v2)")]


def test_staged_review_with_scripted_backend(tmp_path):
    hunks = review.parse_unified_diff(SAMPLE_DIFF)
    backend = ScriptedBackend(
        [
            {"risk": 4, "category": "security", "needs_test": True, "breaking": False},  # stage1 hunk0
            {"risk": 1, "category": "logic", "needs_test": False, "breaking": False},  # hunk1
            {"risk": 0, "category": "docs", "needs_test": False, "breaking": False},  # hunk2
            {"has_bug": True, "security_issue": 0.9, "needs_human_review": True},  # stage2 hunk0
            {"merge_readiness": 0},  # stage3
        ]
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "auth.py").write_text("import db\n\n\ndef login(user, password):\n" + "    pass\n" * 20, encoding="utf-8")
    report = review.review_hunks(hunks, Jev(backend), root=str(tmp_path))
    assert report.jev_calls == 5
    assert set(backend.calls[0]["questions"]) == {"risk", "category", "needs_test", "breaking"}
    assert "function_context" in backend.calls[3]["state"] and "def login" in backend.calls[3]["state"]["function_context"]
    h0 = report.hunks[0]
    assert h0.risk_level == 4 and h0.category == "security" and h0.deep["has_bug"] >= 0.9
    assert "security concern" in h0.note and "possible bug" in h0.note and "lines 12-13" in h0.note
    assert report.hunks[1].deep is None  # low risk は stage 2 に進まない
    assert report.files["app/auth.py"]["max_risk"] == "critical" and report.files["app/auth.py"]["possible_bugs"] == 1
    assert report.merge_readiness_level == 0
    data = report.to_dict()
    json.dumps(data)
    assert data["merge_readiness_label"].startswith("not mergeable")


def test_html_contains_hunks_and_escapes():
    hunks = review.parse_unified_diff(SAMPLE_DIFF)
    backend = ScriptedBackend(default={"risk": 2, "category": "logic", "needs_test": False, "breaking": False, "has_bug": False, "security_issue": False, "needs_human_review": False, "merge_readiness": 2})
    report = review.review_hunks(hunks, Jev(backend))
    page = review.render_html(report)
    assert page.count("class='hunk'") == 3
    assert "app/auth.py" in page and "README.md" in page
    assert "&quot;SELECT * FROM users" in page or "SELECT * FROM users" in page
    assert "<script" not in page
    assert "user + &quot;&#x27;&quot;" in page  # 追加行がエスケープされて入っている


def test_repo_walk_and_main(tmp_path, capsys):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("ignored", encoding="utf-8")
    hunks = review.repo_to_hunks(str(tmp_path))
    assert [h.file for h in hunks] == ["a.py"] and hunks[0].lines[0] == "+def f():"
    diff_file = tmp_path / "x.diff"
    diff_file.write_text(SAMPLE_DIFF, encoding="utf-8")
    out_html = tmp_path / "report.html"
    assert review.main(["--diff-file", str(diff_file), "--html", str(out_html), "--json", "-", "--backend", "mock", "--root", str(tmp_path)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["hunks"]) == 3 and "merge_readiness_label" in data
    assert "<html" in out_html.read_text(encoding="utf-8")
