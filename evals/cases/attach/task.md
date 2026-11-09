# Folders in ticket attachments

Support asked to keep log bundles together, so attachment names may now contain folders.

- `save("OPS-42", "logs/app.txt", data)` creates `logs/` inside the ticket's directory. Names use
  `/` as the separator on every platform, including in what `list` returns.
- `list(ticket)` returns every file of the ticket, recursively, as sorted relative names such as
  `logs/app.txt`.
- `export_zip(ticket)` returns a zip archive of the ticket's attachments under the same relative
  names. Identical contents must give byte-identical archives so they can be cached and diffed.
- Reading, writing or deleting never touches anything outside the ticket's own directory.
