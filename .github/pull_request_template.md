## Description
<!-- Provide a clear summary of the changes and motivation. -->

## Related Issues
<!-- Link related issues, e.g. Fixes #123, Closes #456 -->

## Quality Gate Checklist
Before submitting this pull request, please ensure:
- [ ] Runtime code introduces **zero third-party dependencies** (`dependencies = []` in `pyproject.toml`, standard library only).
- [ ] Linter passes with zero warnings: `python3 -m ruff check craftsman scripts tests`
- [ ] Strict type checking passes across all files: `uvx mypy --strict craftsman scripts tests`
- [ ] Full unit and integration test suite passes: `python3 -m unittest discover -s tests`
- [ ] System diagnostics pass on a clean environment: `HOME=$(mktemp -d) python3 -m craftsman.cli doctor`
- [ ] Commits follow **Conventional Commits** in American English.
- [ ] Commits are authored as the contributor's real name and email without AI attribution trailers.
