"""Paths a command explicitly names outside its repository; never execute or expand the command."""
from __future__ import annotations

import glob
import hashlib
import os
import shlex
import stat
from pathlib import Path

MAX_BYTES = 16 * 1024 * 1024


def _input_word(word, spelling):
    """Keep shell pattern characters active only outside quotes and escapes."""
    decoded, pattern, quote, active, dynamic = [], [], "", False, False
    index = 0
    while index < len(spelling):
        char = spelling[index]
        index += 1
        if char == "\\" and quote != "'" and index < len(spelling):
            following = spelling[index]
            if not quote or following in '$`"\\\n':
                index += 1
                if following != "\n":
                    decoded.append(following)
                    pattern.append(glob.escape(following))
                continue
        if char == quote:
            quote = ""
        elif char in "'\"" and not quote:
            quote = char
        else:
            decoded.append(char)
            dynamic |= char in "$`" and quote != "'"
            magic = not quote and char in "*?["
            active |= magic
            pattern.append(char if magic else glob.escape(char))
    if quote:
        return word, None, True
    return "".join(decoded), "".join(pattern) if active else None, dynamic


def _paths(command: str, content_inputs: bool = False, repository=None, directory_change=None):
    """Read quoted words too, including a shell command passed as one word to bash -c.
    Content inputs carry their optional shell pattern alongside the path.
    """
    pending, seen = [(command, Path("."))], set()
    while pending:
        line, directory = pending.pop()
        if (line, directory) in seen:
            continue
        seen.add((line, directory))
        try:
            lex = shlex.shlex(line, posix=True, punctuation_chars=";&|()<>" + ("\n" if content_inputs else ""))
            if content_inputs:
                lex.whitespace = " \t\r"
            lex.whitespace_split = True
            lex.commenters = ""
            words = []
            while True:
                # Keep the spelling too: a quoted or escaped '>' is an argument, not a redirection.
                # shlex reads one character ahead and holds it in _pushback_chars.
                start = lex.instream.tell() - len(lex._pushback_chars)
                word = lex.get_token()
                if word is None:
                    break
                end = lex.instream.tell() - len(lex._pushback_chars)
                words.append((word, line[start:end].strip(" \t\r" if content_inputs else None)))
        except ValueError:
            words = [(word, word) for word in line.split()]
        output, first, reads, cd_next, input_file, physical = False, True, True, False, False, False
        directories = []
        pipeline_directory, piped = directory, False
        history, ambiguous, command_name, redirected = {directory}, False, "", False
        for word, spelling in words:
            if output:
                output = False
                if content_inputs:
                    word, _, dynamic = _input_word(word, spelling)
                    if dynamic and ("$(" in word or "`" in word):
                        pending.extend((word.replace("`", " "), location)
                                       for location in (history | {directory} if ambiguous else {directory}))
                continue
            if spelling in (">", ">>", ">|", "&>", "&>>", ">&"):
                redirected = True
                ambiguous |= command_name == "cd"
                output = True
                continue
            word, pattern, dynamic = _input_word(word, spelling) if content_inputs else (word, None, True)
            read_word, change_dir = input_file or reads, False
            assignment = (first and "=" in word and word.split("=", 1)[0].isascii() and
                          word.split("=", 1)[0].isidentifier())
            if content_inputs:
                if spelling in ("<", "<>"):
                    redirected = True
                    ambiguous |= command_name == "cd"
                    input_file = True
                    continue
                if spelling and all(c in ";&|()\n" for c in spelling):
                    ambiguous |= "||" in spelling or ("&&" in spelling and command_name != "cd")
                    for char in spelling.replace("&&", ";").replace("||", ";"):
                        if char == "(":
                            directories.append((directory, pipeline_directory, piped))
                            pipeline_directory, piped = directory, False
                        elif char == ")" and directories:
                            directory, pipeline_directory, piped = directories.pop()
                        elif char == "|":
                            directory, piped = pipeline_directory, True
                        else:
                            if piped:
                                directory = pipeline_directory
                            pipeline_directory, piped = directory, False
                    first, reads, cd_next = True, True, False
                    command_name, redirected = "", False
                    continue
                if first and word in ("if", "elif", "else", "for", "while", "until", "case", "{", "}"):
                    ambiguous = True
                if first and not input_file and word in ("if", "then", "elif", "else", "while", "until", "do", "!"):
                    continue
                if first and not input_file and not assignment:
                    # These commands write files or text, or test metadata; their operands are not content inputs.
                    reads = word not in ("touch", "/bin/touch", "/usr/bin/touch", "echo", "printf", "test", "[")
                    cd_next = word == "cd"
                    command_name = word
                    ambiguous |= cd_next and redirected
                    physical = False
                    first = False
                    read_word = True
                else:
                    if cd_next and word in ("-P", "-L"):
                        physical = word == "-P"
                    elif command_name == "cd" and (not cd_next or (word.startswith("-") and word != "--")):
                        ambiguous = True
                    change_dir = not input_file and cd_next and not word.startswith("-")
                    if change_dir:
                        cd_next = False
                input_file = False
            # Options and assignments can hold a path after '='. No variable but HOME is read.
            if word.startswith(("/", "~/", "$HOME/", "${HOME}/", "./", "../")) or "=" not in word:
                values = [word]
            else:
                values = word.split("=")[1:]
            # An operand may be a filename containing '=', as well as an option holding a path.
            if content_inputs and not assignment and values != [word] and not word.startswith("-"):
                values.insert(0, word)
            patterns = ([pattern] if len(values) == 1 and values[0] == word else
                        ([pattern] if values[0] == word else []) + pattern.split("=")[1:]
                        if pattern else [None] * len(values))
            for value, shell_pattern in zip(values, patterns):
                if dynamic:
                    value = value.replace("${HOME}/", str(Path.home()) + "/").replace("$HOME/", str(Path.home()) + "/")
                if shell_pattern and dynamic:
                    shell_pattern = shell_pattern.replace("${HOME}/", str(Path.home()) + "/").replace("$HOME/", str(Path.home()) + "/")
                if (dynamic and ("$" in value or "`" in value)) or (value.startswith("~") and not value.startswith("~/")):
                    continue
                p = Path(value).expanduser()
                # Bare names can be repository links leading outside too; inspect resolves them.
                if read_word:
                    if content_inputs:
                        for location in sorted(history | {directory} if ambiguous else {directory}, key=str):
                            yield location / p, location / Path(shell_pattern).expanduser() if shell_pattern else None
                    else:
                        yield p
                if change_dir:
                    new_directory = directory / p
                    try:
                        if directory_change:
                            if ambiguous:
                                for previous in list(history):
                                    changed = directory_change(previous / p, physical)
                                    if changed is not None:
                                        history.add(changed)
                            changed = directory_change(new_directory, physical)
                            if changed is not None:
                                directory = changed
                        else:
                            location = Path(repository or ".") / new_directory
                            if location.is_dir() and os.access(location, os.X_OK):
                                directory = new_directory
                        history.add(directory)
                    except (OSError, RuntimeError, ValueError):
                        pass
            nested = content_inputs and ((dynamic and ("$(" in word or "`" in word)) or
                                         (read_word and any(c in word for c in ";&|()<>")))
            if word != line and (nested or (read_word and any(c.isspace() or c in "'\"" for c in word))):
                pending.extend((word.replace("`", " ") if nested else word, location)
                               for location in (history | {directory} if ambiguous else {directory}))


