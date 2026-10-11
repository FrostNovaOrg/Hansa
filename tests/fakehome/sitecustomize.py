"""For hansa's tests only: a hansa started as a command answers, for the account's home folder, the folder
named in HANSA_TESTS_HOME; and with HANSA_TESTS_ROOT_ABOVE, takes the folders above the one it names as root's
(tests/test_hansa.py, root_above). The tests put this folder on PYTHONPATH (tests/test_hansa.py, as_home); hansa
itself never reads the variables."""
import os
import pwd

if os.environ.get("HANSA_TESTS_HOME"):
    _real = pwd.getpwuid

    def _getpwuid(uid):
        pw = _real(uid)
        return pwd.struct_passwd((*pw[:5], os.environ["HANSA_TESTS_HOME"], *pw[6:]))

    pwd.getpwuid = _getpwuid

if os.environ.get("HANSA_TESTS_ROOT_ABOVE"):
    from pathlib import Path

    from hansa import account  # the hansa that runs: its folder is first on PYTHONPATH (bin/hansa)

    _above, _owner = list(Path(os.path.realpath(os.environ["HANSA_TESTS_ROOT_ABOVE"])).parents), account.owner
    account.owner = lambda path: 0 if Path(os.path.realpath(path)) in _above else _owner(path)
