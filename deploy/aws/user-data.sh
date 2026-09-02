#!/bin/bash
# EC2 user-data: bring up the FaceMark backend on a fresh Amazon Linux 2023 box.
#
# Paste into the "User data" box when launching the instance, or pass with
#   aws ec2 run-instances --user-data file://deploy/aws/user-data.sh
#
# Runs once on first boot as root. Progress goes to /var/log/user-data.log.
#
#
# BEFORE YOU LAUNCH
# -----------------
# 1. Edit REPO_URL and AWS_REGION below. Those are the only two values in this
#    file, and neither is a secret.
#
# 2. Put the secrets in SSM Parameter Store as SecureString, NOT in this file:
#
#      aws ssm put-parameter --type SecureString --name /facemark/database_url \
#          --value 'postgresql://USER:PASSWORD@HOST:5432/facemark?sslmode=require'
#      aws ssm put-parameter --type SecureString --name /facemark/admin_password \
#          --value 'a-long-random-string'
#
#    They are deliberately not here because user-data is NOT a private channel.
#    Anything written below is readable from the instance metadata service by
#    every process on the box (and by anything with an SSRF against it), is
#    shown in the EC2 console, is returned by ec2:DescribeInstanceAttribute, and
#    with `set -x` would be echoed into /var/log/user-data.log as well. A
#    database URL contains the database password, so putting it here publishes
#    the database.
#
# 3. Attach an instance role granting, scoped to this app's own resources:
#      ssm:GetParameter        on arn:aws:ssm:REGION:ACCOUNT:parameter/facemark/*
#      kms:Decrypt             on the key those parameters use
#      s3:GetObject/PutObject  on the photo bucket, if FACEMARK_STORAGE=s3
#      s3:PutObject            on the backup bucket, if BACKUP_BUCKET is set
#
#    The role is also why there are no S3 access keys anywhere in this file:
#    boto3 picks up credentials from the instance role automatically, so
#    S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY must be left EMPTY.

REPO_URL="https://github.com/YOUR-USER/YOUR-REPO.git"
AWS_REGION="ap-south-1"

# Optional. Where the nightly database dump goes. Leave empty to skip backups.
BACKUP_BUCKET=""

# Optional. Photo storage: "local" keeps them on the data volume, "s3" puts
# them in a bucket (set S3_BUCKET too, and leave the access keys empty so the
# instance role is used).
FACEMARK_STORAGE="local"
S3_BUCKET=""

# -x is deliberately NOT set: this script reads secrets, and tracing would copy
# them into the log. -e -u -o pipefail still apply.
set -euo pipefail
exec > >(tee /var/log/user-data.log) 2>&1

# --- packages ---------------------------------------------------------------
dnf update -y
dnf install -y docker git awscli postgresql15

# --- data volume ------------------------------------------------------------
# Holds enrolment photos and uploads. The DATABASE now lives in Postgres (RDS
# or another managed host), not on this volume - but the photos are still
# irreplaceable, and everything on the root volume is replaced when the
# instance is rebuilt, so nothing durable may live there.
DEVICE=""
for d in /dev/nvme1n1 /dev/xvdf /dev/sdf; do
    [ -b "$d" ] && DEVICE="$d" && break
done

if [ -n "$DEVICE" ]; then
    # Only format when the volume is genuinely blank - reformatting a volume
    # that already holds the photos would destroy every enrolment portrait.
    if ! blkid "$DEVICE"; then
        mkfs -t xfs "$DEVICE"
    fi
    mkdir -p /data
    grep -q "$DEVICE" /etc/fstab || echo "$DEVICE /data xfs defaults,nofail 0 2" >> /etc/fstab
    mount -a
else
    echo "WARNING: no data volume found. Falling back to the root volume, which"
    echo "         means enrolment photos are LOST on instance replacement."
    mkdir -p /data
fi
mkdir -p /data/uploads /data/students
chown -R 1000:1000 /data

