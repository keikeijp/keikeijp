"""6. foreman: 自律コーディングエージェント (Codex / Claude Code CLI) のサブプロセスを Jev が監督する。

元ネタ: thruwire/foreman

- 任意のコマンドを起動して stdout を行単位で読む
- K 行ごと、または一定時間出力が止まったら state を組み立てて Jev に 1 回で聞く
    verdict           Choice {continue, verify, stop, intervene}
    progress          Score  [stuck, slow, steady, fast]
    claims_completion Noul   (完了を主張しているか)
    looks_looping     Noul   (同じ出力を繰り返しているか)
- verify   → 検証コマンド (pytest 等) を実行し、結果をエージェントの stdin に返す (無理なら記録だけ)
- stop     → プロセスを終了
- intervene→ 促しメッセージを stdin に送る (回数上限を超えたら stop)

オフライン用に `FakeAgentProcess` (台本の行を返す) と `--dry-run` を用意している。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from jevlab.core import Choice, Jev, Noul, Score

TESTS_RE = re.compile(r"(\d+\s+(passed|failed|errors?)\b|\bpytest\b|\bnpm test\b|\bcargo test\b|\bgo test\b|\btests?\s+(passed|failed)\b|\bFAILED\b|\bPASSED\b|\bOK\b\s*$)", re.IGNORECASE | re.MULTILINE)
DONE_RE = re.compile(r"\b(done|completed?|finished|implemented|all set|task (is )?complete|successfully (implemented|completed|fixed)|ready for review)\b", re.IGNORECASE)

QUESTIONS = {
    "verdict": Choice(
        {
            "continue": "the agent is making progress; leave it alone",
            "verify": "the agent claims (or seems) to have finished a meaningful step; run the verification command now",
            "stop": "the agent is finished and verified, or hopelessly stuck / destructive; terminate it",
            "intervene": "the agent is looping, drifting from the task, or waiting; send it a nudge",
        },
        "You supervise an autonomous coding agent. Based on its recent output, what should the supervisor do next?",
    ),
    "progress": Score(["stuck: no meaningful new output", "slow: small steps, lots of retries", "steady: clear progress on the task", "fast: rapid, confident progress"], "How is the agent progressing?"),
    "claims_completion": Noul("Does the agent claim the task is complete or done in its recent output?"),
    "looks_looping": Noul("Is the agent repeating the same actions or output over and over (looping)?"),
}


# ---------------------------------------------------------------------------
# エージェントプロセス
# ---------------------------------------------------------------------------


class FakeAgentProcess:
    """台本の行を順に返すオフライン用エージェント。`send` された文字列は `inbox` に溜まる。"""

    supports_stdin = True

    def __init__(self, lines: Sequence[str], exit_code: int = 0):
        self._lines = list(lines)
        self.exit_code = exit_code
        self.inbox: list[str] = []
        self.terminated = False

    @property
    def alive(self) -> bool:
        return bool(self._lines) and not self.terminated

    def readline(self, timeout: float) -> str | None:
        if not self.alive:
            return None
        line = self._lines.pop(0)
        return None if line is None else line  # None を挟むと「アイドル」を再現できる

    def send(self, text: str) -> None:
        self.inbox.append(text)

    def terminate(self) -> None:
        self.terminated = True

    @property
    def returncode(self) -> int | None:
        return None if self.alive else self.exit_code


class SubprocessAgent:
    """`subprocess.Popen` をラップし、別スレッドで stdout を行キューに流す。"""

    def __init__(self, cmd: str, cwd: str | None = None, env: dict[str, str] | None = None):
        self.proc = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.supports_stdin = self.proc.stdin is not None
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._queue.put(line.rstrip("\n"))
        self._queue.put(None)

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None or not self._queue.empty()

    def readline(self, timeout: float) -> str | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def send(self, text: str) -> None:
        if self.proc.stdin and self.proc.poll() is None:
            try:
                self.proc.stdin.write(text.rstrip("\n") + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self.supports_stdin = False

    def terminate(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    @property
    def returncode(self) -> int | None:
        return self.proc.poll()


# ---------------------------------------------------------------------------
# 設定・セッションログ
# ---------------------------------------------------------------------------


@dataclass
class ForemanConfig:
    task: str
    verify_cmd: str | None = None
    interval_lines: int = 20
    idle_seconds: float = 30.0
    max_minutes: float = 30.0
    tail_lines: int = 40
    max_interventions: int = 3
    min_confidence: float = 0.4
    stop_when_verified: bool = True
    dry_run: bool = True
    cwd: str | None = None
    intervene_message: str = "[foreman] You seem stuck or looping. Re-read the task, state your next concrete step, and continue."


@dataclass
class Session:
    """監督セッションの記録。`log_path` があれば JSONL に追記する。"""

    task: str
    events: list[dict[str, Any]] = field(default_factory=list)
    log_path: str | None = None
    verdicts: Counter = field(default_factory=Counter)
    verified: bool | None = None
    stopped_by: str | None = None

    def log(self, kind: str, **data: Any) -> dict[str, Any]:
        event = {"t": round(time.time(), 3), "kind": kind, **data}
        self.events.append(event)
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        return event

    def summary(self) -> dict[str, Any]:
        return {"task": self.task, "checks": self.verdicts.total(), "verdicts": dict(self.verdicts), "verified": self.verified, "stopped_by": self.stopped_by, "events": len(self.events)}


# ---------------------------------------------------------------------------
# 監督本体
# ---------------------------------------------------------------------------


def repeat_ratio(lines: Sequence[str]) -> float:
    """末尾の行のうち重複している割合 (ループ検出のヒューリスティック)。"""
    stripped = [line.strip() for line in lines if line.strip()]
    if len(stripped) < 4:
        return 0.0
    counts = Counter(stripped)
    return 1.0 - len(counts) / len(stripped)


def git_changed_files(cwd: str | None) -> list[str]:
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [line[3:].strip() for line in out.stdout.splitlines() if line.strip()][:50]


class Foreman:
    def __init__(
        self,
        jev: Jev,
        agent: Any,
        config: ForemanConfig,
        session: Session | None = None,
        files_changed_fn: Callable[[], list[str]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        verify_runner: Callable[[str], tuple[int, str]] | None = None,
    ):
        self.jev = jev
        self.agent = agent
        self.config = config
        self.session = session or Session(task=config.task)
        self.files_changed_fn = files_changed_fn or (lambda: git_changed_files(config.cwd))
        self.clock = clock
        self.verify_runner = verify_runner or self._run_shell
        self.output: list[str] = []
        self.started = clock()
        self.interventions = 0
        self._lines_since_check = 0

    # -- state / 判定 --------------------------------------------------------

    def build_state(self) -> dict[str, Any]:
        tail = self.output[-self.config.tail_lines :]
        joined = "\n".join(tail)
        return {
            "task": self.config.task[:1000],
            "recent_output": [line[:300] for line in tail],
            "elapsed_seconds": round(self.clock() - self.started, 1),
            "total_lines": len(self.output),
            "files_changed": self.files_changed_fn(),
            "tests_ran": bool(TESTS_RE.search(joined)),
            "claims_done": bool(DONE_RE.search(joined)),
            "repeat_ratio": round(repeat_ratio(tail), 2),
            "interventions_so_far": self.interventions,
            "last_verification": self.session.verified,
        }

    def check(self) -> dict[str, Any]:
        state = self.build_state()
        decision = self.jev.decide(state, QUESTIONS)
        verdict = decision.choice("verdict")
        result = {
            "verdict": verdict.choice,
            "confidence": round(verdict.confidence, 3),
            "progress": decision.score("progress").level,
            "claims_completion": decision.noul("claims_completion").yes,
            "looks_looping": decision.noul("looks_looping").yes or state["repeat_ratio"] >= 0.6,
            "claims_done_regex": state["claims_done"],
        }
        if verdict.confidence < self.config.min_confidence and verdict.choice != "continue":
            result["low_confidence"] = True
            result["verdict"] = "continue"  # 自信がなければ何もしない (安全側)
        self.session.verdicts[result["verdict"]] += 1
        self.session.log("check", state=state, **result)
        self._lines_since_check = 0
        return result

    def handle(self, result: dict[str, Any]) -> bool:
        """判定を実行する。戻り値 False で監督ループを終える。"""
        verdict = result["verdict"]
        if verdict == "intervene" or (verdict == "continue" and result["looks_looping"]):
            self.interventions += 1
            if self.interventions > self.config.max_interventions:
                self.session.stopped_by = "too_many_interventions"
                self.session.log("stop", reason=self.session.stopped_by)
                self.agent.terminate()
                return False
            self._send(self.config.intervene_message)
            self.session.log("intervene", n=self.interventions)
            return True
        if verdict == "verify":
            return self.verify()
        if verdict == "stop":
            self.session.stopped_by = "jev_stop"
            self.session.log("stop", reason=self.session.stopped_by)
            self.agent.terminate()
            return False
        return True

    def verify(self) -> bool:
        if not self.config.verify_cmd:
            self.session.log("verify", skipped=True, reason="no verify command")
            return True
        if self.config.dry_run:
            self.session.log("verify", skipped=True, reason="dry_run", cmd=self.config.verify_cmd)
            return True
        code, output = self.verify_runner(self.config.verify_cmd)
        passed = code == 0
        self.session.verified = passed
        tail = "\n".join(output.splitlines()[-30:])
        self.session.log("verify", cmd=self.config.verify_cmd, returncode=code, passed=passed, output_tail=tail)
        self._send(f"[foreman] verification `{self.config.verify_cmd}` {'PASSED' if passed else 'FAILED'} (exit {code}).\n{tail}")
        if passed and self.config.stop_when_verified:
            self.session.stopped_by = "verified"
            self.session.log("stop", reason=self.session.stopped_by)
            self.agent.terminate()
            return False
        return True

    def _send(self, text: str) -> None:
        if getattr(self.agent, "supports_stdin", False):
            self.agent.send(text)
            self.session.log("send", text=text[:500])
        else:
            self.session.log("note", text=text[:500])

    def _run_shell(self, cmd: str) -> tuple[int, str]:
        proc = subprocess.run(cmd, shell=True, cwd=self.config.cwd, capture_output=True, text=True, timeout=600)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    # -- ループ --------------------------------------------------------------

    def run(self) -> Session:
        self.session.log("start", task=self.config.task, dry_run=self.config.dry_run)
        deadline = self.started + self.config.max_minutes * 60
        while self.agent.alive:
            if self.clock() > deadline:
                self.session.stopped_by = "timeout"
                self.session.log("stop", reason="timeout")
                self.agent.terminate()
                break
            line = self.agent.readline(self.config.idle_seconds)
            if line is None:
                if not self.agent.alive:
                    break
                self.session.log("idle")
                if not self.handle(self.check()):
                    break
                continue
            self.output.append(line)
            self.session.log("line", text=line[:500])
            self._lines_since_check += 1
            if self._lines_since_check >= self.config.interval_lines and not self.handle(self.check()):
                break
        if self.session.stopped_by is None:
            # エージェントが自分で終了した。最後の出力について 1 回だけ判定する (verify / 完了主張の確認)
            self.session.log("agent_exited", returncode=self.agent.returncode)
            if self._lines_since_check:
                self.handle(self.check())
            self.session.stopped_by = self.session.stopped_by or "agent_exited"
        self.session.log("end", summary=self.session.summary())
        return self.session


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

DEMO_LINES = [
    "Reading task...",
    "Inspecting repository layout",
    "Editing src/app.py",
    "Running pytest -q",
    "3 passed in 0.12s",
    "Task completed successfully.",
]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jevlab foreman", description="自律コーディングエージェントを Jev で監督する")
    parser.add_argument("--cmd", help='起動するコマンド (例: "codex exec --full-auto \\"...\\"")')
    parser.add_argument("--task", required=True, help="エージェントに与えたタスクの説明")
    parser.add_argument("--verify", default=None, help='検証コマンド (例: "pytest -q")')
    parser.add_argument("--interval", type=int, default=20, help="この行数ごとに Jev へ判定を聞く")
    parser.add_argument("--idle", type=float, default=30.0, help="出力が止まってから判定するまでの秒数")
    parser.add_argument("--max-minutes", type=float, default=30.0)
    parser.add_argument("--max-interventions", type=int, default=3)
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--log", default=None, help="セッションログ JSONL")
    parser.add_argument("--dry-run", action="store_true", help="サブプロセスを起動せず内蔵デモ出力で動かす。検証コマンドも実行しない")
    parser.add_argument("--backend", default=None)
    args = parser.parse_args(argv)

    if not args.dry_run and not args.cmd:
        parser.error("--cmd が必要です (--dry-run ならデモ出力を使います)")
    config = ForemanConfig(
        task=args.task,
        verify_cmd=args.verify,
        interval_lines=args.interval,
        idle_seconds=args.idle,
        max_minutes=args.max_minutes,
        max_interventions=args.max_interventions,
        dry_run=args.dry_run,
        cwd=args.cwd or os.getcwd(),
    )
    agent: Any = FakeAgentProcess(DEMO_LINES) if args.dry_run else SubprocessAgent(args.cmd, cwd=config.cwd)
    session = Session(task=args.task, log_path=args.log)
    foreman = Foreman(Jev(args.backend), agent, config, session, files_changed_fn=(lambda: []) if args.dry_run else None)
    session = foreman.run()
    print(json.dumps(session.summary(), ensure_ascii=False, indent=2))
    return 0 if session.stopped_by in (None, "verified", "jev_stop", "agent_exited") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
