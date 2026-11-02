# Documentation tests

`python3 tests/run_parallel.py` includes `tests.test_docs`. Run just these tests
with `python3 tests/run_parallel.py tests.test_docs`.

The check prevents guides from silently pointing to missing files or sections
after a move or heading edit. It reads Markdown files listed by `git ls-files`,
excluding `tests/` and `contrib/codex/tests/`, where examples may intentionally
contain broken links. New Markdown files enter the check once Git tracks them.

Relative inline links must reach an existing file or folder. A fragment on a
Markdown link, including a link within the same file, must match a heading or
an explicit `<a name="...">` anchor. Heading anchors follow GitHub's lower-case
slugs, retaining Unicode letters and numbering duplicate headings with `-1`,
`-2`, and so on. External URLs and links inside fenced or inline code are skipped;
the check uses only the standard library and does not make network requests.

The test prints how many relative links it checked. Each broken link is reported
with its source file, line, destination and whether the file or anchor is missing.
A temporary Git repository with a missing file and a missing anchor proves that
the checker reports both failures. Keep this check until a replacement provides
the same coverage for local documentation links.

## Runner output

When `tests/run_parallel.py` reports failures or errors, its last line is
`Failed tests: <name>, <name>`, on the same stream as unittest's summary and
after the optional `--slowest K` list. Names use `str(test)` in unittest's
reporting order (errors, then failures). It lists at most ten names, followed
by `and N more` for any remaining entries. Skipped tests do not count, and a
run without failures or errors has no such line. This keeps failing test names
visible in the last lines shown by lane checks and release reports.
