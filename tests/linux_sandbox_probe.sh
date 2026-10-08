#!/bin/sh
# What a lane's commands find in the sandbox on Linux: run it as a lane's check (README, "On a Linux server"),
# in the lane's worktree. It prints, for the TMPDIR it is given and for the worktree, what the mount table says
# (findmnt, df), what Java's FileStore.isReadOnly says (when java is there), and whether a write there works;
# then the untracked files git status lists at the top of the worktree. It changes nothing in the worktree and
# always exits 0, so the lane's try reads as passed and the log holds the answers.
echo "TMPDIR=${TMPDIR:-} (expect it to end in /hansa-tmpfs, a tmpfs of its own)"
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
for d in "${TMPDIR:-/tmp}" "$(pwd)"; do  # three lines each: hansa lane try prints a log's last 15
  echo "findmnt $d: $(findmnt -n -T "$d" -o TARGET,FSTYPE,OPTIONS 2>&1 | head -n 1)"
  echo "df $d: $(df -P "$d" 2>&1 | tail -n 1)"
  f="$d/.hansa-probe-$$"
  if : >"$f" 2>/dev/null; then echo "write $d: works"; rm -f "$f"; else echo "write $d: refused"; fi
done
if [ -n "$src" ] && command -v java >/dev/null 2>&1; then
  java "$src" "${TMPDIR:-/tmp}" "$(pwd)" 2>&1 | grep -v '^Picked up JAVA_TOOL_OPTIONS'
else
  echo "java: not on the PATH (or no TMPDIR to write its source in)"
fi
rm -f "$src"
echo "git status, untracked at the top (expect none of .bashrc, .gitconfig, .mcp.json, .vscode and the like;" \
  "excludes ${HANSA_GIT_EXCLUDES:-not set}):"
git status --porcelain -uall 2>&1 | grep '^?? [^/]*$' || echo "(none)"
exit 0
