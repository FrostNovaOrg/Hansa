"""For hansa's tests only: a hansa started as a command answers, for the account's home folder, the folder
named in HANSA_TESTS_HOME. The tests put this folder on PYTHONPATH (tests/test_hansa.py, as_home); hansa itself
never reads the variable."""
import os
import pwd

if os.environ.get("HANSA_TESTS_HOME"):
    _real = pwd.getpwuid

    def _getpwuid(uid):
        pw = _real(uid)
        return pwd.struct_passwd((*pw[:5], os.environ["HANSA_TESTS_HOME"], *pw[6:]))

    pwd.getpwuid = _getpwuid
