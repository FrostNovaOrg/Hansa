#!/bin/sh
# What a lane's commands find in the sandbox on Linux: run it as a lane's check (docs/linux-server.md),
# in the lane's worktree. It prints, for the TMPDIR it is given and for the worktree, what the mount table says
# (findmnt, df), what Java's FileStore.isReadOnly says (when java is there), and whether a write there works;
# then the untracked files git status lists at the top of the worktree (the first 3, and how many more). It prints
# at most 14 lines, within the 15 of a log that hansa lane try prints: 1 for TMPDIR, 3 for each folder, at most 2
# for Java and at most 5 for git. It changes nothing in the worktree and always exits 0, so the lane's try reads
# as passed and the log holds the answers.
echo "TMPDIR=${TMPDIR:-} (expect the desk's .hansa/tmp/hansa-check-*, a folder on disk mounted on its own)"
src=${TMPDIR:-/tmp}/HansaProbe.java
cat >"$src" <<'EOF' 2>/dev/null || src=
import java.nio.file.*;
public class HansaProbe {
    public static void main(String[] a) throws Exception {
        for (String s : a) {
            Path p = Path.of(s);
            FileStore f = Files.getFileStore(p);
            System.out.println("java: " + s + ": FileStore " + f + " type " + f.type() + " isReadOnly=" + f.isReadOnly());
        }
    }
}
EOF
for d in "${TMPDIR:-/tmp}" "$(pwd)"; do  # three lines each
  echo "findmnt $d: $(findmnt -n -T "$d" -o TARGET,FSTYPE,OPTIONS 2>&1 | head -n 1)"
  echo "df $d: $(df -P "$d" 2>&1 | tail -n 1)"
  f="$d/.hansa-probe-$$"
  if : >"$f" 2>/dev/null; then echo "write $d: works"; rm -f "$f"; else echo "write $d: refused"; fi
done
if [ -n "$src" ] && command -v java >/dev/null 2>&1; then
  java "$src" "${TMPDIR:-/tmp}" "$(pwd)" 2>&1 | grep -v '^Picked up JAVA_TOOL_OPTIONS' | head -n 2
else
  echo "java: not on the PATH (or no TMPDIR to write its source in)"
fi
rm -f "$src"
echo "git status, untracked at the top (expect none of .bashrc, .gitconfig, .mcp.json, .vscode and the like;" \
  "excludes ${HANSA_GIT_EXCLUDES:-not set}, global config ${GIT_CONFIG_GLOBAL:-not set}):"
git status --porcelain -uall 2>&1 | grep '^?? [^/]*$' >"${TMPDIR:-/tmp}/.hansa-probe-git-$$"
n=$(wc -l <"${TMPDIR:-/tmp}/.hansa-probe-git-$$" 2>/dev/null | tr -d ' ')
head -n 3 "${TMPDIR:-/tmp}/.hansa-probe-git-$$" 2>/dev/null
case ${n:-0} in 0) echo "(none)" ;; 1|2|3) ;; *) echo "... and $((n - 3)) more" ;; esac
rm -f "${TMPDIR:-/tmp}/.hansa-probe-git-$$"
exit 0
