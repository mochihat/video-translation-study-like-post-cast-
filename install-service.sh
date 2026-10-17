#!/usr/bin/env bash
# Cho web tự chạy nền mỗi khi bật máy/đăng nhập — lần sau chỉ cần mở http://localhost:5000
#   Cài:  ./install-service.sh
#   Gỡ:   ./install-service.sh --remove
#   Xem log:  journalctl --user -u youtube-dub -f
set -e
cd "$(dirname "$0")"
DIR=$(pwd)
UNIT="$HOME/.config/systemd/user/youtube-dub.service"

if [ "$1" = "--remove" ]; then
  systemctl --user disable --now youtube-dub 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload
  echo "Đã gỡ chế độ tự chạy."
  exit 0
fi

mkdir -p "$(dirname "$UNIT")"
cat > "$UNIT" <<UNIT
[Unit]
Description=Web lồng tiếng Việt cho YouTube
After=network-online.target

[Service]
ExecStart=$DIR/run.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now youtube-dub
echo "Xong! Web luôn chạy tại http://localhost:5000"
