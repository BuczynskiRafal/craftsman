"""Tests for content risk-signal regex patterns in `craftsman.diffscan_signals`."""

from __future__ import annotations

import unittest

from craftsman.diffscan import SIGNALS
from craftsman.diffscan_signals import CONTENT_SIGNAL_PATTERNS

EXPECTED_CATEGORIES = frozenset(SIGNALS) - {"config", "tests", "deletion", "docs"}

POSITIVE_AND_NEGATIVE_CASES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "process": (
        (
            'subprocess.run(["git", "status"])',
            'os.system("uname -a")',
            'cmd := exec.CommandContext(ctx, "ls")',
            '"git rev-parse HEAD".execute()',
            'sh "make test"',
        ),
        (
            "process_count = 4",
            'logger.system("not a bare libc call")',
            "shell_enabled = False",
        ),
    ),
    "filesystem": (
        (
            'with open("config.toml", "rb") as handle:',
            'root = Path("/tmp/work")',
            "shutil.rmtree(staging_dir)",
            "payload = target.read_text()",
            "std::filesystem::path root;",
        ),
        (
            'conn.reopen("primary")',
            "file_descriptor_limit = 1024",
        ),
    ),
    "network": (
        (
            "resp = requests.get(endpoint, timeout=5)",
            'const res = await fetch("/api/items")',
            'req, err := http.NewRequest("POST", url, body)',
            "boost::asio::io_context io;",
        ),
        (
            "prefetch_batch_size = 16",
            'network_label = "internal"',
        ),
    ),
    "sql": (
        (
            'query = "SELECT id, email FROM accounts WHERE active = 1"',
            'stmt = "INSERT INTO events (kind) VALUES (?)"',
            'cursor.execute("DELETE FROM sessions WHERE expired = 1")',
            "rows = db.query(sql)",
        ),
        (
            "task.execute()",
            'note = "selected items from_cache for display"',
        ),
    ),
    "deserialization": (
        (
            "obj = pickle.loads(blob)",
            "data = yaml.load(stream)",
            "parsed = JSON.parse(rawBody)",
            "result = eval(expression)",
            "new ObjectInputStream(input)",
        ),
        (
            "data = yaml.safe_load(stream)",
            "model.eval()",
        ),
    ),
    "concurrency": (
        (
            "guard = threading.RLock()",
            "async def fetch_all(items):",
            "value = await future",
            "var wg sync.WaitGroup",
            "std::scoped_lock lock(mu);",
        ),
        (
            "awaiting_review = True",
            "max_threads_hint = 8",
        ),
    ),
    "time": (
        (
            "started = time.monotonic()",
            "stamp = datetime.now(timezone.utc)",
            "const ms = Date.now()",
            "std::chrono::milliseconds(250)",
            "localtime_r(&now, & broken_down);",
        ),
        (
            "runtime_budget = 60",
            "clock.localtime(&now)",
        ),
    ),
    "regex": (
        (
            'PATTERN = re.compile(r"^[a-z]+$")',
            'const rx = new RegExp("^v\\\\d+")',
            're := regexp.MustCompile(`[0-9]+`)',
            "if ($line =~ /^WARN/)",
        ),
        (
            "compiler.compile(source_ast)",
            "if count == 0:",
        ),
    ),
    "auth": (
        (
            "hashed_password = hash_secret(raw)",
            "headers = {'Authorization': bearer_token}",
            "const nextToken = session.accessToken",
            "rotate_api_keys(account)",
        ),
        (
            'author_name = "Ada Lovelace"',
            "lexeme_count = lexer_tokenize(source)",
        ),
    ),
    "errors": (
        (
            "    try:\n        parse(line)",
            'raise ValueError("invalid header")',
            "catch (err) {",
            "if err != nil {",
            "let cfg = loaded.unwrap();",
        ),
        (
            "retry_limit = 3",
            'last_error_code = "none"',
        ),
    ),
    "datamodel": (
        (
            "@dataclass(frozen=True)\nclass Finding:",
            "export type RoundId = string",
            "type Config struct {",
            "public record Coordinate(int x, int y) {}",
        ),
        (
            "# record the result before returning",
            "classify_entry(item)",
        ),
    ),
    "platform": (
        (
            'if sys.platform == "win32":',
            "parts = raw.split(os.pathsep)",
            'encoded = text.encode("utf-8")',
            'if runtime.GOOS == "windows" {',
            "const isWin = process.platform === 'win32'",
        ),
        (
            "platform_version = 4",
            "encoder_hidden_size = 256",
        ),
    ),
}


class DiffscanSignalsTests(unittest.TestCase):
    def test_catalog_covers_every_content_signal_category(self) -> None:
        self.assertEqual(set(CONTENT_SIGNAL_PATTERNS), EXPECTED_CATEGORIES)
        self.assertEqual(set(POSITIVE_AND_NEGATIVE_CASES), EXPECTED_CATEGORIES)

    def test_each_category_matches_positives_while_rejecting_negatives(self) -> None:
        for category, (positives, negatives) in POSITIVE_AND_NEGATIVE_CASES.items():
            pattern = CONTENT_SIGNAL_PATTERNS[category]
            for snippet in positives:
                with self.subTest(category=category, kind="positive", snippet=snippet):
                    self.assertIsNotNone(
                        pattern.search(snippet),
                        f"expected {category!r} to match {snippet!r}",
                    )
            for snippet in negatives:
                with self.subTest(category=category, kind="negative", snippet=snippet):
                    self.assertIsNone(
                        pattern.search(snippet),
                        f"expected {category!r} not to match {snippet!r}",
                    )


if __name__ == "__main__":
    unittest.main()
