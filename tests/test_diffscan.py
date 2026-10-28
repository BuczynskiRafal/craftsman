#!/usr/bin/env python3
"""Behavioral tests for diff snapshots, hunk digests, scopes, references and risk signals."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from craftsman import diffscan
from craftsman.diffscan import (
    DiffScanError,
    Hunk,
    WorktreeIndex,
    detect_signals,
    git_dir,
    resolve_base_sha,
    snapshot,
)

from tests.support import GitRepo, change_hunk


def numbered_lines(count: int, prefix: str = "line") -> str:
    return "".join(f"{prefix} {index}\n" for index in range(1, count + 1))


class RepoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = GitRepo(Path(tmp.name).resolve())


class SnapshotTests(RepoTestCase):
    def test_modified_file_reports_its_line_numbers(self) -> None:
        self.repo.write("app.py", "def total(items):\n" + numbered_lines(9, "    x ="))
        base = self.repo.commit()
        source = (self.repo.root / "app.py").read_text(encoding="utf-8")
        self.repo.write("app.py", source.replace("    x = 6\n", "    y = 6\n"))

        snap = snapshot(self.repo.root, base)

        self.assertEqual(snap.files, ("app.py",))
        (hunk,) = snap.hunks
        self.assertEqual(
            (hunk.id, hunk.path, hunk.status, hunk.old_path), ("H1", "app.py", "modified", None)
        )
        self.assertEqual((hunk.old_start, hunk.old_len, hunk.new_start, hunk.new_len), (4, 7, 4, 7))
        self.assertEqual(hunk.header, "def total(items):")
        self.assertEqual(hunk.added_lines, ((7, "    y = 6"),))
        self.assertEqual(hunk.removed_lines, ((7, "    x = 6"),))
        self.assertEqual((snap.added, snap.deleted), (1, 1))
        self.assertEqual(snap.base_sha, base)

    def test_file_status_is_reported_per_change(self) -> None:
        self.repo.write("gone.txt", "bye\n")
        self.repo.write("old/name.txt", numbered_lines(10))
        self.repo.write("moved.txt", "same\n")
        base = self.repo.commit()
        (self.repo.root / "gone.txt").unlink()
        (self.repo.root / "new").mkdir()
        self.repo.git("mv", "old/name.txt", "new/name.txt")
        content = numbered_lines(10).replace("line 5\n", "line five\n")
        self.repo.write("new/name.txt", content)
        self.repo.git("mv", "moved.txt", "moved2.txt")
        self.repo.write("staged.txt", "hello\n")
        self.repo.git("add", "staged.txt")

        by_path = {hunk.path: hunk for hunk in snapshot(self.repo.root, base).hunks}

        self.assertEqual(by_path["gone.txt"].status, "deleted")
        self.assertEqual(by_path["gone.txt"].removed_lines, ((1, "bye"),))
        self.assertEqual(by_path["new/name.txt"].status, "renamed")
        self.assertEqual(by_path["new/name.txt"].old_path, "old/name.txt")
        self.assertEqual(by_path["new/name.txt"].added_lines, ((5, "line five"),))
        self.assertEqual(by_path["moved2.txt"].lines, ("~ renamed from moved.txt",))
        self.assertEqual(by_path["staged.txt"].status, "added")
        self.assertEqual(by_path["staged.txt"].added_lines, ((1, "hello"),))

    def test_binary_changes_are_skipped_but_get_a_digested_placeholder(self) -> None:
        self.repo.write("logo.bin", b"\x00\x01one")
        base = self.repo.commit()
        self.repo.write("logo.bin", b"\x00\x01two")
        self.repo.write("new.bin", b"\x00raw")

        first = snapshot(self.repo.root, base)
        self.repo.write("logo.bin", b"\x00\x01three")
        second = snapshot(self.repo.root, base)

        self.assertEqual(first.skipped, ("logo.bin", "new.bin"))
        self.assertEqual([hunk.lines for hunk in first.hunks], [("~ binary file changed",)] * 2)
        self.assertEqual((first.hunks[0].new_start, first.hunks[0].new_len), (0, 0))
        self.assertNotEqual(first.hunks[0].digest, second.hunks[0].digest)
        self.assertEqual(first.hunks[1].digest, second.hunks[1].digest)
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_untracked_text_file_is_synthesized_as_a_new_file(self) -> None:
        base = self.repo.commit_empty()
        self.repo.write("notes/todo.py", "a = 1\nb = 2")

        snap = snapshot(self.repo.root, base)

        (hunk,) = snap.hunks
        self.assertEqual((hunk.path, hunk.status), ("notes/todo.py", "added"))
        self.assertEqual((hunk.old_start, hunk.old_len, hunk.new_start, hunk.new_len), (0, 0, 1, 2))
        self.assertEqual(hunk.lines, ("+a = 1", "+b = 2", "\\ No newline at end of file"))
        self.assertEqual(hunk.added_lines, ((1, "a = 1"), (2, "b = 2")))
        self.assertEqual(snap.added, 2)

    def test_generated_or_ignored_files_stay_out_of_the_snapshot(self) -> None:
        base = self.repo.commit_empty()
        self.repo.write("pkg/mod.py", "x = 1\n")
        before = snapshot(self.repo.root, base)

        self.repo.write("pkg/__pycache__/mod.cpython-312.pyc", b"\x00cache")
        self.repo.write(".pytest_cache/v/cache/lastfailed", "{}\n")
        self.repo.write("web/node_modules/lib/index.js", "module.exports = 1;\n")
        self.repo.write("out/report.html", "<html></html>\n")
        self.repo.write(".craftsmanignore", "out/\n")
        with_ignore_file = snapshot(self.repo.root, base)

        self.assertEqual(with_ignore_file.files, (".craftsmanignore", "pkg/mod.py"))
        (self.repo.root / ".craftsmanignore").unlink()
        self.assertIn("out/report.html", snapshot(self.repo.root, base).files)
        self.assertEqual(before.files, ("pkg/mod.py",))

    def test_untracked_files_of_any_size_produce_hunks(self) -> None:
        base = self.repo.commit_empty()
        self.repo.write("empty.txt", "")
        self.repo.write("huge.log", "x" * (1_048_576 + 1))

        snap = snapshot(self.repo.root, base)

        self.assertEqual(snap.files, ("empty.txt", "huge.log"))
        self.assertEqual(snap.skipped, ("huge.log",))
        self.assertEqual(snap.hunks[0].lines, ("~ empty file added",))
        self.assertEqual(snap.hunks[1].lines, ("~ file too large to inline: 1048577 bytes",))
        self.assertFalse(snap.is_empty)

    @unittest.skipIf(os.name == "nt", "the executable bit is not tracked on Windows checkouts")
    def test_mode_only_change_produces_a_placeholder_hunk(self) -> None:
        script = self.repo.write("run.sh", "echo hi\n")
        base = self.repo.commit()
        script.chmod(0o755)

        snap = snapshot(self.repo.root, base)

        self.assertEqual(snap.hunks[0].lines, ("~ mode 100644 -> 100755",))
        self.assertEqual(snap.files, ("run.sh",))

    def test_paths_with_special_characters_are_unquoted(self) -> None:
        self.repo.write("dir with space/a b.txt", "one\n")
        self.repo.write('q"uote.txt', "one\n")
        base = self.repo.commit()
        self.repo.write("dir with space/a b.txt", "two\n")
        self.repo.write('q"uote.txt', "two\n")
        self.repo.write("new dir/c d.txt", "three\n")

        snap = snapshot(self.repo.root, base)

        self.assertEqual(snap.files, ("dir with space/a b.txt", "new dir/c d.txt", 'q"uote.txt'))
        self.assertEqual([hunk.path for hunk in snap.hunks], list(snap.files))

    def test_line_endings_are_kept_exactly(self) -> None:
        self.repo.write("win.txt", b"a\r\nb\r\n")
        base = self.repo.commit()
        self.repo.write("win.txt", b"a\r\nc\r\n")
        self.repo.write("tail.txt", b"x\r\ny")

        hunks = {hunk.path: hunk for hunk in snapshot(self.repo.root, base).hunks}

        self.assertEqual(hunks["win.txt"].lines, (" a\r", "-b\r", "+c\r"))
        self.assertEqual(hunks["tail.txt"].lines, ("+x\r", "+y", "\\ No newline at end of file"))

    def test_hunk_header_without_lengths_means_one_line(self) -> None:
        self.repo.write("one.txt", "before\n")
        base = self.repo.commit()
        self.repo.write("one.txt", "after\n")

        (hunk,) = snapshot(self.repo.root, base).hunks

        self.assertEqual((hunk.old_start, hunk.old_len, hunk.new_start, hunk.new_len), (1, 1, 1, 1))

    @unittest.skipIf(os.name == "nt", "symlinks need extra privileges on Windows")
    def test_untracked_special_entries_are_reported(self) -> None:
        base = self.repo.commit_empty()
        (self.repo.root / "link").symlink_to("target.txt")
        nested = self.repo.root / "vendor"
        nested.mkdir()
        GitRepo(nested).write("lib.py", "x = 1\n")

        hunks = {hunk.path: hunk for hunk in snapshot(self.repo.root, base).hunks}

        self.assertEqual(hunks["link"].lines, ("+target.txt", "\\ No newline at end of file"))
        self.assertEqual(hunks["vendor/"].lines, ("~ untracked directory or special file",))

    def test_user_git_settings_do_not_change_the_output_format(self) -> None:
        self.repo.write("a.py", "value = 1\n")
        base = self.repo.commit()
        self.repo.write("a.py", "value = 2\n")
        expected = snapshot(self.repo.root, base)
        for key, value in (
            ("diff.noprefix", "true"),
            ("diff.mnemonicPrefix", "true"),
            ("diff.submodule", "log"),
            ("color.ui", "always"),
            ("grep.column", "true"),
            ("grep.lineNumber", "false"),
        ):
            self.repo.git("config", key, value)

        self.assertEqual(snapshot(self.repo.root, base), expected)
        self.assertEqual(WorktreeIndex(self.repo.root).references("value")[0].line, 1)

    def test_fingerprint_changes_only_with_the_content(self) -> None:
        self.repo.write("a.txt", "a\n")
        base = self.repo.commit()
        self.repo.write("a.txt", "b\n")
        self.repo.write("u.txt", "u\n")

        original = snapshot(self.repo.root, base)
        self.assertEqual(snapshot(self.repo.root, base), original)

        self.repo.write("a.txt", "c\n")
        tracked_edit = snapshot(self.repo.root, base).fingerprint
        self.repo.write("a.txt", "b\n")
        self.assertEqual(snapshot(self.repo.root, base).fingerprint, original.fingerprint)
        self.repo.write("u.txt", "v\n")
        untracked_edit = snapshot(self.repo.root, base).fingerprint

        self.assertEqual(len({original.fingerprint, tracked_edit, untracked_edit}), 3)

    def test_digest_survives_a_shift_caused_by_a_hunk_above(self) -> None:
        self.repo.write("long.txt", numbered_lines(40))
        base = self.repo.commit()
        self.repo.write("long.txt", numbered_lines(40).replace("line 30\n", "line thirty\n"))
        before = snapshot(self.repo.root, base).hunks[0]

        self.repo.write(
            "long.txt", "new top\n" + numbered_lines(40).replace("line 30\n", "line thirty\n")
        )
        after = snapshot(self.repo.root, base).hunks

        self.assertEqual(len(after), 2)
        self.assertEqual(after[1].new_start, before.new_start + 1)
        self.assertEqual(after[1].digest, before.digest)

    def test_identical_hunks_in_one_file_are_disambiguated(self) -> None:
        filler = "same\n" * 10  # identical context and function header around both edits
        self.repo.write("twins.txt", filler + "start\n" + filler + "start\n" + filler)
        base = self.repo.commit()
        self.repo.write("twins.txt", filler + "begin\n" + filler + "begin\n" + filler)

        hunks = snapshot(self.repo.root, base).hunks

        self.assertEqual(len(hunks), 2)
        self.assertEqual(hunks[0].lines, hunks[1].lines)
        self.assertEqual(hunks[1].digest, f"{hunks[0].digest}#2")

    def test_all_files_share_one_path_order(self) -> None:
        self.repo.write("b.txt", "b\n")
        base = self.repo.commit()
        self.repo.write("b.txt", "bb\n")
        self.repo.write("a.txt", "a\n")
        self.repo.write("c.txt", "c\n")

        snap = snapshot(self.repo.root, base)

        self.assertEqual(
            [(hunk.id, hunk.path) for hunk in snap.hunks],
            [("H1", "a.txt"), ("H2", "b.txt"), ("H3", "c.txt")],
        )

    def test_subdirectory_cwd_yields_root_relative_paths(self) -> None:
        self.repo.write("pkg/mod.py", "x = 1\n")
        base = self.repo.commit()
        self.repo.write("pkg/mod.py", "x = 2\n")
        self.repo.write("top.txt", "t\n")

        self.assertEqual(snapshot(self.repo.root / "pkg", base), snapshot(self.repo.root, base))
        self.assertEqual(snapshot(self.repo.root / "pkg", base).files, ("pkg/mod.py", "top.txt"))

    def test_clean_tree_has_an_empty_snapshot(self) -> None:
        self.repo.write("README.md", "# hi\n")
        base = self.repo.commit()
        clean = snapshot(self.repo.root, base)
        self.assertTrue(clean.is_empty)
        self.assertFalse(clean.docs_only)

        self.repo.write("README.md", "# hello\n")
        self.repo.write("docs/guide.rst", "Guide\n")
        self.assertTrue(snapshot(self.repo.root, base).docs_only)
        self.repo.write("requirements.txt", "requests==2.0\n")
        self.assertFalse(snapshot(self.repo.root, base).docs_only)

    def test_invalid_locations_or_revisions_raise(self) -> None:
        base = self.repo.commit_empty()
        with tempfile.TemporaryDirectory() as outside:
            with self.assertRaisesRegex(DiffScanError, "not inside a git work tree"):
                snapshot(outside, base)
            with self.assertRaisesRegex(DiffScanError, "not inside a git repository"):
                git_dir(outside)
        with self.assertRaisesRegex(DiffScanError, "not a directory"):
            snapshot(self.repo.root / "missing", base)
        with self.assertRaisesRegex(DiffScanError, "does not name a commit"):
            snapshot(self.repo.root, "0" * 40)
        with self.assertRaisesRegex(DiffScanError, "invalid git revision"):
            resolve_base_sha(self.repo.root, "--output=/tmp/x")

    def test_resolve_base_sha_defaults_to_head(self) -> None:
        head = self.repo.commit_empty()
        self.assertEqual(resolve_base_sha(self.repo.root, None), head)
        self.assertEqual(resolve_base_sha(self.repo.root, "HEAD"), head)
        self.assertEqual(resolve_base_sha(self.repo.root, head[:10]), head)


PY_SOURCE = """\
import functools


