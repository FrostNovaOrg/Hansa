"""bh: a small, file-based desk for a team of Claude Code agents.

One directory (the desk) holds the settings, the keys, the tasks and an
append-only event log. The CLI, the web page and the agents all read the
same files, so nobody has to read this code to know what is going on.
"""

__version__ = "0.5.0a1"
