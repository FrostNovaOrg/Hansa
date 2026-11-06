"""The git environment the tests run in, as it is on GitHub's machines: git is given an identity, it never invents
one. macOS invents one from the account and a hostname with a domain part; a Linux host with no git identity (a CI
runner, the Linux test bench) refuses a commit with "unable to auto-detect email address". A test that commits
without `-c user.name=... -c user.email=...` (CONTRIBUTING.md, Tests) was for that reason seen to fail only after
it was merged, by the Linux runs; with this it fails here too."""
import os

# What every git the tests start, and every git hansa starts inside them, is given: git's own guess of an identity
# is off (user.useConfigOnly), and so is the account's global and system config, which a developer's Mac may hold an
# identity in and GitHub's machines do not. An identity in the environment (hansa gives a worker one) is no more
# configured than git's guess, so that goes too. It can go when the suite no longer makes commits.
GIT_ENV = {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "user.useConfigOnly",
    "GIT_CONFIG_VALUE_0": "true",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}
IDENTITY_ENV = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL")


def apply(env=os.environ) -> None:
    """Give env the suite's git settings, without an identity the suite did not set itself."""
    env.update(GIT_ENV)
    for name in IDENTITY_ENV:
        env.pop(name, None)
