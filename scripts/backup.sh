#!/bin/sh
# Run from anywhere. Uses online backup, then copies the complete snapshot off-volume.
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$task_root"
umask 077
mkdir -p backups
backup_name="bingo-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
docker compose exec -T app python manage.py backup "/app/backups/$backup_name"
docker compose cp "app:/app/backups/$backup_name" "backups/$backup_name"
chmod 600 "backups/$backup_name"
printf 'Backup ready: %s/backups/%s\n' "$task_root" "$backup_name"
