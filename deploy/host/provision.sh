#!/usr/bin/env bash
#
# Prepares a fresh Debian or Ubuntu host for the KeenySpace compose stack:
#   1. optionally formats (first run only) and mounts a dedicated data disk,
#   2. installs Docker Engine, with its data-root on that disk when one is given,
#      so every named volume (canon markdown, both Postgres databases,
#      certificates) lives on storage that can outlive the machine,
#   3. installs the scheduled-backup systemd units (not enabled).
#
# Idempotent: safe to re-run. Run as root from the repository checkout:
#   DATA_DEVICE=/dev/disk/by-id/<your-disk> bash deploy/host/provision.sh
#
# DATA_DEVICE is optional; without it Docker keeps its default data-root on the
# system disk. A device that already carries a filesystem is never reformatted.
# If Docker is already installed it is left alone; set its data-root yourself.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_DEVICE="${DATA_DEVICE:-}"
DATA_MOUNT="${DATA_MOUNT:-/mnt/keenyspace}"

if [ "$(id -u)" -ne 0 ]; then
  echo "provision.sh must run as root" >&2
  exit 1
fi

mount_data_device() {
  local tries=0
  # Cloud block devices can appear a few seconds after boot.
  until [ -b "$DATA_DEVICE" ]; do
    tries=$((tries + 1))
    if [ "$tries" -gt 30 ]; then
      echo "Data device $DATA_DEVICE not found" >&2
      exit 1
    fi
    sleep 2
  done

  if ! blkid "$DATA_DEVICE" > /dev/null 2>&1; then
    mkfs.ext4 -q "$DATA_DEVICE"
  fi

  mkdir -p "$DATA_MOUNT"
  local uuid
  uuid="$(blkid -s UUID -o value "$DATA_DEVICE")"
  if ! grep -q "UUID=$uuid" /etc/fstab; then
    echo "UUID=$uuid $DATA_MOUNT ext4 defaults,nofail,discard 0 2" >> /etc/fstab
  fi
  if ! mountpoint -q "$DATA_MOUNT"; then
    mount "$DATA_MOUNT"
  fi
}

install_docker() {
  if command -v docker > /dev/null 2>&1; then
    return
  fi

  if [ -n "$DATA_DEVICE" ]; then
    mkdir -p /etc/docker "$DATA_MOUNT/docker"
    printf '{\n  "data-root": "%s/docker"\n}\n' "$DATA_MOUNT" > /etc/docker/daemon.json
  fi

  local distro codename
  # shellcheck disable=SC1091  # os-release exists only on the target host
  distro="$(. /etc/os-release && echo "$ID")"
  # shellcheck disable=SC1091
  codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"
  case "$distro" in
    debian | ubuntu) ;;
    *)
      echo "Unsupported distribution '$distro': install Docker Engine with the compose plugin, then re-run" >&2
      exit 1
      ;;
  esac

  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$distro/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$distro $codename stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
}

install_backup_units() {
  sed "s|__REPO_DIR__|$REPO_DIR|g" "$REPO_DIR/deploy/host/keenyspace-backup.service" \
    > /etc/systemd/system/keenyspace-backup.service
  install -m 0644 "$REPO_DIR/deploy/host/keenyspace-backup.timer" /etc/systemd/system/
  install -d -m 0700 /etc/keenyspace
  if [ ! -f /etc/keenyspace/backup.env ]; then
    install -m 0600 "$REPO_DIR/deploy/host/backup.env.example" /etc/keenyspace/backup.env
  fi
  systemctl daemon-reload
}

if [ -n "$DATA_DEVICE" ]; then
  mount_data_device
fi
install_docker
install_backup_units

echo "Provisioned. Docker data-root: $(docker info --format '{{.DockerRootDir}}')"
