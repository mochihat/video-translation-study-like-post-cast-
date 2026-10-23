#!/usr/bin/env bash
# Cho phép web tự mở lại ổ NTFS bị rút ra giữa chừng (bị đánh dấu "dirty") mà không cần ntfsfix / Windows.
# Chạy một lần:  pkexec bash allow-ntfs-force.sh
#
# Chỉ thêm tuỳ chọn "force" vào danh sách tuỳ chọn mount udisks cho phép với driver ntfs3.
# Không chạm vào dữ liệu trên ổ nào. Gỡ bỏ: xoá /etc/udisks2/mount_options.conf (hoặc khôi phục bản .bak).
set -euo pipefail
CONF=/etc/udisks2/mount_options.conf
LINE='ntfs:ntfs3_allow=uid=$UID,gid=$GID,umask,dmask,fmask,iocharset,discard,nodiscard,sparse,nosparse,hidden,nohidden,sys_immutable,nosys_immutable,showmeta,noshowmeta,prealloc,noprealloc,hide_dot_files,nohide_dot_files,windows_names,nocase,case,force'

if [ "$(id -u)" != 0 ]; then
  echo "Hãy chạy bằng: pkexec bash $0" >&2
  exit 1
fi
if [ -f "$CONF" ] && grep -q '^ntfs:ntfs3_allow=.*force' "$CONF"; then
  echo "Đã cấu hình từ trước, không cần làm gì."
  exit 0
fi
if [ -f "$CONF" ]; then
  cp -a "$CONF" "$CONF.bak.$(date +%Y%m%d%H%M%S)"
  if grep -q '^ntfs:ntfs3_allow=' "$CONF"; then
    sed -i 's/^\(ntfs:ntfs3_allow=.*\)$/\1,force/' "$CONF"
  elif grep -q '^\[defaults\]' "$CONF"; then
    sed -i "/^\[defaults\]/a $LINE" "$CONF"
  else
    printf '\n[defaults]\n%s\n' "$LINE" >> "$CONF"
  fi
else
  printf '[defaults]\n%s\n' "$LINE" > "$CONF"
fi
echo "Xong: udisks giờ cho phép mở ổ NTFS 'dirty' bằng tuỳ chọn force ($CONF)."
