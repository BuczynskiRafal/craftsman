"""Tests for the standard stream setup of every entry point (`craftsman.streams`)."""

from __future__ import annotations

import io
import sys
import unittest
from unittest import mock

from craftsman.streams import configure_std_streams


def pipe(encoding: str, data: bytes = b"") -> io.TextIOWrapper:
    """A text stream over a pipe-like buffer, as Python opens one on Windows (not a console)."""
    return io.TextIOWrapper(io.BytesIO(data), encoding=encoding, errors="strict")


class ConfigureStdStreamsTests(unittest.TestCase):
    def configure(self, platform: str, stdin: io.TextIOWrapper) -> tuple[io.TextIOWrapper, io.TextIOWrapper]:
        stdout, stderr = pipe("cp1252"), pipe("cp1252")
        with (
            mock.patch.object(sys, "platform", platform),
            mock.patch.object(sys, "stdin", stdin),
            mock.patch.object(sys, "stdout", stdout),
            mock.patch.object(sys, "stderr", stderr),
        ):
            configure_std_streams()
        return stdout, stderr

    def test_windows_pipes_exchange_utf8(self) -> None:
        stdin = pipe("cp1252", "/craftsman popraw błąd →".encode())
        stdout, stderr = self.configure("win32", stdin)

        self.assertEqual(stdin.read(), "/craftsman popraw błąd →")
        for stream in (stdout, stderr):
            stream.write("R1 (test) → exit 0 · C:\\Users\\Rafał")
            stream.flush()
            assert isinstance(stream.buffer, io.BytesIO)
            self.assertEqual(stream.buffer.getvalue().decode(), "R1 (test) → exit 0 · C:\\Users\\Rafał")

    def test_elsewhere_the_encoding_stays_and_unencodable_text_is_replaced(self) -> None:
        stdin = pipe("cp1252")
        stdout, _ = self.configure("linux", stdin)

        self.assertEqual((stdin.encoding, stdin.errors), ("cp1252", "strict"))
        stdout.write("a → b")
        stdout.flush()
        assert isinstance(stdout.buffer, io.BytesIO)
        self.assertEqual(stdout.buffer.getvalue(), b"a ? b")

    def test_a_closed_stdin_is_left_alone(self) -> None:
        stdin = pipe("cp1252")
        stdin.close()
        stdout, _ = self.configure("win32", stdin)
        self.assertEqual(stdout.encoding, "utf-8")


if __name__ == "__main__":
    unittest.main()