class Outer:
    value = 1

    def method(self):
        a = 1
        return a


@functools.cache
def decorated():
    return 2


TOP = 3
"""

JS_SOURCE = """\
function loadUser(id) {
  const url = "/users/{id}";
  return fetch(url);
}

const saveUser = async (user) => {
  await db.put(user);
};
"""

GO_SOURCE = """\
package main

func (s *Server) Handle(w Writer, r *Request) {
	if r == nil {
		return
	}
	s.count++
}
"""


class ScopeTests(RepoTestCase):
    def test_python_scopes_are_exact_with_qualified_names(self) -> None:
        self.repo.write("mod.py", PY_SOURCE)

        method = WorktreeIndex(self.repo.root).scope("mod.py", 8)
        decorated = WorktreeIndex(self.repo.root).scope("mod.py", 14)
        module_level = WorktreeIndex(self.repo.root).scope("mod.py", 17)

        assert method is not None and decorated is not None and module_level is not None
        self.assertEqual(
            (method.name, method.kind, method.start, method.end), ("Outer.method", "function", 7, 9)
        )
        self.assertTrue(method.text.startswith("    def method(self):"))
        self.assertEqual((decorated.name, decorated.start, decorated.end), ("decorated", 12, 14))
        self.assertEqual(module_level.kind, "block")

    def test_python_syntax_error_falls_back_to_the_heuristic(self) -> None:
        self.repo.write("broken.py", "def ok(x):\n    return x +\n")

        scope = WorktreeIndex(self.repo.root).scope("broken.py", 2)

        assert scope is not None
        self.assertEqual((scope.name, scope.kind, scope.start, scope.end), ("ok", "function", 1, 2))

    def test_js_functions_of_both_syntaxes_are_scopes(self) -> None:
        self.repo.write("user.js", JS_SOURCE)

        function = WorktreeIndex(self.repo.root).scope("user.js", 3)
        arrow = WorktreeIndex(self.repo.root).scope("user.js", 7)

        assert function is not None and arrow is not None
        self.assertEqual((function.name, function.start, function.end), ("loadUser", 1, 4))
        self.assertEqual((arrow.name, arrow.start, arrow.end), ("saveUser", 6, 8))

    def test_go_method_with_receiver(self) -> None:
        self.repo.write("server.go", GO_SOURCE)

        scope = WorktreeIndex(self.repo.root).scope("server.go", 5)

        assert scope is not None
        self.assertEqual((scope.name, scope.kind, scope.start, scope.end), ("Handle", "function", 3, 8))

    def test_long_scope_keeps_its_whole_text(self) -> None:
        body = "".join(f"    v{index} = {index}\n" for index in range(200))
        self.repo.write("big.py", "def big():\n" + body)

        scope = WorktreeIndex(self.repo.root).scope("big.py", 150)

        assert scope is not None
        self.assertEqual((scope.start, scope.end), (1, 201))
        self.assertEqual(len(scope.text.split("\n")), 201)

    def test_unreadable_lines_have_no_scope(self) -> None:
        self.repo.write("blob.bin", b"\x00\x01")
        self.repo.write("small.txt", "one\n")

        self.assertIsNone(WorktreeIndex(self.repo.root).scope("nope.py", 1))
        self.assertIsNone(WorktreeIndex(self.repo.root).scope("blob.bin", 1))
        self.assertIsNone(WorktreeIndex(self.repo.root).scope("small.txt", 0))
        self.assertIsNone(WorktreeIndex(self.repo.root).scope("small.txt", 99))
        with self.assertRaises(DiffScanError):
            WorktreeIndex(self.repo.root).scope("../outside.py", 1)


class SymbolAndReferenceTests(RepoTestCase):
    def test_symbols_cover_every_definition_a_hunk_touches(self) -> None:
        self.repo.write(
            "calc.py",
            "def compute_total(items):\n    return sum(items)\n\n\ndef legacy_rate():\n    return 1\n",
        )
        base = self.repo.commit()
        self.repo.write(
            "calc.py",
            "def compute_total(items):\n    return sum(items) * 2\n\n\ndef main():\n    return 1\n",
        )

        symbols = [
            WorktreeIndex(self.repo.root).symbols(hunk) for hunk in snapshot(self.repo.root, base).hunks
        ]

        self.assertEqual(symbols, [("legacy_rate", "compute_total")])

    def test_references_match_whole_words_in_every_work_tree_file(self) -> None:
        self.repo.write("a.py", "compute_total(1)\ncompute_totals(2)\n")
        self.repo.commit()
        self.repo.write("b.py", "x = compute_total\n")

        references = WorktreeIndex(self.repo.root).references("compute_total")

        self.assertEqual(
            [(ref.path, ref.line, ref.text) for ref in references],
            [("a.py", 1, "compute_total(1)"), ("b.py", 1, "x = compute_total")],
        )
        self.assertEqual(
            WorktreeIndex(self.repo.root).references("compute_total", limit=1)[0].path, "a.py"
        )
        self.assertEqual(WorktreeIndex(self.repo.root).references("nothing_here"), ())
        with self.assertRaises(DiffScanError):
            WorktreeIndex(self.repo.root).references("")


SIGNAL_EXAMPLES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "process": (
        (
            "subprocess.run(cmd)",
            "os.system('ls')",
            "run(cmd, shell=True)",
            "require('child_process')",
            'exec.Command("ls")',
            "Runtime.getRuntime().exec(cmd)",
            "Process.Start(info)",
        ),
        ("process_items(data)", "self.subprocess_count = 1"),
    ),
    "filesystem": (
        (
            "with open(path) as fh:",
            "Path(name)",
            "os.path.join(a, b)",
            "shutil.copy(a, b)",
            "os.remove(p)",
            "target.unlink()",
            "rmtree(d)",
            "os.replace(a, b)",
            "fs.readFileSync(p)",
            "File.ReadAllText(p)",
        ),
        ("reopen_ticket(t)", "x = profile.name"),
    ),
    "network": (
        (
            "requests.get(url)",
            "httpx.post(u)",
            "urllib.request.urlopen(u)",
            "fetch(url)",
            "axios.get(u)",
            "socket.create_connection(a)",
            "http.Client{}",
            "new HttpClient()",
        ),
        ("prefetched = True",),
    ),
    "sql": (
        (
            "SELECT id FROM users",
            "insert into t values (1)",
            "UPDATE users SET a = 1",
            "delete from t",
            "cur.execute(q)",
            "cursor.fetchall()",
            "db.query(sql)",
        ),
        ("selected = items", "Cursor IDE integration"),
    ),
    "deserialization": (
        (
            "pickle.loads(data)",
            "yaml.load(stream)",
            "marshal.loads(b)",
            "eval(expr)",
            "exec(code)",
            "JSON.parse(body)",
            "new ObjectInputStream(in)",
        ),
        ("yaml.safe_load(stream)", "ast.literal_eval(s)", "regex.exec(text)"),
    ),
    "concurrency": (
        (
            "import threading",
            "Thread(target=f)",
            "Lock()",
            "asyncio.run(main())",
            "await job",
            "async def handler():",
            "new Promise(r)",
            "go func() {",
            "sync.Mutex",
            "multiprocessing.Pool()",
            "concurrent.futures.wait(fs)",
            "std::atomic<int> n;",
        ),
        ("save_state_atomic(path)", "awaiting_input = True"),
    ),
    "time": (
        (
            "datetime.now()",
            "time.time()",
            "time.sleep(1)",
            "timezone.utc",
            "dt.tzinfo",
            "new Date()",
            "Date.now()",
            "datetime.utcnow()",
        ),
        ("runtime = 3", "timeout_s = 2"),
    ),
    "regex": (
        (
            "re.compile(p)",
            "re.match(p, s)",
            "re.search(p, s)",
            "re.sub(a, b, s)",
            "re.fullmatch(p, s)",
            "new RegExp(p)",
            "regexp.MustCompile(p)",
        ),
        ("more.compile(x)", "pre.match"),
    ),
    "auth": (
        (
            "password = x",
            "PASSWD",
            "token = t",
            "GITHUB_TOKEN",
            "client_secret",
            "api_key",
            "apiKey",
            "auth()",
            "checkPermission(u)",
            "jwt.decode(t)",
            "set_cookie(c)",
            "request.session",
            "csrf_protect",
            "oauth_flow",
            "getAuthToken()",
        ),
        ("author = 'me'", "Co-authored-by: Dev", "tokenizer.split(s)", "secretary"),
    ),
    "errors": (
        (
            "try:",
            "except ValueError:",
            "finally:",
            "raise KeyError(k)",
            "} catch (e) {",
            "catch {",
            "throw new Error(m)",
            "panic(err)",
            "recover()",
            "Result<u8, E>",
            "x.unwrap()",
        ),
        ("retry_count = 3", "exceptional = 1"),
    ),
    "datamodel": (
        (
            "class User:",
            "@dataclass(frozen=True)",
            "class P(TypedDict):",
            "NamedTuple",
            "BaseModel",
            "Protocol",
            "class Color(Enum):",
            "interface Props {",
            "type Id = string",
            "struct Point {",
            "enum Mode {",
            "record Pair(int a)",
        ),
        ("classify(x)", "structure = 1"),
    ),
    "platform": (
        (
            "sys.platform == 'win32'",
            "os.name == 'nt'",
            "os.sep",
            "platform.system()",
            "'\\r\\n'",
            "open(p, encoding='utf-8')",
            "s.encode()",
            "b.decode()",
            "locale.getlocale()",
            "powershell -Command",
            "cmd.exe /c",
            "%APPDATA%",
            "$HOME/.config",
        ),
        ("homepage = url", "platformer = game"),
    ),
}

PATH_SIGNAL_EXAMPLES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "config": (
        (
            "pyproject.toml",
            "app/settings.yaml",
            "x.yml",
            "package.json",
            "setup.cfg",
            ".env",
            ".env.local",
            "Dockerfile",
            "Makefile",
            "poetry.lock",
            ".github/workflows/ci.yml",
            "requirements-dev.txt",
            "conf.ini",
        ),
        ("app.py", "README.md"),
    ),
    "tests": (
        (
            "tests/helpers.py",
            "spec/models/user_spec.rb",
            "test_app.py",
            "app_test.py",
            "src/x.test.ts",
            "src/x.spec.ts",
            "pkg/x_test.go",
        ),
        ("contest.py", "src/attestation.py"),
    ),
    "docs": (
        ("README.md", "guide.rst", "notes.txt", "docs/manual.adoc", "CHANGES.MD", "src/notes.Txt"),
        (
            "requirements.txt",
            "CMakeLists.txt",
            "app.py",
            "docs/config.yaml",
            "docs/conf.py",
            "scripts/docs/deploy.sh",
            "src/api/docs/views.py",
        ),
    ),
}


class SignalTests(unittest.TestCase):
    def test_content_signals_fire_only_on_their_examples(self) -> None:
        for signal, (positives, negatives) in SIGNAL_EXAMPLES.items():
            for text in positives:
                with self.subTest(signal=signal, text=text):
                    self.assertIn(signal, detect_signals([change_hunk("src/mod.py", added=[text])]))
            for text in negatives:
                with self.subTest(signal=signal, text=text):
                    self.assertNotIn(signal, detect_signals([change_hunk("src/mod.py", added=[text])]))

    def test_path_signals(self) -> None:
        for signal, (positives, negatives) in PATH_SIGNAL_EXAMPLES.items():
            for path in positives:
                with self.subTest(signal=signal, path=path):
                    self.assertIn(signal, detect_signals([change_hunk(path, added=["x"])]))
            for path in negatives:
                with self.subTest(signal=signal, path=path):
                    self.assertNotIn(signal, detect_signals([change_hunk(path, added=["x"])]))

    def test_deletion_requires_a_non_blank_removed_line_or_a_deleted_file(self) -> None:
        self.assertIn("deletion", detect_signals([change_hunk("a.py", removed=["x = 1"])]))
        self.assertNotIn("deletion", detect_signals([change_hunk("a.py", removed=["   "], added=["y"])]))
        self.assertIn("deletion", detect_signals([change_hunk("a.bin", status="deleted")]))

    def test_removed_lines_trigger_signals_in_hunk_order(self) -> None:
        hunks = [
            change_hunk("a.py", removed=["subprocess.run(x)"], hunk_id="H1"),
            change_hunk("b.py", added=["value = 1"], hunk_id="H2"),
            change_hunk("c.py", added=["os.system(cmd)"], hunk_id="H3"),
        ]

        signals = detect_signals(hunks)

        self.assertEqual(signals["process"], ("H1", "H3"))
        self.assertEqual(list(signals), ["process", "deletion"])

    def test_docs_content_does_not_trigger_code_signals(self) -> None:
        signals = detect_signals([change_hunk("README.md", added=["select the token from the session"])])
        self.assertEqual(signals, {"docs": ("H1",)})


class SnapshotStabilityTests(RepoTestCase):
    """The fingerprint and digests describe content, so staging or committing must not move them."""

    def view(self, base: str) -> tuple[str, list[tuple[str, str, str]]]:
        snap = snapshot(self.repo.root, base)
        return snap.fingerprint, [(hunk.path, hunk.status, hunk.digest) for hunk in snap.hunks]

    def test_committing_new_files_keeps_the_fingerprint(self) -> None:
        self.repo.write("keep.txt", "keep\n")
        base = self.repo.commit()
        self.repo.write("src/new.py", "def f():\n    return 1\n")
        self.repo.write("tail.txt", "no newline")
        self.repo.write("empty.txt", "")
        self.repo.write("blob.bin", b"\x00\x01binary")
        if os.name != "nt":  # Windows forbids quotes in names and symlinks need privileges
            self.repo.write('we"ird name.txt', "quoted\n")
            self.repo.write("run.sh", "echo hi\n").chmod(0o755)
            (self.repo.root / "link").symlink_to("src/new.py")

        untracked = self.view(base)
        self.repo.git("add", "-A")
        staged = self.view(base)
        self.repo.git("commit", "-q", "--no-verify", "-m", "add files")
        committed = self.view(base)

        self.assertEqual(len(untracked[1]), 7 if os.name != "nt" else 4)
        self.assertEqual(staged, untracked)
        self.assertEqual(committed, untracked)

    def test_crlf_file_keeps_its_digests_under_autocrlf(self) -> None:
        self.repo.git("config", "core.autocrlf", "true")
        base = self.repo.commit_empty()
        self.repo.write("win.txt", b"a\r\nb\r\n")

        untracked = snapshot(self.repo.root, base)
        self.repo.git("add", "-A")
        staged = snapshot(self.repo.root, base)

        self.assertEqual(untracked.hunks[0].lines, ("+a\r", "+b\r"))
        self.assertEqual(
            (staged.fingerprint, [hunk.digest for hunk in staged.hunks]),
            (untracked.fingerprint, [hunk.digest for hunk in untracked.hunks]),
        )

    def test_unindexed_file_on_disk_is_diffed_against_base(self) -> None:
        self.repo.write("keep.py", numbered_lines(12, "value ="))
        base = self.repo.commit()
        self.repo.git("rm", "-q", "--cached", "keep.py")

        self.assertTrue(snapshot(self.repo.root, base).is_empty)

        self.repo.write("keep.py", numbered_lines(12, "value =").replace("= 6\n", "= six\n"))
        snap = snapshot(self.repo.root, base)

        self.assertEqual(snap.files, ("keep.py",))
        (hunk,) = snap.hunks
        self.assertEqual((hunk.status, hunk.added_lines), ("modified", ((6, "value = six"),)))
        self.assertEqual(hunk.removed_lines, ((6, "value = 6"),))
        self.assertEqual((snap.added, snap.deleted), (1, 1))

    def test_unborn_head_compares_against_the_empty_tree(self) -> None:
        self.repo.write("staged.py", "x = 1\n")
        self.repo.git("add", "staged.py")
        self.repo.write("loose.txt", "y\n")

        base = resolve_base_sha(self.repo.root, None)
        snap = snapshot(self.repo.root, base)

        self.assertEqual(base, "4b825dc642cb6eb9a060e54bf8d69288fbee4904")  # git's empty tree
        self.assertEqual(snap.files, ("loose.txt", "staged.py"))
        self.assertEqual(snap.hunks[1].added_lines, ((1, "x = 1"),))
        with self.assertRaisesRegex(DiffScanError, "does not name a commit"):
            resolve_base_sha(self.repo.root, "main")


class DigestTests(RepoTestCase):
    def test_identical_edits_in_different_functions_get_different_digests(self) -> None:
        body = "".join(f"    step_{index}()\n" for index in range(8))
        tail = "    # one\n    # two\n    # three\n"
        source = f"def alpha():\n{body}    return None\n{tail}def beta():\n{body}    return None\n{tail}"
        self.repo.write("svc.py", source)
        base = self.repo.commit()
        self.repo.write("svc.py", source.replace("return None", "return 0"))

        first, second = snapshot(self.repo.root, base).hunks

        self.assertEqual(first.lines, second.lines)
        self.assertEqual((first.header, second.header), ("def alpha():", "def beta():"))
        self.assertNotEqual(first.digest.split("#")[0], second.digest.split("#")[0])

    def test_an_edit_moved_elsewhere_in_the_file_gets_a_new_digest(self) -> None:
        source = (
            "def helper():\n    a = 1\n    b = 2\n    return a + b\n\n\n"
            "def critical():\n    token = load()\n    check(token)\n    return token\n"
        )
        self.repo.write("auth.py", source)
        base = self.repo.commit()
        self.repo.write("auth.py", source.replace("    b = 2\n", "    b = 2\n    skip = True\n"))
        in_helper = snapshot(self.repo.root, base).hunks

        self.repo.write(
            "auth.py", source.replace("    check(token)\n", "    check(token)\n    skip = True\n")
        )
        in_critical = snapshot(self.repo.root, base).hunks

        self.assertEqual(len(in_helper), 1)
        self.assertEqual(len(in_critical), 1)
        self.assertEqual(in_helper[0].added_lines[0][1], in_critical[0].added_lines[0][1])
        self.assertNotEqual(in_helper[0].digest, in_critical[0].digest)

    @unittest.skipIf(os.name == "nt", "the executable bit is not tracked on Windows checkouts")
    def test_mode_change_next_to_content_edits_is_digested(self) -> None:
        script = self.repo.write("run.sh", "echo one\n")
        base = self.repo.commit()
        self.repo.write("run.sh", "echo two\n")
        content_only = snapshot(self.repo.root, base).hunks[0]
        script.chmod(0o755)

        (hunk,) = snapshot(self.repo.root, base).hunks

        self.assertEqual(hunk.lines, ("~ mode 100644 -> 100755", "-echo one", "+echo two"))
        self.assertEqual(hunk.added_lines, ((1, "echo two"),))
        self.assertNotEqual(hunk.digest, content_only.digest)

    def test_end_of_file_newline_changes_are_digested(self) -> None:
        self.repo.write("eof.txt", "last")
        base = self.repo.commit()
        self.repo.write("eof.txt", "last\n")
        adds_newline = snapshot(self.repo.root, base).hunks[0]
        self.repo.git("commit", "-q", "-a", "--no-verify", "-m", "newline")
        self.repo.write("eof.txt", "last")
        drops_newline = snapshot(self.repo.root, self.repo.git("rev-parse", "HEAD").strip()).hunks[0]

        self.assertEqual(adds_newline.lines, ("-last", "\\ No newline at end of file", "+last"))
        self.assertEqual(drops_newline.lines, ("-last", "+last", "\\ No newline at end of file"))
        self.assertEqual(adds_newline.added_lines, ((1, "last"),))
        self.assertNotEqual(adds_newline.digest, drops_newline.digest)


class HunkScopeTests(RepoTestCase):
    SOURCE = "def foo():\n    a = 1\n    return a\ndef bar():\n    x = 1\n    y = 2\n    return x + y\n"

    def scope_name(self, hunk: Hunk) -> str | None:
        scope = WorktreeIndex(self.repo.root).hunk_scope(hunk)
        return None if scope is None else scope.name

    def test_pure_deletion_is_scoped_to_the_function_it_was_made_in(self) -> None:
        self.repo.write("mod.py", self.SOURCE)
        base = self.repo.commit()
        self.repo.write("mod.py", self.SOURCE.replace("    x = 1\n", ""))

        (hunk,) = snapshot(self.repo.root, base).hunks

        self.assertEqual(hunk.new_start, 2)  # the leading context starts inside foo
        self.assertEqual(self.scope_name(hunk), "bar")

    def test_changed_definition_line_is_scoped_to_its_function(self) -> None:
        self.repo.write("mod.py", self.SOURCE)
        base = self.repo.commit()
        self.repo.write("mod.py", self.SOURCE.replace("def bar():", "def bar(c):"))

        (hunk,) = snapshot(self.repo.root, base).hunks

        self.assertEqual(self.scope_name(hunk), "bar")

    def test_deleting_the_last_line_of_a_function_keeps_its_scope(self) -> None:
        self.repo.write("mod.py", self.SOURCE)
        base = self.repo.commit()
        self.repo.write("mod.py", self.SOURCE.replace("    return a\n", ""))

        (hunk,) = snapshot(self.repo.root, base).hunks

        self.assertEqual(self.scope_name(hunk), "foo")

    def test_deletion_at_the_end_of_the_file_uses_the_line_above(self) -> None:
        self.repo.write("mod.py", self.SOURCE)
        base = self.repo.commit()
        self.repo.write("mod.py", self.SOURCE.replace("    return x + y\n", ""))

        (hunk,) = snapshot(self.repo.root, base).hunks

        self.assertEqual(self.scope_name(hunk), "bar")

    def test_hunks_without_content_have_no_scope(self) -> None:
        self.repo.write("mod.py", self.SOURCE)
        base = self.repo.commit()
        self.repo.git("mv", "mod.py", "moved.py")

        (hunk,) = snapshot(self.repo.root, base).hunks

        self.assertEqual(hunk.lines, ("~ renamed from mod.py",))
        self.assertIsNone(self.scope_name(hunk))


JS_CLASS_SOURCE = """\
class Api {
  constructor() {
    this.x = 1;
  }

