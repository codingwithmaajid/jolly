# EC2 user-data for the Jolly box (Amazon Linux 2023, arm64). Runs once, as root, on first boot.
# __NAME__ placeholders are filled in by the CDK stack; the files in host/ are
# shipped base64-encoded below so no quoting survives into the shell.
set -euxo pipefail
exec > >(tee -a /var/log/jolly-bootstrap.log) 2>&1

dnf install -y docker jq amazon-cloudwatch-agent
systemctl enable --now docker

mkdir -p /opt/jolly/data
cat > /opt/jolly/infra.env <<'ENV'
REGION=__REGION__
SECRET_ID=__SECRET_ID__
PARAM_PREFIX=__PARAM_PREFIX__
BACKUP_BUCKET=__BACKUP_BUCKET__
LOG_GROUP=__LOG_GROUP__
DOMAIN=__DOMAIN__
IMAGE=__IMAGE__
DATA_VOLUME_ID=__DATA_VOLUME_ID__
ENV
source /opt/jolly/infra.env

# Encrypted gp3 data volume for SQLite (survives instance replacement; RETAIN/SNAPSHOT in CDK).
DEV="/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${DATA_VOLUME_ID/-/}"
for _ in $(seq 1 60); do [ -e "$DEV" ] && break; sleep 5; done
if [ -e "$DEV" ]; then
  blkid "$DEV" || mkfs.xfs -L jolly-data "$DEV"
  UUID=$(blkid -s UUID -o value "$DEV")
  grep -q "$UUID" /etc/fstab || echo "UUID=$UUID /opt/jolly/data xfs defaults,nofail 0 2" >> /etc/fstab
  mount -a
else
  echo "WARN: data volume not attached; SQLite will live on the root volume" >&2
fi
chown 10001:10001 /opt/jolly/data   # uid of the non-root user in the image

echo '__B64_DEPLOY__'  | base64 -d > /opt/jolly/deploy.sh
echo '__B64_BACKUP__'  | base64 -d > /opt/jolly/backup.sh
echo '__B64_RESTORE__' | base64 -d > /opt/jolly/restore.sh
echo '__B64_CADDY__'   | base64 -d > /opt/jolly/Caddyfile
echo '__B64_SVC__'     | base64 -d > /etc/systemd/system/jolly-backup.service
echo '__B64_TIMER__'   | base64 -d > /etc/systemd/system/jolly-backup.timer
echo '__B64_CWA__'     | base64 -d > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json
chmod 700 /opt/jolly/*.sh

systemctl daemon-reload
systemctl enable --now jolly-backup.timer
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -s \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json || true

# First deploy. It fails harmlessly if the image or secrets aren't there yet;
# re-run later with the GitHub "Deploy to AWS" workflow or: sudo /opt/jolly/deploy.sh
/opt/jolly/deploy.sh || echo "first deploy did not complete; run /opt/jolly/deploy.sh once secrets and image exist"
