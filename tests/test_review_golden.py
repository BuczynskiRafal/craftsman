"""Golden outputs of `craftsman review`: the text a user or an agent reads must not change in a refactor.

A scripted session on a throwaway repository runs every `craftsman review` subcommand through the real CLI and
compares what it printed (stdout, stderr, exit code) with the files under `tests/golden/review/`.

Set `CRAFTSMAN_GOLDEN_SOURCE` to another source tree (a pre-refactor copy) to run the same session against it,
and `CRAFTSMAN_GOLDEN_RECORD=<dir>` to write the golden files into `<dir>` instead of comparing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from craftsman.records import format_timestamp
from craftsman.workspace import HOOKS_MARKER

from tests.ambient import AMBIENT_PLATFORM_ENV_VARS
from tests.support import REPO_ROOT, GitRepo, coverage_note

GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "review"
SOURCE = Path(os.environ.get("CRAFTSMAN_GOLDEN_SOURCE", str(REPO_ROOT))).resolve()
RECORD = os.environ.get("CRAFTSMAN_GOLDEN_RECORD")
COMMANDS = (
    "prepare", "view", "scout", "cover", "brief", "add", "move", "merge", "verdict", "resolve",
    "waive-run", "show", "check", "report", "reset", "inbox", "scratch",
)  # fmt: skip
GIT_DATES = {
    "GIT_AUTHOR_DATE": "2026-01-02T03:04:05+00:00",
    "GIT_COMMITTER_DATE": "2026-01-02T03:04:05+00:00",
}

APP = '''"""A tiny shopping cart."""
import subprocess


def total(items):
    """Sum of prices."""
    return sum(item["price"] for item in items)


def label(item):
    return item["name"].title()


def run(command):
    return subprocess.run(command, shell=True, check=False)
'''
APP_EDITED = APP.replace(
    'return sum(item["price"] for item in items)',
    'return sum(item["price"] for item in items) / len(items)',
).replace("return item[\"name\"].title()", "return item[\"name\"].strip().title()")
UTIL = "def clamp(value, low, high):\n    return max(low, min(value, high))\n"
UTIL_EDITED = UTIL + "\n\ndef scale(value, factor):\n    return value * factor\n"
TEST_APP = "import app\n\n\ndef test_total():\n    assert app.total([{'price': 2}]) == 2\n"
GUIDE = "# Guide\n\nUse `total` to add prices.\n"
GUIDE_EDITED = GUIDE + "\nUse `label` to name an item.\n"
EXTRA = "def double(value):\n    return value * 2\n"
NOTES = "notes\n"
NEW_TEST = "import extra\n\n\ndef test_double():\n    assert extra.double(2) == 4\n"
FINDING = (
    "--file", "app.py", "--line", "7", "--lens", "contracts", "--severity", "major",
    "--title", "Empty cart divides by zero",
    "--scenario", "total([]) evaluates sum([]) / len([]) and raises ZeroDivisionError",
)  # fmt: skip


class Session:
    """The scripted run: one repository, one transcript of command outputs per step."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.steps: dict[str, str] = {}
        root.mkdir(parents=True)
        self.repo = GitRepo(root)
        for key, value in GIT_DATES.items():
            os.environ[key] = value
        self.repo.write("app.py", APP)
        self.repo.write("lib/util.py", UTIL)
        self.repo.write("tests/test_app.py", TEST_APP)
        self.repo.write("docs/guide.md", GUIDE)
        self.repo.write("old_name.py", "def legacy():\n    return 1\n")
        self.base = self.repo.commit("init")
        self.inbox_files: list[str] = []

    def env(self, extra: dict[str, str] | None) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in AMBIENT_PLATFORM_ENV_VARS}
        env.update({"PYTHONPATH": str(SOURCE), "COLUMNS": "80", "LC_ALL": "C.UTF-8", "TZ": "UTC"})
        env.pop("CRAFTSMAN_GOLDEN_RECORD", None)
        if extra:
            env.update(extra)
        return env

    def normalize(self, text: str) -> str:
        for path, token in ((sys.executable, "<PY>"), (str(self.root.resolve()), "<REPO>"), (str(self.root), "<REPO>")):
            # Raw and as a JSON string escapes it (they differ where the path has backslashes, as on Windows).
            text = text.replace(json.dumps(path)[1:-1], token).replace(path, token)
        if os.sep == "\\":
            text = re.sub(r"<REPO>[^\s\"'`]*", lambda m: m.group(0).replace("\\\\", "/").replace("\\", "/"), text)
        for length in (40, 12, 7):
            text = text.replace(self.base[:length], f"<BASE{length}>")
        text = re.sub(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?", "<TIME>", text)
        text = re.sub(r"\d{8}T\d{6}\.\d+Z", "<STAMP>", text)
        text = re.sub(r"\b(cover|add|verdict)-\d+-[0-9a-f]+\.json", r"\1-<PID>-<ID>.json", text)
        text = re.sub(r"\b\d+\.\d+s\b", "<SECONDS>s", text)
        text = re.sub(r"(/craftsman/scratch/)[A-Za-z0-9_.-]+", r"\1<NAME>", text)
        # A recorded command is shortened before `<PY>` replaces the interpreter path, so how much of it is shown (if
        # it is cut at all, and whether the path is quoted) depends on the path on the machine.
        text = re.sub(r"`'?<PY>'?(?: [^`\n]*)?`", "`<PY> …`", text)
        text = re.sub(r"`'?" + re.escape(json.dumps(sys.executable)[1:11]) + r"[^`\n]*…`", "`<PY> …`", text)
        text = re.sub(r"`'?" + re.escape(sys.executable[:10]) + r"[^`\n]*…`", "`<PY> …`", text)
        # argparse quotes the choices in its error on some Python releases and not on others.
        text = re.sub(
            r"\(choose from ([^)\n]*)\)", lambda m: "(choose from " + m.group(1).replace("'", "") + ")", text
        )
        # Before Python 3.13 argparse puts the `...` of a long subcommand list on a line of its own.
        text = re.sub(r"\n {2,}\.\.\.\n", " ...\n", text)
        return re.sub(r"[0-9a-f]{40}", "<SHA>", text)

    def run(
        self, step: str, *args: str, stdin: str | None = None, env: dict[str, str] | None = None
    ) -> str:
        return self.cli(step, "review", *args, stdin=stdin, env=env)

    def cli(
        self, step: str, *args: str, stdin: str | None = None, env: dict[str, str] | None = None
    ) -> str:
        proc = subprocess.run(
            [sys.executable, "-m", "craftsman.cli", *args],
            cwd=self.root,
            env=self.env(env),
            input=stdin,
            capture_output=True,
            encoding="utf-8",
            check=False,
            timeout=120,
        )
        shown = " ".join(args)
        block = f"$ craftsman {shown}\n[exit {proc.returncode}]\n--- stdout\n{proc.stdout}--- stderr\n{proc.stderr}"
        self.steps[step] = self.normalize(block)
        return proc.stdout

    def arm_marker(self, *, task: str) -> None:
        """The review marker the hooks write when they see a `/craft-review` session (fresh for `prepare`)."""
        now = format_timestamp(datetime.now(timezone.utc))
        marker = {
            "platform": "claude", "session_id": "golden", "at": now, "touched_at": now,
            "guarding": True, "deep": False, "task": task,
        }  # fmt: skip
        (self.root / ".git" / "craftsman").mkdir(parents=True, exist_ok=True)
        (self.root / ".git" / "craftsman" / HOOKS_MARKER).write_text(json.dumps(marker) + "\n", encoding="utf-8")

    def packet(self, step: str, path_text: str) -> None:
        path = Path(path_text.strip().splitlines()[-1])
        self.steps[step] = self.normalize(path.read_text(encoding="utf-8"))

    def show_json(self) -> dict[str, object]:
        proc = subprocess.run(
            [sys.executable, "-m", "craftsman.cli", "review", "show", "--json"],
            cwd=self.root,
            env=self.env(None),
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
        loaded = json.loads(proc.stdout)
        assert isinstance(loaded, dict)
        return loaded


def play(session: Session) -> None:
    run, repo = session.run, session.repo
    run("00-help", "--help")
    for name in COMMANDS:
        run(f"01-help-{name}", name, "--help")

    run("02-show-before", "show")
    run("02-show-before-json", "show", "--json")
    run("02-check-before", "check", "--review-only")
    run("02-report-partial-before", "report", "--review-only", "--partial")
    run("02-brief-before", "brief", "W1-1")
    run("02-prepare-nothing", "prepare")

    repo.write("app.py", APP_EDITED)
    repo.write("lib/util.py", UTIL_EDITED)
    repo.write("docs/guide.md", GUIDE_EDITED)
    repo.write("extra.py", EXTRA)
    repo.write("tests/test_extra.py", NEW_TEST)
    repo.git("mv", "old_name.py", "new_name.py")
    repo.write("NOTES.txt", NOTES)

    run("03-prepare-task-and-file-conflict", "prepare", "--task", "x", "--task-file", "-", stdin="y")
    run("03-prepare-scope-conflict", "prepare", "--paths", "app.py", "--staged")
    run("03-prepare-unmatched-path", "prepare", "--paths", "nothing.py")
    run("03-prepare-add-paths-first", "prepare", "--add-paths", "app.py")
    out = run("03-prepare", "prepare", "--task", "Average the prices of a cart and name items nicely")
    run("03-prepare-again", "prepare")
    run("03-show", "show")
    run("03-show-hunks", "show", "--hunks")
    run("03-show-hunks-json", "show", "--hunks", "--json")
    run("03-show-json", "show", "--json")
    packet = run("03-show-packet-path", "show", "--packet-path")
    session.packet("03-packet-file", packet)
    run("03-show-conflict", "show", "--packet-path", "--hunks")
    view = run("04-view-hunks", "view", "--hunks", "H1-H2")
    session.packet("04-view-hunks-file", view)
    view = run("04-view-files", "view", "--files", "app.py,./lib/util.py")
    session.packet("04-view-files-file", view)
    run("04-view-neither", "view")
    run("04-view-unknown-file", "view", "--files", "nope.py")
    run("04-scout-hunk", "scout", "H1")
    run("04-scout-location", "scout", "app.py:7")
    run("04-scout-symbol", "scout", "total")
    run("04-scout-bad", "scout", "")
    assert "Round 1" in out

    run("05-check", "check", "--review-only")
    run("05-check-json", "check", "--review-only", "--json")
    run("05-check-needs-flag-under-nothing", "check")
    run("05-report-draft", "report", "--review-only")
    run("05-report-draft-json", "report", "--review-only", "--json")
    run("05-report-partial-nothing-launched", "report", "--review-only", "--partial")
    run("05-report-partial-needs-flag", "report", "--partial")
    run("05-brief-slot", "brief", "W1-1", env={"CLAUDECODE": "1"})
    run("05-brief-slot-cli", "brief", "W1-1", env={"CRAFTSMAN_SESSION_ID": "golden"})
    run("05-brief-unknown", "brief", "W1-99")
    run("05-brief-wave-gone", "brief", "W2-1")
    run("05-brief-not-a-slot", "brief", "nonsense")
    run("05-brief-both", "brief", "W1-1", "--verifier", "F1")
    run("05-brief-neither", "brief")
    run("05-brief-verifier-unknown", "brief", "--verifier", "F9")

    shown = session.show_json()
    hunks = [str(item["id"]) for item in shown["hunks"] if not item["carried"]]  # type: ignore[attr-defined]
    run("06-cover-no-note", "cover", "--hunks", "H1")
    run("06-cover-no-target", "cover", "--note", coverage_note(["H1"]))
    run("06-cover-short-note", "cover", "--hunks", "H1", "--note", "too short")
    run("06-cover-unknown-hunk", "cover", "--hunks", "H99", "--note", coverage_note(["H99"]))
    run("06-cover-check", "cover", "--hunks", "H1", "--note", coverage_note(["H1"]), "--check")
    run("06-cover-lead", "cover", "--hunks", "H1", "--hunter", "lead", "--note", coverage_note(["H1"]))
    run("06-cover-unknown-slot", "cover", "--hunks", "H1", "--hunter", "W1-99", "--note", coverage_note(["H1"]))
    run("06-cover-first", "cover", "--hunks", "H1", "--note", coverage_note(["H1"]))
    run("06-cover-again", "cover", "--hunks", "H1", "--note", coverage_note(["H1"]))
    run("06-cover-files", "cover", "--files", "lib/util.py", "--note", "util.py:2 clamp(1, 2, 0) returns 0")
    batch = json.dumps([{"hunks": ["H2"], "note": coverage_note(["H2"])}, {"hunks": [], "note": "x"}, {"nope": 1}])
    run("06-cover-batch-rejected", "cover", "--from-json", "-", stdin=batch)
    run("06-cover-from-json-with-flag", "cover", "--from-json", "-", "--note", "x", stdin="[]")
    run("06-cover-batch-not-json", "cover", "--from-json", "-", stdin="{")
    run("06-cover-batch-empty", "cover", "--from-json", "-", stdin="[]")
    run("06-cover-batch-ok", "cover", "--from-json", "-", stdin=json.dumps([{"hunks": ["H2"], "note": coverage_note(["H2"])}]))

    run("07-add-missing-flags", "add", "--lens", "contracts")
    run("07-add-bad-line", "add", *FINDING[:3], "--line", "999", *FINDING[4:])
    run("07-add-check", "add", *FINDING, "--check")
    run("07-add", "add", *FINDING)
    run("07-add-lead", "add", *FINDING[:2], "--line", "11", *FINDING[4:8], "--title", "Label crashes on None name",
        "--scenario", "label({}) raises KeyError", "--hunter", "lead")  # fmt: skip
    bad = json.dumps([
        {"lens": "intent", "file": "app.py", "line": 1, "severity": "minor", "title": "t", "scenario": "s"},
        {"lens": "nope", "file": "missing.py", "line": 0, "severity": "huge", "title": "", "scenario": "s", "extra": 1},
    ])  # fmt: skip
    run("07-add-batch-rejected", "add", "--from-json", "-", stdin=bad)
    good = json.dumps([
        {"lens": "ripple", "file": "lib/util.py", "line": 5, "severity": "minor", "title": "scale has no test",
         "scenario": "scale() is new and nothing calls it", "suggestion": "add a test"},
        {"lens": "craft", "file": "extra.py", "line": 2, "severity": "nit", "title": "Magic number",
         "scenario": "2 is a magic number in double()"},
    ])  # fmt: skip
    run("07-add-batch-ok", "add", "--from-json", "-", stdin=good)
    run("07-move", "move", "F1", "--line", "8")
    run("07-move-bad", "move", "F9", "--line", "1")
    run("07-show-findings", "show")
    run("07-merge", "merge", "F3", "F4", "--title", "Small things")
    run("07-merge-unknown", "merge", "F3", "F99")

    run("08-verdict-missing", "verdict")
    run("08-verdict-check", "verdict", "F1", "--as", "proven", "--evidence", "ran total([]) and got ZeroDivisionError", "--check")
    session.cli("08-run-repro", "run", "--kind", "repro", "--", sys.executable, "-c", "print('ZeroDivisionError')")
    run("08-verdict-proven-needs-run", "verdict", "F1", "--as", "proven", "--evidence", "ran total([]) and got ZeroDivisionError")
    run("08-verdict-proven", "verdict", "F1", "--as", "proven", "--evidence", "ran total([]) and got ZeroDivisionError", "--run", "R1")
    run("08-verdict-unknown", "verdict", "F99", "--as", "proven", "--evidence", "none at all")
    run("08-verdict-batch", "verdict", "--from-json", "-", stdin=json.dumps([
        {"id": "F2", "as": "dismissed", "evidence": "guarded by app.py:12, which only receives named items"},
        {"id": "F3", "as": "probable", "evidence": "static: lib/util.py:5 scale has no caller and no test anywhere"},
    ]))  # fmt: skip
    run("08-verdict-batch-rejected", "verdict", "--from-json", "-", stdin=json.dumps([{"id": "F1"}, {"as": "zzz"}]))
    run("08-resolve-bad", "resolve", "F1", "--as", "fixed")
    run("08-resolve-accept", "resolve", "F3", "--as", "accepted", "--note", "scale is used by a later change")
    run("08-waive-unknown", "waive-run", "R9", "--reason", "gone")
    run("08-show-after-verdicts", "show")
    run("08-check-after-verdicts", "check", "--review-only")

    run("09-cover-rest", "cover", "--hunks", ",".join(hunks), "--note", coverage_note(hunks))
    run("09-check-covered", "check", "--review-only")
    run("09-report", "report", "--review-only")
    run("09-report-json", "report", "--review-only", "--json")
    run("09-report-partial", "report", "--review-only", "--partial")
    session.cli("09-run-tests", "run", "--kind", "test", "--", sys.executable, "-c", "assert True; print('3 passed')")
    run("09-report-with-run", "report", "--review-only")

    repo.write("app.py", APP_EDITED + "\n\ndef discount(value):\n    return value * 0.9\n")
    run("10-show-stale", "show")
    run("10-show-hunks-stale", "show", "--hunks")
    run("10-check-stale", "check", "--review-only")
    run("10-prepare-delta", "prepare")
    run("10-prepare-deep", "prepare", "--deep")
    run("10-show-deep", "show")
    run("10-brief-after-deep", "brief", "W1-1")
    run("10-check-deep", "check", "--review-only")
    run("10-brief-verifier", "brief", "--verifier", "F1,F2", env={"CLAUDECODE": "1"})
    run("10-brief-verifier-cli", "brief", "--verifier", "F1", env={"CRAFTSMAN_SESSION_ID": "golden"})

    run("11-inbox", "inbox", "cover")
    run("11-inbox-bad-kind", "inbox", "nope")
    run("11-scratch", "scratch")
    run("11-reset", "reset")
    run("11-reset-again", "reset")
    run("12-prepare-paths", "prepare", "--paths", "app.py,lib/")
    run("12-prepare-add-paths", "prepare", "--add-paths", "extra.py")
    run("12-prepare-other-scope", "prepare", "--tracked")
    run("12-show-scoped", "show")
    run("12-check-scoped", "check", "--review-only")
    run("12-reset", "reset")
    run("13-prepare-tracked", "prepare", "--tracked")
    run("13-reset", "reset")
    repo.git("add", "app.py")
    run("13-prepare-staged", "prepare", "--staged")
    run("13-reset-again", "reset")

    session.arm_marker(task="Golden task from the review request")
    run("14-prepare-enforced", "prepare")
    run("14-check-enforced", "check", "--review-only")
    run("14-check-enforced-json", "check", "--review-only", "--json")
    run("14-report-partial-nothing-launched", "report", "--review-only", "--partial")
    run("14-cover-needs-hunter", "cover", "--hunks", "H1", "--note", coverage_note(["H1"]))
    run("14-add-needs-hunter", "add", *FINDING)
    run("14-brief-claude", "brief", "W1-1", env={"CLAUDECODE": "1"})
    run("14-brief-again", "brief", "W1-1", env={"CLAUDECODE": "1"})
    run("14-cover-slot", "cover", "--hunter", "W1-1", "--hunks", "H1", "--note", coverage_note(["H1"]))
    run("14-cover-slot-foreign-hunk", "cover", "--hunter", "W1-2", "--hunks", "H99", "--note", coverage_note(["H99"]))
    run("14-add-slot", "add", "--hunter", "W1-1", *FINDING)
    run("14-add-lead", "add", "--hunter", "lead", *FINDING[:2], "--line", "11", *FINDING[4:8], "--title", "Label",
        "--scenario", "label({}) raises KeyError")  # fmt: skip
    run("14-show-enforced", "show")
    run("14-show-hunks-enforced", "show", "--hunks")
    run("14-check-running", "check", "--review-only")
    run("14-prepare-running", "prepare")
    run("14-report-partial", "report", "--review-only", "--partial")
    run("14-report-enforced", "report", "--review-only")
    run("14-report-enforced-json", "report", "--review-only", "--json")
    run("14-reset", "reset")


class GoldenReviewTests(unittest.TestCase):
    def test_scripted_session_prints_the_recorded_text(self) -> None:
        saved = {key: os.environ.get(key) for key in GIT_DATES}
        self.addCleanup(self._restore, saved)
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp) / "repo")
            play(session)
        if RECORD:
            target = Path(RECORD)
            target.mkdir(parents=True, exist_ok=True)
            for name, text in session.steps.items():
                (target / f"{name}.txt").write_text(text, encoding="utf-8")
            return
        expected = sorted(path.stem for path in GOLDEN_DIR.glob("*.txt"))
        self.assertEqual(sorted(session.steps), expected, "the session and the golden files list different steps")
        for name, text in session.steps.items():
            with self.subTest(step=name):
                self.assertEqual((GOLDEN_DIR / f"{name}.txt").read_text(encoding="utf-8"), text)

    @staticmethod
    def _restore(saved: dict[str, str | None]) -> None:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
