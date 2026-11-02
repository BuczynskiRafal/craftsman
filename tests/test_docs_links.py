"""Tests verifying all relative file links, section anchors, and images across documentation."""

import re
import unittest
import urllib.parse
from pathlib import Path

# Match Markdown links and images: [text](target) or ![alt](target)
# Exclude reference links or HTML tags
MD_LINK_RE = re.compile(r"!?\[(?:[^\]\\]|\\.)*\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
RAW_GITHUB_PREFIX = "https://raw.githubusercontent.com/BuczynskiRafal/craftsman/main/"
BLOB_GITHUB_PREFIX = "https://github.com/BuczynskiRafal/craftsman/blob/main/"


def github_slug(heading_text: str) -> str:
    """Compute GitHub-compatible heading slug for Markdown anchor matching."""
    text = heading_text.strip()
    # Strip inline markdown links [foo](bar) -> foo
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Strip backticks, formatting
    text = re.sub(r"[`*_~]", "", text)
    # Lowercase
    text = text.lower()
    # Remove punctuation (keep alphanumeric, whitespace, hyphens)
    text = re.sub(r"[^\w\s-]", "", text)
    # Replace spaces with hyphens
    text = re.sub(r"[\s]+", "-", text)
    return text.strip("-")


def extract_headings(content: str) -> set[str]:
    """Extract all heading slugs from Markdown content."""
    slugs: set[str] = set()
    slug_counts: dict[str, int] = {}
    for match in HEADING_RE.finditer(content):
        raw_heading = match.group(2)
        slug = github_slug(raw_heading)
        if slug in slug_counts:
            count = slug_counts[slug] + 1
            slug_counts[slug] = count
            slugs.add(f"{slug}-{count}")
        else:
            slug_counts[slug] = 0
            slugs.add(slug)
    return slugs


class TestDocsLinks(unittest.TestCase):
    """Verify link integrity across README.md and all docs/*.md files."""

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parent.parent
        self.doc_files = [self.repo_root / "README.md"] + sorted(
            (self.repo_root / "docs").rglob("*.md")
        )

    def test_all_links_and_images_valid(self) -> None:
        broken_links: list[str] = []

        for doc_path in self.doc_files:
            if not doc_path.exists():
                broken_links.append(f"Referenced document does not exist: {doc_path}")
                continue

            content = doc_path.read_text(encoding="utf-8")
            headings = extract_headings(content)

            for match in MD_LINK_RE.finditer(content):
                target = match.group(1).strip()
                # Strip title if present: e.g. (foo.md "title")
                target = target.split()[0]
                # Strip angle brackets if present: e.g. (<foo.md>)
                target = target.strip("<>")

                # 1. Absolute GitHub URLs into this repository (raw assets, or files linked
                #    absolutely so they also resolve on PyPI)
                repo_prefix = next(
                    (p for p in (RAW_GITHUB_PREFIX, BLOB_GITHUB_PREFIX) if target.startswith(p)),
                    None,
                )
                if repo_prefix is not None:
                    rel_repo_path = target[len(repo_prefix) :].split("#", 1)[0]
                    local_target = self.repo_root / rel_repo_path
                    if not local_target.exists():
                        broken_links.append(
                            f"{doc_path.relative_to(self.repo_root)}: "
                            f"GitHub repository file not found locally: {rel_repo_path}"
                        )
                    continue

                # 2. External HTTP/HTTPS web links or mailto
                if (
                    target.startswith("http://")
                    or target.startswith("https://")
                    or target.startswith("mailto:")
                ):
                    continue

                # 3. Same-document section anchors (#heading)
                if target.startswith("#"):
                    anchor = target[1:]
                    if anchor not in headings:
                        broken_links.append(
                            f"{doc_path.relative_to(self.repo_root)}: "
                            f"Anchor '#{anchor}' not found in document (available: {sorted(headings)})"
                        )
                    continue

                # 4. Relative file paths (with optional anchor)
                url_parts = urllib.parse.urlsplit(target)
                file_part = url_parts.path
                anchor_part = url_parts.fragment

                target_file = (doc_path.parent / file_part).resolve()

                # Verify target file exists
                if not target_file.exists():
                    broken_links.append(
                        f"{doc_path.relative_to(self.repo_root)}: "
                        f"Target path does not exist: {target} (resolved to {target_file})"
                    )
                    continue

                # If target is a Markdown file and has an anchor, verify anchor in target
                if anchor_part and target_file.suffix == ".md":
                    target_content = target_file.read_text(encoding="utf-8")
                    target_headings = extract_headings(target_content)
                    if anchor_part not in target_headings:
                        broken_links.append(
                            f"{doc_path.relative_to(self.repo_root)}: "
                            f"Anchor '#{anchor_part}' not found in target file {target_file.name} "
                            f"(available: {sorted(target_headings)})"
                        )

        self.assertEqual(
            broken_links,
            [],
            "Found broken documentation links/images:\n" + "\n".join(broken_links),
        )


if __name__ == "__main__":
    unittest.main()
