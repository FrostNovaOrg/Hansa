#!/bin/sh
# Install for this account only. Keep stdin available to a shell reading a pipe.

fail() {
    printf 'hansa install: %s\n' "$1" >&2
    exit 1
}

command -v git >/dev/null 2>&1 || fail 'git is required. Install Git, then run this installer again.'
command -v python3 >/dev/null 2>&1 || fail 'python3 3.9 or newer is required.'
python3 -B -c 'import sys; sys.exit(sys.version_info < (3, 9))' </dev/null ||
    fail 'python3 3.9 or newer is required.'

case ${HOME:-} in
    /*) ;;
    *) fail 'HOME must be an absolute path.' ;;
esac
install_dir=$HOME/.local/share/hansa
bin_dir=$HOME/.local/bin
link=$bin_dir/hansa
source=${HANSA_SOURCE:-https://github.com/FrostNovaOrg/Hansa.git}

# Reject conflicts before mkdir, clone or pull can change anything.
if [ -e "$install_dir" ] || [ -L "$install_dir" ]; then
    [ -e "$install_dir/.git" ] && [ -f "$install_dir/bin/hansa" ] &&
        [ "$(git -C "$install_dir" rev-parse --is-inside-work-tree </dev/null 2>/dev/null)" = true ] &&
        [ -z "$(git -C "$install_dir" rev-parse --show-prefix </dev/null 2>/dev/null)" ] ||
        fail "$install_dir exists and is not a hansa clone. Move it aside first."
fi
if [ -e "$link" ] || [ -L "$link" ]; then
    python3 -B -c '
import os, sys
link, target = sys.argv[1:]
sys.exit(not (os.path.islink(link) and os.path.realpath(link) == os.path.realpath(target)))
' "$link" "$install_dir/bin/hansa" </dev/null ||
        fail "$link exists and is not a link to this installation. Move it aside first."
fi

# Git must not ask for credentials, including when the source uses SSH.
GIT_TERMINAL_PROMPT=0
GIT_SSH_COMMAND='ssh -oBatchMode=yes'
export GIT_TERMINAL_PROMPT GIT_SSH_COMMAND

if [ -d "$install_dir" ]; then
    git -C "$install_dir" pull --ff-only </dev/null ||
        fail 'Update failed; the installed files have been left in place.'
    action=Updated
else
    mkdir -p "$HOME/.local/share" || fail 'Could not create the installation parent folder.'
    # A failed or interrupted clone must not leave an incomplete installation.
    trap 'rm -rf "$install_dir"' 0
    trap 'exit 1' 1 2 15
    git clone -- "$source" "$install_dir" </dev/null || fail 'Clone failed.'
    trap - 0 1 2 15
    action=Installed
fi
mkdir -p "$bin_dir" || fail "Could not create $bin_dir."
if [ -L "$link" ]; then
    rm -f "$link" || fail "Could not replace $link."
fi
ln -s "$install_dir/bin/hansa" "$link" || fail "Could not create $link."
printf '%s hansa at %s\n\n' "$action" "$install_dir"

# Quote commands for copying into a shell, including HOME paths with spaces.
python3 -B - "$link" "$install_dir/docs/getting-started.md" <<'PY'
import os
import shlex
import sys

link, guide = sys.argv[1:]
on_path = any(os.path.abspath(p or '.') == os.path.dirname(link)
              for p in os.environ.get('PATH', '').split(':'))
command = 'hansa' if on_path else shlex.quote(link)
lang = os.environ.get('LC_ALL') or os.environ.get('LC_MESSAGES') or os.environ.get('LANG', '')
zh = lang.startswith('zh')
if not on_path:
    print('export PATH="$HOME/.local/bin:$PATH"')
    print('请自行将上面这行添加到 shell 配置文件。' if zh else
          'Add the line above to your shell profile yourself.')
    print()
steps = [
    (' doctor', '检查 hansa 所需的工具，并说明如何安装缺少的工具',
     'checks the tools hansa needs and says how to get what is missing'),
    (' init ~/shop/desk --repo ~/shop/repo', '为你的项目创建工作台',
     'creates a desk for your project'),
    ('', '打开菜单：选择工作台，按 s 启动，按 Enter 打开协调员',
     'opens the menu: pick a desk, s starts it, Enter opens its coordinator'),
]
for args, chinese, english in steps:
    print(f'{command + args:<44} {chinese if zh else english}')
print()
print('从另一台电脑连接：' if zh else 'From another computer:')
# SSH joins its arguments into a remote shell command: preserve both shells' quotes.
print('ssh -t -L 8765:127.0.0.1:8765 YOUR-HOST ' + shlex.quote(shlex.quote(link)))
print()
print(('入门指南：' if zh else 'Getting started: ') + guide)
PY