  async handle(req) {
    const a = 1;
    return a;
  }
}
"""

JS_CALL_SOURCE = """\
function report(items) {
  logger.info(
    "count",
    items.length,
  );
  return items;
}
"""

RUST_SOURCE = """\
pub fn parse(input: &'static str) -> usize {
    let brace = '{';
    input.len()
}

fn other() {
    1
}
"""


class HeuristicScopeTests(RepoTestCase):
    def scope(self, path: str, content: str, line: int) -> tuple[str, int, int]:
        self.repo.write(path, content)
        scope = WorktreeIndex(self.repo.root).scope(path, line)
        assert scope is not None
        return scope.name, scope.start, scope.end

    def test_an_edited_method_signature_is_its_own_scope(self) -> None:
        self.assertEqual(self.scope("api.js", JS_CLASS_SOURCE, 6), ("handle", 6, 9))

    def test_a_multi_line_call_is_not_a_definition(self) -> None:
        self.assertEqual(self.scope("report.js", JS_CALL_SOURCE, 4), ("report", 1, 7))

    def test_rust_lifetimes_do_not_open_a_string(self) -> None:
        self.assertEqual(self.scope("lib.rs", RUST_SOURCE, 3), ("parse", 1, 4))

    @unittest.skipIf(os.name == "nt", "symlinks need extra privileges on Windows")
    def test_symlinks_out_of_the_work_tree_are_not_read(self) -> None:
        with tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / "secret.py"
            secret.write_text("def secret_helper():\n    return 'password'\n", encoding="utf-8")
            (self.repo.root / "link.py").symlink_to(secret)
            self.repo.write("inside.py", "x = 1\n")
            (self.repo.root / "inner.py").symlink_to(self.repo.root / "inside.py")

            self.assertIsNone(WorktreeIndex(self.repo.root).scope("link.py", 1))
            self.assertEqual(WorktreeIndex(self.repo.root).references("secret_helper"), ())
            self.assertIsNotNone(WorktreeIndex(self.repo.root).scope("inner.py", 1))

    def test_python_too_deeply_nested_to_parse_falls_back_to_the_heuristic(self) -> None:
        self.repo.write("deep.py", "total = " + " + ".join(["1"] * 100_000) + "\n")

        scope = WorktreeIndex(self.repo.root).scope("deep.py", 1)

        assert scope is not None
        self.assertEqual(scope.kind, "block")


class SymbolPrecisionTests(RepoTestCase):
    def symbols_after(self, path: str, before: str, after: str) -> tuple[str, ...]:
        self.repo.write(path, before)
        base = self.repo.commit()
        self.repo.write(path, after)
        (hunk,) = snapshot(self.repo.root, base).hunks
        return WorktreeIndex(self.repo.root).symbols(hunk)

    def test_python_prose_or_calls_yield_no_symbols(self) -> None:
        after = (
            "def keep():\n"
            '    """type annotations for this module\n\n'
            "    record the result in the ledger\n"
            "    def fake_helper(x):\n"
            '    """\n'
            "    # function returns None\n"
            "    if type(val) not in allowed:\n"
            "        return 2\n"
            "    return 1\n"
        )

        symbols = self.symbols_after("keep.py", "def keep():\n    return 1\n", after)

        self.assertEqual(symbols, ("keep",))

    def test_python_definitions_come_from_the_syntax_tree(self) -> None:
        after = "def keep():\n    return 1\n\n\nclass Ledger_Entry:\n    async def settle_up(self):\n        pass\n"

        symbols = self.symbols_after("keep.py", "def keep():\n    return 1\n", after)

        self.assertEqual(symbols, ("Ledger_Entry", "settle_up"))

    def test_other_languages_need_definition_syntax(self) -> None:
        after = (
            "// class Widget was removed here\n"
            "const text = `\n"
            "  type annotations for this module\n"
            "  record the result in the ledger\n"
            "`;\n"
            "export function loadUser(id) {\n"
            "  return id;\n"
            "}\n"
            "type Props = { id: string };\n"
        )

        symbols = self.symbols_after("user.ts", "const a = 1;\n", after)

        self.assertIn("loadUser", symbols)
        self.assertIn("Props", symbols)
        for prose in ("Widget", "annotations", "the", "text"):
            self.assertNotIn(prose, symbols)

    def test_docs_files_define_no_symbols(self) -> None:
        after = "# Guide\n\nclass Widget:\n    def render_all(self): ...\n"

        self.assertEqual(self.symbols_after("guide.md", "# Guide\n", after), ())