# No GPU handling. The models total 37 MB and a group photo takes 0.6 s on a
# small CPU instance, so a GPU would cost far more and save nothing measurable.

systemctl enable --now docker

# --- secrets ----------------------------------------------------------------
# Fetched at boot from SSM into a root-only file. They are passed to the
# container with --env-file rather than -e: the -e form puts the value in the
# systemd unit (world-readable at /etc/systemd/system/) and in the output of
# `docker inspect` and `ps` for anyone on the box.
DATABASE_URL="$(aws ssm get-parameter --region "$AWS_REGION" \
    --name /facemark/database_url --with-decryption \
    --query Parameter.Value --output text)"

# Optional: only read on the very first run, when the users table is empty.
ADMIN_PASSWORD="$(aws ssm get-parameter --region "$AWS_REGION" \
    --name /facemark/admin_password --with-decryption \
    --query Parameter.Value --output text 2>/dev/null || echo "")"

if [ -z "$DATABASE_URL" ]; then
    echo "FATAL: /facemark/database_url is empty or unreadable."
    echo "       The app is Postgres-only and will not start without it."
    echo "       Check the parameter exists and the instance role allows"
    echo "       ssm:GetParameter and kms:Decrypt."
    exit 1
fi

install -m 600 /dev/null /etc/facemark.env
cat > /etc/facemark.env <<ENVFILE
DATABASE_URL=${DATABASE_URL}
FACEMARK_ADMIN_PASSWORD=${ADMIN_PASSWORD}
FACEMARK_DATA_DIR=/data
FACEMARK_MODELS_DIR=/app/data/models
FACEMARK_STORAGE=${FACEMARK_STORAGE}
S3_BUCKET=${S3_BUCKET}
S3_REGION=${AWS_REGION}
S3_ACCESS_KEY_ID=
S3_SECRET_ACCESS_KEY=
COOKIE_SECURE=1
DETECTION_MODE=fused
ENVFILE

# --- build ------------------------------------------------------------------
# Both models are public and permissively licensed, so there is nothing to
# configure - the build fetches them itself.
cd /opt
git clone "$REPO_URL" facemark
cd facemark
docker build -t facemark:latest .

# --- run as a service -------------------------------------------------------
cat > /etc/systemd/system/facemark.service <<'UNIT'
[Unit]
Description=FaceMark attendance API
After=docker.service network-online.target
Requires=docker.service

[Service]
Restart=always
RestartSec=10
ExecStartPre=-/usr/bin/docker rm -f facemark
ExecStart=/usr/bin/docker run --rm --name facemark \
    -p 80:8000 \
    -v /data:/data \
    --env-file /etc/facemark.env \
    facemark:latest
ExecStop=/usr/bin/docker stop facemark

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now facemark

# --- nightly database backup to S3 ------------------------------------------
# The database holds every enrolled face template. If it is managed RDS with
# automated backups this is redundant - but a plain Postgres host has no
# backups by default, and losing it means re-enrolling every athlete.
#
# The photos on /data are NOT covered here; snapshot the EBS volume for those.
if [ -n "$BACKUP_BUCKET" ]; then
    cat > /etc/cron.daily/facemark-backup <<CRON
#!/bin/bash
# Reads the URL from the root-only env file rather than embedding it here.
set -euo pipefail
DB="\$(grep '^DATABASE_URL=' /etc/facemark.env | cut -d= -f2-)"
STAMP=\$(date +%Y%m%d)
OUT="/tmp/facemark-\$STAMP.dump"
trap 'rm -f "\$OUT"' EXIT
pg_dump --format=custom --dbname="\$DB" --file="\$OUT"
aws s3 cp "\$OUT" "s3://$BACKUP_BUCKET/db/"
CRON
    chmod 700 /etc/cron.daily/facemark-backup
fi

echo "FaceMark is up on port 80."
echo "If this was the first run, the admin password is the SSM value at"
echo "  /facemark/admin_password"
echo "or, if that was unset, generated into:"
echo "  docker logs facemark 2>&1 | grep 'FIRST RUN'"