def inspect(command: str, repository, fingerprint: bool = True, directory=None) -> dict:
    """Existing outside text files (first 8 KB without NUL) and folders, following links and HOME.
    Unreadable or oversized files are named but have no hash. Devices and binaries are left out.
    Call only at a target change, release request/check or tick save/evaluation; views may omit hashes.
    """
    root = Path(repository).expanduser().resolve()
    files, folders, seen = [], [], set()
    if not isinstance(command, str):
        return {"outside": files, "outside_folders": folders}
    for candidate in _paths(command):
        p = Path(directory or root) / candidate
        try:
            p = p.resolve()
            if p == root or root in p.parents or p in seen:
                continue
            seen.add(p)
            st = p.stat()
            if stat.S_ISDIR(st.st_mode):
                folders.append(str(p))
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
        except PermissionError:
            files.append({"path": str(p), "why": "unreadable", "why_zh": "读不出来"})
            continue
        except (OSError, RuntimeError, ValueError):
            continue
        row = {"path": str(p)}
        try:
            with p.open("rb") as fh:
                head = fh.read(8192)
                if b"\0" in head:
                    continue
                if os.fstat(fh.fileno()).st_size > MAX_BYTES:
                    row.update(why="over 16 MB", why_zh="超过 16 MB")
                elif fingerprint:
                    digest, size = hashlib.sha256(head), len(head)
                    while size <= MAX_BYTES:
                        block = fh.read(min(65536, MAX_BYTES + 1 - size))
                        if not block:
                            break
                        digest.update(block)
                        size += len(block)
                    if size > MAX_BYTES:
                        row.update(why="over 16 MB", why_zh="超过 16 MB")
                    else:
                        row["sha256"] = digest.hexdigest()
        except OSError:
            row.update(why="unreadable", why_zh="读不出来")
        files.append(row)
    return {"outside": files, "outside_folders": folders}


def changed(before: list, after: list) -> str:
    """Compare against the recording: hashes must match, new files invalidate, unhashed recordings are exempt."""
    old, new = ({x["path"]: x for x in rows} for rows in (before, after))
    for path in dict.fromkeys([*old, *new]):
        a, b = old.get(path), new.get(path)
        if a is None or (a.get("sha256") and a["sha256"] != (b or {}).get("sha256")):
            return path
    return ""


def lines(found: dict) -> list:
    return [f"outside file: {x['path']}" + (f"; sha256 {x['sha256']}" if x.get("sha256") else
            f"; not fingerprinted: {x['why']}" if x.get("why") else "") for x in found.get("outside") or []] + [
            f"outside folder the command runs in: {p}; no fingerprint" for p in found.get("outside_folders") or []]
