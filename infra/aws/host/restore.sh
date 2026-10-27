#!/bin/bash
# Restore a backup: sudo /opt/jolly/restore.sh sqlite/jolly-2026-10-28T0300Z.db.gz
# (list them with: aws s3 ls s3://$BACKUP_BUCKET/sqlite/)
set -euo pipefail
source /opt/jolly/infra.env
KEY="${1:?usage: restore.sh <s3-key>}"
aws s3 cp --region "$REGION" --only-show-errors "s3://$BACKUP_BUCKET/$KEY" /tmp/restore.db.gz
docker stop api
cp -a /opt/jolly/data/jolly.db "/opt/jolly/data/jolly.db.pre-restore.$(date -u +%s)" 2>/dev/null || true
gunzip -c /tmp/restore.db.gz > /opt/jolly/data/jolly.db
chown 10001:10001 /opt/jolly/data/jolly.db
rm -f /tmp/restore.db.gz /opt/jolly/data/jolly.db-wal /opt/jolly/data/jolly.db-shm
docker start api
echo "[restore] restored $KEY"
