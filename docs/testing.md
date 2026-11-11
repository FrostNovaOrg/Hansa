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

## What the runner runs

`python3 tests/run_parallel.py` with no names runs the modules in its `SUITES`:
`tests.test_hansa`, `tests.test_push_services`, `tests.test_docs`,
`tests.test_mod` and `contrib.codex.tests`. GitHub's checks name the same
modules in the workflow's "Run the tests" step, and `WorkflowTest` keeps the two
lists equal. `tests/test_menu.py`, `tests/test_doctor.py`, `tests/test_install.py`
and the files in `tests/bench/` have no entry of their own: `tests.test_hansa`
imports their test classes, so they run wherever it runs.
`test_every_test_class_under_tests_is_in_those_suites` fails for a test class
under `tests/` (or `contrib/codex/tests/`) that none of those modules reaches.

## The coordinator's mod

`tests.test_mod` runs the mod's validation and tests (`claude plugin validate
hansa/mod` and `claude plugin test hansa/mod`), each as one test, with the
`claude` found on the PATH when the module loads. A failure fails the run and
names `test_the_coordinators_mod_validates` or
`test_the_coordinators_mod_passes_its_tests` in the `Failed tests:` line; its
message holds what `claude` said, without the tests that passed. The commands
run with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, so they send nothing to
Anthropic, and need no login.

Both tests skip, saying why, where that `claude` cannot run them: there is no
`claude` on the PATH; it is older than 2.1.306 or has no `claude plugin test`; or
a sandbox refused what it needed (a line saying `operation not permitted`,
`EPERM`, `EACCES` or `permission denied` before `claude` reported a result). With
`HANSA_TESTS_NEED_CLAUDE_PLUGIN=1`, as every CI job sets, a skip fails instead.
`ModRunTest` checks each of these with stand-ins for `claude`.

Tests in the mod's own kit must not rely on how far `clock.advance` or
`clock.settle` gets while one of the test's own hooks is waiting (a clear held
open, say). The kit cannot wait for such a hook: it stops waiting once nothing
has moved for a moment, so on a loaded machine the advance can return before the
hook has done what the test then reads. Hold such a call with a promise the test
resolves itself, as `holdClear` in `hansa/mod/tests/register.test.ts` does, and
look at what happened from the test's body, after awaiting what it waits for.

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

## The account's state in tests

hansa keeps what desks on one account share under `XDG_STATE_HOME` (else
`~/.local/state`): the list of desks and `provider-rests.json`, the rests shared
between desks on one login. A test that makes a desk sets `XDG_STATE_HOME` to a
folder of its own, as the Codex adapter's `Scratch` (`contrib/codex/tests/codexkit.py`)
does for all its tests. Their providers share one placeholder login, so without it a
credit rest one test records in the account's file is shared into the next test's
desk, whose usage limit then starts no rest of its own. The file is missed only where
a sandbox keeps tests from writing the real one, as hansa's worker sandbox on a Mac does.
