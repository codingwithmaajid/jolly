#!/bin/bash
# Online-safe SQLite backup (sqlite3 .backup API inside the container) to S3.
set -euo pipefail
source /opt/jolly/infra.env
STAMP=$(date -u +%Y-%m-%dT%H%MZ)
docker exec api python -c "import sqlite3; s=sqlite3.connect('/data/jolly.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d); d.close(); s.close()"
gzip -c /opt/jolly/data/backup.db > "/tmp/jolly-$STAMP.db.gz"
aws s3 cp --region "$REGION" --only-show-errors "/tmp/jolly-$STAMP.db.gz" "s3://$BACKUP_BUCKET/sqlite/jolly-$STAMP.db.gz"
rm -f "/tmp/jolly-$STAMP.db.gz" /opt/jolly/data/backup.db
echo "[backup] s3://$BACKUP_BUCKET/sqlite/jolly-$STAMP.db.gz"
