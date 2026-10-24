"""Paths a command explicitly names outside its repository; never execute or expand the command."""
from __future__ import annotations

import glob
import hashlib
import os
import re
import shlex
import stat
from pathlib import Path

MAX_BYTES = 16 * 1024 * 1024


def _input_word(spelling):
    """Decode shell fragments while keeping quoted and escaped pattern characters literal."""
    decoded, patterns, dynamic = [], [], False
    for token in re.findall(r'''\\[\s\S]|'[^']*'|"(?:\\[\s\S]|[^"\\])*"|[^\\'"]+''', spelling):
        quote = token[0]
        dynamic |= quote != "'" and bool(re.search(r"[$`]", re.sub(r"\\[\s\S]", "", token)))
        value = token[1:-1] if quote in "'\"" else token[1:].replace("\n", "") if quote == "\\" else token
        if quote == '"':
            value = re.sub(r'\\([$`"\\\n])', lambda m: m[1] if m[1] != "\n" else "", value)
        decoded.append(value)
        patterns.append(glob.escape(value) if quote in "'\"\\" else value)
    return "".join(decoded), "".join(patterns), dynamic


def _input_paths(command: str, directory_change):
    """Recognized content inputs, with literal quoting and possible shell working directories."""
    pending = [(command, frozenset({Path(".")}))]
    while pending:
        line, locations = pending.pop()
        # A word consists of unquoted characters, escapes and quoted fragments; operators stand alone.
        words = re.findall(r'''(?:[^\s;&|()<>'"\\]|\\[\s\S]|'[^']*'|"(?:\\[\s\S]|[^"\\])*")+|[;&|()<>\n]+''', line)
        output, first, reads, cd_next, input_file, physical, skip_option = False, True, False, False, False, False, False
        stack, pipeline, piped, before, ambiguous, name, redirected = [], locations, False, locations, False, "", False
        for index, spelling in enumerate(words):
            word, pattern, dynamic = _input_word(spelling)
            if dynamic and ("$(" in word or "`" in word):
                pending.append((word.replace("`", " "), locations))
            if output:
                output = False
                continue
            if word.isdecimal() and words[index + 1:index + 2] and words[index + 1] in ("<", "<>", ">", ">>", ">|", ">&"):
                continue
            if spelling in (">", ">>", ">|", "&>", "&>>", ">&", "<", "<>"):
                output, input_file, redirected = spelling not in ("<", "<>"), spelling in ("<", "<>"), True
                if name == "cd":
                    ambiguous, locations = True, locations | before
                continue
            if spelling and all(c in ";&|()\n" for c in spelling):
                ambiguous |= "||" in spelling or ("&&" in spelling and name != "cd")
                for char in spelling.replace("&&", ";").replace("||", ";"):
                    if char == "(":
                        stack.append((locations, pipeline, piped))
                        pipeline, piped = locations, False
                    elif char == ")" and stack:
                        locations, pipeline, piped = stack.pop()
                    elif char == "|":
                        locations, piped = pipeline, True
                    else:
                        locations = pipeline if piped else locations
                        pipeline, piped = locations, False
                first, reads, cd_next, name, redirected, before, skip_option = True, False, False, "", False, locations, False
                continue
            ambiguous |= first and word in ("if", "elif", "else", "for", "while", "until", "case", "{", "}")
            if first and not input_file and word in ("if", "then", "elif", "else", "while", "until", "do", "!"):
                continue
            if first and not input_file and re.match(r"^[a-zA-Z_][a-zA-Z_0-9]*=", word):
                continue
            read_word, change_dir = input_file or reads, False
            if name in ("sed", "awk", "gawk", "mawk") and word in ("-f", "--file"):
                input_file = True
                continue
            read_word |= name == "sed" and word.startswith(("-f", "--file="))
            if first and not input_file:
                if name and (word.startswith("-") or skip_option):
                    skip_option = not skip_option and word in ("-u", "--unset", "-g", "--group", "--user", "-n", "-a")
                    continue
                name = Path(word).name or word
                reads = name in ("sh bash dash zsh ksh ash python python3 python2 perl ruby node php lua . source "
                                 "cat awk gawk mawk env sudo command exec nohup timeout xargs find").split()
                reads |= bool(re.fullmatch(r"python[0-9.]+", name))
                cd_next, physical, first, read_word = word == "cd", False, name in ("env", "sudo", "command", "exec", "nohup", "nice"), True
                ambiguous |= cd_next and redirected
            elif cd_next and word in ("-P", "-L"):
                physical = word == "-P"
            else:
                if name == "cd" and (not cd_next or (word.startswith("-") and word != "--")):
                    ambiguous, locations = True, locations | before
                change_dir = not input_file and cd_next and not word.startswith("-")
                cd_next &= not change_dir
            if name in ("sed", "awk", "gawk", "mawk") and word.startswith("-f") and len(word) > 2:
                value, shell_pattern = word[2:], pattern[2:]
            else:
                value = word.split("=", 1)[-1] if word.startswith("-") and not input_file else word
                shell_pattern = pattern.split("=", 1)[-1] if word.startswith("-") and not input_file else pattern
            if dynamic:
                for prefix in ("${HOME}/", "$HOME/"):
                    value = value.replace(prefix, str(Path.home()) + "/")
                    shell_pattern = shell_pattern.replace(prefix, glob.escape(str(Path.home())) + "/")
            if not (dynamic and ("$" in value or "`" in value)) and not (value.startswith("~") and not value.startswith("~/")):
                p = Path(value).expanduser()
                if shell_pattern.startswith("~/"):
                    shell_pattern = glob.escape(str(Path.home())) + shell_pattern[1:]
                if read_word:
                    for location in sorted(locations, key=str):
                        yield location / p, location / Path(shell_pattern).expanduser()
                if change_dir:
                    changed = frozenset(directory_change(location / p, physical) or location for location in locations)
                    locations = locations | changed if ambiguous else changed
            input_file = False
            if read_word and word != line and any(c.isspace() or c in "'\";&|()<>" for c in word):
                pending.append((word, locations))


def _paths(command: str):
    """Read quoted words too, including a shell command passed as one word to bash -c."""
    pending, seen = [command], set()
    while pending:
        line = pending.pop()
        if line in seen:
            continue
        seen.add(line)
        try:
            lex = shlex.shlex(line, posix=True, punctuation_chars=";&|()<>")
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
                words.append((word, line[start:end].strip()))
        except ValueError:
            words = [(word, word) for word in line.split()]
        output = False
        for word, spelling in words:
            if output:
                output = False
                continue
            if spelling in (">", ">>", ">|", "&>", "&>>", ">&"):
                output = True
                continue
            # Options and assignments can hold a path after '='. No variable but HOME is read.
            if word.startswith(("/", "~/", "$HOME/", "${HOME}/", "./", "../")) or "=" not in word:
                values = [word]
            else:
                values = word.split("=")[1:]
            for value in values:
                value = value.replace("${HOME}/", str(Path.home()) + "/").replace("$HOME/", str(Path.home()) + "/")
                if "$" in value or "`" in value or (value.startswith("~") and not value.startswith("~/")):
                    continue
                p = Path(value).expanduser()
                # Bare names can be repository links leading outside too; inspect resolves them.
                yield p
            if word != line and any(c.isspace() or c in "'\"" for c in word):
                pending.append(word)


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
