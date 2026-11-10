#!/usr/bin/env bash
# Nightly: copy the SQLite file out of the volume with SQLite's online backup, keep 30 days.
set -euo pipefail
dest=/root/compass-backups; mkdir -p "$dest"; chmod 700 "$dest"
docker compose -f /root/compass/deploy/docker-compose.yml exec -T web \
  python -c "import sqlite3; s=sqlite3.connect('/data/compass.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d); d.close()"
vol=$(docker volume inspect deploy_compass-data --format '{{ .Mountpoint }}')
cp "$vol/backup.db" "$dest/compass-$(date +%F).db"
chmod 600 "$dest"/compass-*.db
find "$dest" -name 'compass-*.db' -mtime +30 -delete
