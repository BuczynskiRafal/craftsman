# Tags, export and digest for feedbox

Feeds can be tagged, articles can be written out as files, and there is a short overview of what is unread.

- **Authors.** An article has an author, taken from `<author>` or, when that is missing, from
  `<dc:creator>`. It is saved with the library and shown in exported files.
- **Import.** `feedbox import` takes several files. A file that cannot be read is reported and the others are
  still imported.
- **Tags.** `feedbox tag FEED TAG [--remove]` adds or removes a tag of a feed. Tags are lower case, a
  feed has each tag once, and each feed has its own list of tags. They are saved in the library;
  libraries written before this change have no tags and must still load. `feedbox feeds` shows them.
- **Filters.** `list` and `export` take `--tag TAG` (repeatable) and `--since DATE`. `--tag` keeps only
  articles of feeds that have **every** given tag. `--since 2026-03-01` keeps articles published on
  that day or later; undated articles never match. `DATE` must look like `YYYY-MM-DD`.
- **Export.** `feedbox export DEST [--format md|html] [--feed F] [--unread] [--tag T] [--since D]`
  writes one file per selected article. The default format is `md`. A feed's articles go into a folder of
  its own named like the feed's slug (a feed called `Field Notes` is exported under `field-notes/`), as
  `<date>-<title slug>.<ext>`, or `undated-...` when the article has no date. Nothing is ever
  written outside `DEST`, and a file that already exists is never overwritten: a numeric suffix is added.
  `DEST/index.md` lists the exported files; its links use forward slashes so the folder can be moved
  between systems.
- **Digest.** `feedbox digest [--since D] [--per-feed N]` prints, for every feed with unread articles, how
  many are unread and the titles of the N newest (3 by default), then how many more there are.
