#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
config_dir="$HOME/.config/turtlewow-manager"
mkdir -p "$config_dir" "$HOME/.config/systemd/user"
chmod 700 "$config_dir"
if [[ ! -f "$config_dir/config.json" ]]; then
  cp "$project_dir/config.example.json" "$config_dir/config.json"
  chmod 600 "$config_dir/config.json"
fi
if [[ "$project_dir" != "$HOME/development/turtlewow-manager" ]]; then
  echo 'Edit WorkingDirectory/ExecStart in the service for your checkout path before installing.' >&2
  exit 1
fi
cp "$project_dir/turtlewow-manager.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now turtlewow-manager.service
echo 'Manager is listening on 127.0.0.1:8787. Create an admin account and configure HTTPS as described in README.md.'
echo 'For startup before SSH login, enable lingering once: sudo loginctl enable-linger "$USER"'
