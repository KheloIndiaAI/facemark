#!/bin/bash
# EC2 user-data: bring up the FaceMark backend on a fresh Amazon Linux 2023 box.
#
# Paste into the "User data" box when launching the instance, or pass with
#   aws ec2 run-instances --user-data file://deploy/aws/user-data.sh
#
# Runs once on first boot as root. Progress goes to /var/log/user-data.log.
#
# EDIT THESE TWO before launching.
REPO_URL="https://github.com/YOUR-USER/YOUR-REPO.git"
ADMIN_PASSWORD=""            # leave empty to have one generated into the logs

set -euxo pipefail
exec > >(tee /var/log/user-data.log) 2>&1

# --- packages ---------------------------------------------------------------
dnf update -y
dnf install -y docker git

# --- data volume ------------------------------------------------------------
# The attached EBS volume holds the SQLite database, enrolment photos and
# uploads. Everything on the root volume is replaced when the instance is
# rebuilt, so nothing durable may live there.
DEVICE=""
for d in /dev/nvme1n1 /dev/xvdf /dev/sdf; do
    [ -b "$d" ] && DEVICE="$d" && break
done

if [ -n "$DEVICE" ]; then
    # Only format when the volume is genuinely blank - reformatting a volume
    # that already holds the database would destroy every enrolled template.
    if ! blkid "$DEVICE"; then
        mkfs -t xfs "$DEVICE"
    fi
    mkdir -p /data
    grep -q "$DEVICE" /etc/fstab || echo "$DEVICE /data xfs defaults,nofail 0 2" >> /etc/fstab
    mount -a
else
    echo "WARNING: no data volume found. Falling back to the root volume, which"
    echo "         means the database is LOST on instance replacement."
    mkdir -p /data
fi
mkdir -p /data/uploads /data/students
chown -R 1000:1000 /data

# No GPU handling. The models total 37 MB and a group photo takes 0.6 s on a
# small CPU instance, so a GPU would cost far more and save nothing measurable.

systemctl enable --now docker

# --- build ------------------------------------------------------------------
# Both models are public and permissively licensed, so there is nothing to
# configure - the build fetches them itself.
cd /opt
git clone "$REPO_URL" facemark
cd facemark
docker build -t facemark:latest .

# --- run as a service -------------------------------------------------------
cat > /etc/systemd/system/facemark.service <<UNIT
[Unit]
Description=FaceMark attendance API
After=docker.service network-online.target
Requires=docker.service

[Service]
Restart=always
RestartSec=10
ExecStartPre=-/usr/bin/docker rm -f facemark
ExecStart=/usr/bin/docker run --rm --name facemark \\
    -p 80:8000 \\
    -v /data:/data \\
    -e FACEMARK_DATA_DIR=/data \\
    -e FACEMARK_MODELS_DIR=/app/data/models \\
    -e FACEMARK_ADMIN_PASSWORD=${ADMIN_PASSWORD} \\
    -e COOKIE_SECURE=1 \\
    -e DETECTION_MODE=fused \\
    facemark:latest
ExecStop=/usr/bin/docker stop facemark

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now facemark

# --- nightly database backup to S3 ------------------------------------------
# The SQLite file holds every enrolled face template. EBS is not backed up by
# default, and losing it means re-enrolling every athlete.
if [ -n "${BACKUP_BUCKET:-}" ]; then
    cat > /etc/cron.daily/facemark-backup <<'CRON'
#!/bin/bash
STAMP=$(date +%Y%m%d)
sqlite3 /data/attendance.db ".backup /tmp/attendance-$STAMP.db"
aws s3 cp "/tmp/attendance-$STAMP.db" "s3://$BACKUP_BUCKET/db/"
rm -f "/tmp/attendance-$STAMP.db"
CRON
    chmod +x /etc/cron.daily/facemark-backup
fi

echo "FaceMark is up on port 80. First-run admin password is in:"
echo "  docker logs facemark 2>&1 | grep 'FIRST RUN'"
