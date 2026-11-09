# Add `invtool search`

Operators want to find items without scrolling through the whole `list` output.

- `invtool search TERM` prints the items whose name contains TERM. The match is case-insensitive:
  `bolt` finds `Hex Bolt`.
- `--min-qty N` keeps only items with a quantity of at least N.
- `--max-price P` keeps only items priced at P or less.
- Matching items are printed in the same format as `list`. When nothing matches, print nothing
  and exit with status 1 so scripts can branch on it.
