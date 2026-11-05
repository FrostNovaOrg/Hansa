#!/bin/sh
# Run as root via Lima admin, exclusively while disabled, from a trusted bundle:
# these four recipe files, guest.py, archive.py and submission.pub (public key only).
# No shares/provision hooks: the later build command uploads the bundle over SSH.
set -eu
umask 022
[ "$#" -eq 0 ]
[ "$(id -u)" -eq 0 ]
cd -- "$(dirname -- "$0")"
if [ ! -s submission.pub ]; then
    echo 'bench: submission.pub is missing or empty; provide a public key before provisioning' >&2
    exit 1
fi
export DEBIAN_FRONTEND=noninteractive
packages='bubblewrap nftables tmux chromium fonts-dejavu-core git python3 openssh-server'
apt-get update
apt-get install -y --no-install-recommends $packages
python3 -I -c 'import sys; sys.exit(sys.version_info[:2] != (3, 13))'
apt-get clean

id -u bench >/dev/null 2>&1 || useradd -m -s /bin/sh bench
# A rerun must not retain supplementary privileges; never alter Lima's admin.
usermod -G '' -s /bin/sh bench
passwd -l bench
install -d -o root -g root -m 0755 /usr/local/lib/hansa-bench/bench /usr/local/libexec /etc/ssh/bench
install -o root -g root -m 0644 guest.py archive.py /usr/local/lib/hansa-bench/bench/
install -o root -g root -m 0644 /dev/null /usr/local/lib/hansa-bench/bench/__init__.py
cat > /usr/local/libexec/hansa-bench <<'GATEWAY'
#!/bin/sh
exec /usr/bin/python3 -I -c 'import sys, runpy; sys.path.insert(0, "/usr/local/lib/hansa-bench"); runpy.run_module("bench.guest", run_name="__main__")'
GATEWAY
chown root:root /usr/local/libexec/hansa-bench
chmod 0755 /usr/local/libexec/hansa-bench
# Disk-backed slots; the gateway makes private work/out/tmp/shm directories per call.
install -d -o bench -g bench -m 0700 /var/lib/hansa-bench
install -d -o root -g root -m 0700 /var/lib/hansa-bench-build
dpkg-query -W -f='${binary:Package}\t${Version}\n' $packages > /var/lib/hansa-bench-build/packages.tsv
sha256sum lima.yaml provision.sh edge.nft sysctl.conf guest.py archive.py > /var/lib/hansa-bench-build/recipe.sha256
install -o root -g root -m 0600 lima.yaml /var/lib/hansa-bench-build/lima.yaml

install -d -o root -g root -m 0755 /etc/cloud/cloud.cfg.d /etc/sysctl.d /etc/ssh/sshd_config.d
# Lima changes cloud-init instance IDs at boot; keep the identity pinned by the host.
cat > /etc/cloud/cloud.cfg.d/99-bench-keep-host-keys.cfg <<'CLOUD'
ssh_deletekeys: false
ssh_genkeytypes: []
CLOUD
install -o root -g root -m 0644 sysctl.conf /etc/sysctl.d/90-hansa-bench.conf
sysctl --load=/etc/sysctl.d/90-hansa-bench.conf
install -o root -g root -m 0644 edge.nft /etc/nftables.conf
nft -c -f /etc/nftables.conf
nft -f /etc/nftables.conf
systemctl enable --now nftables.service

# Install credentials only after the persistent edge is active; fail before reload.
install -o root -g root -m 0644 submission.pub /etc/ssh/bench/authorized_keys
cat > /etc/ssh/sshd_config.d/50-bench.conf <<'SSHD'
Match User bench
    AuthorizedKeysFile /etc/ssh/bench/authorized_keys
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    ForceCommand /usr/local/libexec/hansa-bench
    PermitTTY no
    AllowTcpForwarding no
    AllowStreamLocalForwarding no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTunnel no
    PermitUserRC no
Match all
SSHD
sshd -t
systemctl reload ssh.service
