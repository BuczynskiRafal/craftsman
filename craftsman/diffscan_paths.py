"""Path classifiers: configuration, tests, documentation, data and agent prompts, by file type or name alone.

A leaf module: it knows paths, not diffs. Callers import these names from here; `craftsman.diffscan` uses
them but does not re-export them.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from pathlib import PurePosixPath

_CONFIG_SUFFIXES = frozenset(
    {".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".env", ".lock", ".gradle", ".cmake"}
)  # fmt: skip
_CONFIG_NAMES = frozenset(
    {"Dockerfile", "Makefile", "package.json", "pyproject.toml", "setup.py", "setup.cfg", ".env",
     "CMakeLists.txt", "Jenkinsfile", "build.gradle.kts", "settings.gradle.kts", "conanfile.txt",
     "conanfile.py"}
)  # fmt: skip
_CONFIG_NAME_GLOBS = (
    "Dockerfile.*", ".env.*", "requirements*.txt", "constraints*.txt", "Jenkinsfile.*", "*.Jenkinsfile",
)  # fmt: skip
_TEST_DIRS = frozenset({"test", "tests", "spec", "specs", "__tests__", "testing"})
_TEST_NAME_GLOBS = (
    "test_*.py", "*_test.py", "*.test.ts", "*.test.tsx", "*.test.js", "*.test.jsx",
    "*.spec.ts", "*.spec.tsx", "*.spec.js", "*.spec.jsx", "*_test.go",
    "*_test.c", "*_test.cc", "*_test.cpp", "*_test.cxx", "*_unittest.cc", "*_unittest.cpp",
    "test_*.c", "test_*.cc", "test_*.cpp", "*Test.java", "*Tests.java", "*Test.kt", "*Test.groovy",
    "*Spec.groovy",
)  # fmt: skip
_DOCS_SUFFIXES = frozenset({".md", ".rst", ".txt", ".adoc"})
# Languages whose risky calls the content patterns above know; changed code in any other language
# is listed by `files_without_signal_patterns` so the reviewer reads it for risks instead.
_PATTERN_LANGUAGE_SUFFIXES = frozenset(
    {".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".java", ".kt", ".cs",
     ".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx", ".groovy", ".gradle"}
)  # fmt: skip
_PATTERN_LANGUAGE_NAME_GLOBS = ("Jenkinsfile", "Jenkinsfile.*", "*.Jenkinsfile")
_DATA_SUFFIXES = frozenset({".json", ".lock", ".toml", ".ini", ".cfg", ".csv", ".tsv", ".svg"})


def name_matches(name: str, globs: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, glob) for glob in globs)


def outside_signal_patterns(path: str) -> bool:
    posix = PurePosixPath(path)
    suffix = posix.suffix.lower()
    if suffix in _DATA_SUFFIXES or name_matches(posix.name, _PATTERN_LANGUAGE_NAME_GLOBS):
        return False
    return suffix not in _PATTERN_LANGUAGE_SUFFIXES


def is_config_path(path: str) -> bool:
    """Configuration by file type or name: manifests, lockfiles, CI workflows, `Dockerfile`, `Makefile`."""
    posix = PurePosixPath(path)
    return (
        posix.suffix.lower() in _CONFIG_SUFFIXES
        or posix.name in _CONFIG_NAMES
        or name_matches(posix.name, _CONFIG_NAME_GLOBS)
        or posix.parts[:2] == (".github", "workflows")
    )



_GENERATED_NAMES = frozenset({"go.sum"})
_GENERATED_SUFFIXES = (".min.js", ".min.css", ".map")


def is_data_path(path: str) -> bool:
    """Data, lockfiles and generated files: changes there are mechanical, not code to read line by line.

    Covers the data suffixes (`.json`, `.lock`, `.svg`, ...), `go.sum` and minified or source-map files.
    """
    posix = PurePosixPath(path)
    name = posix.name.lower()
    return (
        posix.suffix.lower() in _DATA_SUFFIXES or name in _GENERATED_NAMES or name.endswith(_GENERATED_SUFFIXES)
    )


def is_test_path(path: str) -> bool:
    """Test code by location or name, e.g. `tests/x.py`, `test_x.py`, `x.spec.ts`, `x_test.go`."""
    posix = PurePosixPath(path)
    return bool(_TEST_DIRS.intersection(posix.parts[:-1])) or name_matches(posix.name, _TEST_NAME_GLOBS)


def is_docs_path(path: str) -> bool:
    """Prose by file type alone: a `docs/` folder can hold code (e.g. Sphinx's conf.py)."""
    return PurePosixPath(path).suffix.lower() in _DOCS_SUFFIXES and not is_config_path(path)


_AGENT_PROMPT_NAMES = frozenset({"skill.md", "agents.md", "claude.md", "gemini.md"})
_AGENT_PROMPT_DIRS = frozenset({"agents", "rules"})
_AGENT_PROMPT_NAME_GLOBS = ("*.prompt.md", "*.instructions.md")


def is_agent_prompt_path(path: str) -> bool:
    """A prompt file that steers an agent: its prose is behavior, not documentation.

    `SKILL.md`, `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `*.prompt.md`, `copilot-instructions.md`,
    Markdown under a `rules/` or `agents/` folder.
    """
    posix = PurePosixPath(path)
    name = posix.name.lower()
    if posix.suffix.lower() not in _DOCS_SUFFIXES:
        return False
    return (
        name in _AGENT_PROMPT_NAMES
        or name == "copilot-instructions.md"
        or name_matches(name, _AGENT_PROMPT_NAME_GLOBS)
        or bool(_AGENT_PROMPT_DIRS.intersection(part.lower() for part in posix.parts[:-1]))
    )