class ReferenceSearchTests(RepoTestCase):
    def test_generated_or_ignored_untracked_files_are_not_searched(self) -> None:
        self.repo.write("a.py", "compute_total(1)\n")
        self.repo.commit()
        self.repo.write("b.py", "x = compute_total\n")
        self.repo.write("web/node_modules/lib/index.js", "compute_total();\n")
        self.repo.write(".tox/py312/lib.py", "compute_total()\n")
        self.repo.write("build/out.py", "compute_total()\n")
        self.repo.write(".craftsmanignore", "build/\n")

        references = WorktreeIndex(self.repo.root).references("compute_total")

        self.assertEqual([ref.path for ref in references], ["a.py", "b.py"])

    def test_references_rank_other_files_first_with_a_cap_per_file(self) -> None:
        self.repo.write("defs.py", "".join(f"compute_total({index})\n" for index in range(12)))
        self.repo.write("z_use.py", "y = compute_total(5)\n")
        self.repo.commit()

        ranked = WorktreeIndex(self.repo.root).references(
            "compute_total", limit=7, defined_in={"defs.py"}
        )
        alphabetical = WorktreeIndex(self.repo.root).references("compute_total", limit=7)

        per_file = diffscan.REFERENCES_PER_FILE
        self.assertEqual(
            [(ref.path, ref.line) for ref in ranked],
            [("z_use.py", 1), *(("defs.py", line) for line in range(1, per_file + 1))],
        )
        self.assertEqual([ref.path for ref in alphabetical], ["defs.py"] * per_file + ["z_use.py"])

    def test_one_index_reads_each_file_once(self) -> None:
        self.repo.write("calc.py", "def compute_total(items):\n    return sum(items)\n")
        base = self.repo.commit()
        self.repo.write("calc.py", "def compute_total(items):\n    return sum(items) * 2\n")
        (hunk,) = snapshot(self.repo.root, base).hunks
        index = WorktreeIndex(self.repo.root)
        before = index.hunk_scope(hunk)

        self.repo.write("calc.py", "x = 1\n\n\ndef compute_total(items):\n    return 0\n")

        self.assertEqual(index.hunk_scope(hunk), before)


class DocsClassificationTests(RepoTestCase):
    def test_code_renamed_to_a_docs_name_keeps_code_signals(self) -> None:
        self.repo.write("src/auth.py", "token = load()\n" + numbered_lines(8, "x ="))
        base = self.repo.commit()
        self.repo.git("mv", "src/auth.py", "notes.txt")
        self.repo.write("notes.txt", "token = reload()\n" + numbered_lines(8, "x ="))

        snap = snapshot(self.repo.root, base)

        self.assertEqual(snap.hunks[0].old_path, "src/auth.py")
        self.assertFalse(snap.docs_only)
        signals = detect_signals(snap.hunks)
        self.assertIn("auth", signals)
        self.assertNotIn("docs", signals)

    def test_docs_renamed_to_docs_stays_docs(self) -> None:
        self.repo.write("guide.md", numbered_lines(8))
        base = self.repo.commit()
        (self.repo.root / "docs").mkdir()
        self.repo.git("mv", "guide.md", "docs/GUIDE.MD")

        snap = snapshot(self.repo.root, base)

        self.assertTrue(snap.docs_only)
        self.assertEqual(detect_signals(snap.hunks), {"docs": ("H1",)})


if __name__ == "__main__":
    unittest.main()
