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

`python3 -m unittest tests.test_docs` also checks every file under `docs/zh-CN/`
against the English file at the same relative path under `docs/`. Each translation
must have an English counterpart, the same heading levels in the same order,
and exactly the same fenced code blocks in order (including commands, output,
paths and fence markers). Mermaid blocks may translate their labels; only their
count must match. Headings inside code examples do not count. Diagnostics name
the file and the first differing heading or block. This prevents structural and
command drift as translations grow; it does not check whether the wording is
accurate or up to date. Keep it while paired English and Chinese docs are maintained.

## Runner output

When `tests/run_parallel.py` reports failures or errors, its last line is
`Failed tests: <name>, <name>`, on the same stream as unittest's summary and
after the optional `--slowest K` list. Names use `str(test)` in unittest's
reporting order (errors, then failures). It lists at most ten names, followed
by `and N more` for any remaining entries. Skipped tests do not count, and a
run without failures or errors has no such line. This keeps failing test names
visible in the last lines shown by lane checks and release reports.

## Bench test clocks and connections

The lifecycle tests replace `selftest.time`, rather than `selftest.time.sleep`:
the latter changes the shared Python `time` module for every thread. An unrelated
thread calling `time.sleep` could advance the test clock beyond the lifecycle's
90 second idle-stop deadline. `test_lifecycle_clock_ignores_unrelated_sleeps`
keeps unrelated clock calls in another thread while exercising both lifecycle
fixtures. Keep it while those fixtures advance a fake clock through sleep calls.

The DHCP tests run the shipped source with imports supplied through that exec's
own builtins. They still exercise lease renewal, stale/missing addresses and a
removed lease, but no longer replace shared `time`, `subprocess` or `pathlib`
attributes. Both DHCP fixtures make unrelated clock calls on another thread:
under the old patches these consumed the finite monotonic sequence and raised
`StopIteration`. Keep that interference while the probes use a finite fake clock.

The broker cancellation fixture duplicates the input descriptor, just as the
real SSH child inherits its own descriptor. It waits for the full request before
allowing cancellation, then deliberately waits until VM stop, after the broker
has closed its original file object, before checking EOF. Reading the original
file object in a loop could instead raise `ValueError` between reads and never
signal connection completion. The fixture retains the cancelled reply, thread
join, stop/reset/start recovery and unchanged cancelled-run files checks. Events
and joins wait for those conditions; 60 second bounds only guard against hangs.

<a name="other-shared-patches-audited"></a>

### Shared patches and thread isolation

Patching an attribute on an imported module, or on a class, affects other threads
using that same object. Before adding a patch, check which threads can call the
target while it is active. Prefer a fake attached to the module under test or a
particular instance. For shipped source run with `exec`, supply fake imports
through that execution's own builtins, as `exec_probe` in `test_11.py` does.

When changing these shared standard-library targets in `tests/bench/`, check
their callers in background threads too (grouped by file):

| File | Targets |
| --- | --- |
| `test_1.py` | `os.open`, `os.getuid`, `os.fdopen`, `os.fstat`, `os.link`, `builtins.__import__` |
| `test_2.py` | `tarfile.TarFile.addfile`, `tempfile.TemporaryFile` |
| `test_3.py` | `tarfile.TarFile.extract`, `uuid.uuid4` |
| `test_4.py` | `subprocess.Popen`, `os.sync`, `shutil.rmtree` |
| `test_6.py` | `shutil.chown`, `os.sync`, `os.path.ismount`, `os.rmdir`, `select.select`, `os.read`, `threading.Thread.join` |
| `test_7.py` | `pathlib.Path.read_bytes` |
| `test_10.py` | `urllib.request.build_opener` |
| `test_11.py` | `socket.socket`, `socket.create_connection`, `socket.getaddrinfo`, `socket.gethostname`, `pwd.getpwnam`, `subprocess.Popen` |

Also shared are environment patches in `test_1.py`, `test_2.py` and `test_4.py`,
`sys.platform` patches in `test_6.py`, `test_7.py` and `test_9.py`, and `sys.argv`
and stdout redirection in `test_11.py`. Hansa module functions and classes patched
by these tests are visible to other callers of those same Hansa objects too;
notably `test_8.py` patches snapshot export, channel claim/read/reply and archive
delivery while running broker threads. Instance patches, such as a particular
fake VM's `connect`, affect only callers using that instance.

Keep a shared patch active until every thread that uses it has finished. Use
events, files or observable state to wait for readiness and completion, with
timeouts to guard against hangs. If a shared patch is needed, exercise unrelated
callers too when they could consume a fake clock or a finite response sequence.
Check that they do not alter the fixture's state.

Some patches must reach another thread: the `test_6.py` stderr fixture intercepts
its drain thread's select/read/join operations. Keep that interception scoped to
the fixture and wait for the drain thread before restoring the targets.
