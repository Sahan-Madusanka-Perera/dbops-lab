# A Linux host for Ansible to configure (M6): Ubuntu 24.04 booting systemd, with
# sshd and an `ansible` user that can sudo. Nothing database-related is baked in:
# that is Ansible's job, exactly as on a freshly created cloud VM.
FROM ubuntu:24.04

ENV container=docker \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      systemd systemd-sysv dbus openssh-server sudo python3 ca-certificates iproute2 \
 && apt-get clean && rm -rf /var/lib/apt/lists/* \
 # units that have nothing to do in a container
 && systemctl mask systemd-udevd.service systemd-udevd-kernel.socket systemd-udevd-control.socket \
      systemd-modules-load.service sys-kernel-config.mount sys-kernel-debug.mount getty.target \
 && systemctl enable ssh.service \
 && useradd --create-home --shell /bin/bash ansible \
 && echo 'ansible ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/ansible && chmod 0440 /etc/sudoers.d/ansible \
 && install -d -m 0700 -o ansible -g ansible /home/ansible/.ssh

# systemd stops cleanly on SIGRTMIN+3, not SIGTERM
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
