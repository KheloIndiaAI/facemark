# Deploying FaceMark

Everything on one EC2 instance — API, frontend and PostgreSQL in three
containers behind Caddy — with photos in S3. Sized for a 10–15 person proof of
concept in `ap-south-1`.

```
                    DNS (A record → Elastic IP)
                            │
                            ▼
   ┌──────────────────────────────────────────────────┐
   │  EC2  t4g.small  ·  Ubuntu 24.04  ·  compose     │
   │                                                  │
   │   caddy  :443 ──▶  app  :8000 ──▶  db  :5432     │
   │   TLS, ACME        FastAPI +        postgres:16  │
   │   upload cap       frontend         internal     │
   └──────────────────────┬───────────────────────────┘
                          │  IAM instance role
                          ▼
                    S3  ·  students/ + uploads/
```

About **$12–14/month**: $8.18 for the instance, ~$3 for a 30 GB gp3 volume,
under $1 for S3, and nothing for the VPC gateway endpoint or a bound Elastic IP.

Written against commit `827fc37` on `dev` — *Move persistence to PostgreSQL and
make photo storage switchable*. First deployed 1 Sep 2026.

---

## 0 · Naming and configuration

Every resource derives from two variables, so the whole thing stays consistent:

```bash
export APP=Attendence-application          # IAM, EC2, security group, tags
export APP_LC=attendence-application       # S3 — bucket names must be lowercase
```

`Attendence-application` is valid for IAM roles, security groups, key pairs and
tags. It is **not** valid as a PostgreSQL identifier without quoting, so the
database and its user are called `attendance` — a name that never leaves the
container.

### Environment variables

These are the names the code actually reads (`backend/config.py`, and
`.env.example` in the repo root). Anything not listed here is not a setting.

| Variable | Value for this deployment | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql://attendance:PASSWORD@db:5432/attendance` | **Required.** No SQLite fallback — the app refuses to start without it, deliberately. |
| `DB_POOL_MIN` / `DB_POOL_MAX` | `1` / `10` | Must stay under Postgres `max_connections` (set to 30 below). |
| `FACEMARK_STORAGE` | `s3` | Or `local` for photos on the EBS volume. |
| `S3_BUCKET` | `attendence-application-media-114171679953` | No `s3://` prefix. |
| `S3_REGION` | `ap-south-1` | **The shipped default is `auto`, a Cloudflare R2 convention that will not sign correctly against AWS S3. You must set this.** |
| `S3_ENDPOINT_URL` | *(empty)* | Only for R2 or MinIO. Empty means AWS. |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | *(empty)* | **Leave blank.** `backend/storage.py` passes `None` to boto3 when they are empty, so it falls back to the default credential chain and picks up the instance role. `.env.example` implies keys are required; on EC2 they are not, and not using them is better. |
| `S3_PREFIX` | *(empty)* | Optional, if one bucket serves several environments. |
| `FACEMARK_DATA_DIR` | `/app/appdata` | **Not `/app/data`.** See the box below. |
| `FACEMARK_ADMIN_PASSWORD` | generated | Only read when the users table is empty. |
| `COOKIE_SECURE` | `1` | |
| `COOKIE_SAMESITE` | `lax` | Correct for the single-hostname setup. |
| `CORS_ORIGINS` | *(unset)* | Not needed — API and frontend share a hostname. |
| `DETECTION_MODE` | `fused` | |

> **The `FACEMARK_DATA_DIR` line is load-bearing.** The Dockerfile bakes the two
> ONNX models into `/app/data/models` and sets `FACEMARK_MODELS_DIR` to point
> there. Mounting a host directory over `/app/data` would hide them and the app
> would fail at startup looking for YuNet. Because `FACEMARK_MODELS_DIR` is
> configured independently of `FACEMARK_DATA_DIR` — exactly as
> `backend/config.py:20-24` intends — moving the data root to `/app/appdata`
> leaves the models untouched. No code change needed.

`backend/config.py` also loads `<repo root>/.env` at import, and a real
environment variable always wins over a value in that file — so compose's
`env_file:` is authoritative.

### Storage layout

There are exactly **two prefixes**, `students/` and `uploads/`, validated on
every call in `backend/storage.py`. Within `uploads/` the filename prefix
distinguishes the three kinds of object, which is what makes differentiated
retention possible:

| Key | Written by | Keep |
|---|---|---|
| `students/student_*.jpg` | enrolment, multi-view capture | indefinitely |
| `uploads/group_*.jpg` | the original group photo | indefinitely — the evidentiary record behind every attendance row |
| `uploads/annotated_*.jpg` | the boxed output image | 90 days |
| `uploads/face_*.jpg` | per-face crops | 30 days |

Photos are **streamed through the application**, not redirected to a presigned
URL — `S3Storage.response()` reads the object and returns it, so the session
check gates every photograph of a minor. That is why `--workers 1` matters
below: each in-flight photo is held in memory.

> `_face_from_original()` reconstructs a group photo's key by string-parsing a
> crop filename (`face_<ts>_<idx>.jpg` → `group_<ts>.jpg`). **Do not rename
> objects in `uploads/`** — the "Who is this?" correction flow would break
> quietly, reporting the original as unavailable rather than erroring.

---

## 1 · Prerequisites

```bash
aws --version          # v2
```

You need a domain you can point at an IP. **TLS is not optional** — browsers
refuse `getUserMedia` outside a secure context, so on plain HTTP the camera
silently never starts and the app is unusable. If you have no domain to hand,
§4 shows the `sslip.io` route, which gives a real Let's Encrypt certificate.

### Preflight: confirm the account and the identity

The console and the CLI authenticate separately, and it is easy to be signed
into one account in the browser while the CLI points at another. Every command
below creates billable resources in whatever account the CLI holds.

Two things must be true:

1. **`Account` is `114171679953`** (`kheloindiaai`).
2. **`Arn` does not end in `:root`.** Root credentials cannot be scoped by
   policy, cannot be limited to one service, and cannot be revoked without
   affecting the whole account. Deploy as an IAM user, never as root.

```bash
aws configure --profile kheloindia     # IAM user access key, region ap-south-1
export AWS_PROFILE=kheloindia

export AWS_REGION=ap-south-1
export EXPECTED_ACCOUNT=114171679953

CALLER=$(aws sts get-caller-identity --output json)
ACCOUNT_ID=$(echo "$CALLER" | python3 -c 'import json,sys;print(json.load(sys.stdin)["Account"])')
CALLER_ARN=$(echo "$CALLER" | python3 -c 'import json,sys;print(json.load(sys.stdin)["Arn"])')

if [ "$ACCOUNT_ID" != "$EXPECTED_ACCOUNT" ]; then
  echo "STOP: CLI is on account $ACCOUNT_ID, expected $EXPECTED_ACCOUNT" >&2
elif case "$CALLER_ARN" in *:root) true;; *) false;; esac; then
  echo "STOP: authenticated as root - $CALLER_ARN" >&2
else
  export ACCOUNT_ID
  export BUCKET=${APP_LC}-media-${ACCOUNT_ID}
  echo "OK: $CALLER_ARN in $ACCOUNT_ID, bucket $BUCKET"
fi
```

Do not continue until this prints `OK:`. The guard is not decoration — `BUCKET`
derives from the account id, so on the wrong account you get a plausible-looking
bucket in the wrong place and no error at all.

Confirm the deploying identity can do what follows:

```bash
aws iam simulate-principal-policy \
  --policy-source-arn "$CALLER_ARN" \
  --action-names iam:CreateRole iam:CreateInstanceProfile iam:PutRolePolicy \
                 ec2:RunInstances ec2:CreateSecurityGroup ec2:AllocateAddress \
                 s3:CreateBucket ssm:GetParameter \
  --query 'EvaluationResults[].{action:EvalActionName,decision:EvalDecision}' \
  --output table
```

Every row must say `allowed`.

### Keep the variables

A new terminal loses all of this.

```bash
cat > ~/.attendence-env <<EOF
export AWS_PROFILE=kheloindia
export APP=${APP}
export APP_LC=${APP_LC}
export AWS_REGION=${AWS_REGION}
export ACCOUNT_ID=${ACCOUNT_ID}
export BUCKET=${BUCKET}
export DOMAIN=
EOF
# later:  source ~/.attendence-env
```

---

## 2 · S3 bucket

```bash
aws s3api create-bucket \
  --bucket "$BUCKET" --region "$AWS_REGION" \
  --create-bucket-configuration LocationConstraint="$AWS_REGION"

# Nothing here is ever public. Objects reach browsers only through the app,
# after it has checked the session.
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

aws s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

# Enrolment portraits are the anchor template for every future match; an
# overwrite is otherwise unrecoverable.
aws s3api put-bucket-versioning \
  --bucket "$BUCKET" --versioning-configuration Status=Enabled
```

### Retention

Filters match the full key prefix, which is how the three object kinds inside
`uploads/` get different lifetimes.

```bash
cat > /tmp/lifecycle.json <<'JSON'
{"Rules":[
  {"ID":"expire-face-crops","Status":"Enabled",
   "Filter":{"Prefix":"uploads/face_"},"Expiration":{"Days":30}},
  {"ID":"expire-annotated","Status":"Enabled",
   "Filter":{"Prefix":"uploads/annotated_"},"Expiration":{"Days":90}},
  {"ID":"expire-backups","Status":"Enabled",
   "Filter":{"Prefix":"backups/"},"Expiration":{"Days":30}},
  {"ID":"abort-incomplete-uploads","Status":"Enabled",
   "Filter":{"Prefix":""},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":7}}
]}
JSON

aws s3api put-bucket-lifecycle-configuration \
  --bucket "$BUCKET" --lifecycle-configuration file:///tmp/lifecycle.json

aws s3api get-bucket-location --bucket "$BUCKET"     # expect ap-south-1
```

`students/` and `uploads/group_` deliberately have no expiry rule. Setting one
is a decision about how long a child's photograph is kept, and belongs to
whoever owns that policy — not to a default.

---

## 3 · IAM role for the instance

This is what lets `S3_ACCESS_KEY_ID` stay empty, so no long-lived key ever
lands on the server.

```bash
cat > /tmp/trust.json <<'JSON'
{"Version":"2012-10-17","Statement":[{
  "Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},
  "Action":"sts:AssumeRole"}]}
JSON

aws iam create-role --role-name "${APP}-ec2" \
  --assume-role-policy-document file:///tmp/trust.json

cat > /tmp/s3policy.json <<JSON
{"Version":"2012-10-17","Statement":[
  {"Effect":"Allow",
   "Action":["s3:GetObject","s3:PutObject","s3:DeleteObject"],
   "Resource":"arn:aws:s3:::${BUCKET}/*"},
  {"Effect":"Allow",
   "Action":["s3:ListBucket","s3:GetBucketLocation"],
   "Resource":"arn:aws:s3:::${BUCKET}"}
]}
JSON

aws iam put-role-policy --role-name "${APP}-ec2" \
  --policy-name "${APP}-s3" --policy-document file:///tmp/s3policy.json

aws iam create-instance-profile --instance-profile-name "${APP}-ec2"
aws iam add-role-to-instance-profile \
  --instance-profile-name "${APP}-ec2" --role-name "${APP}-ec2"

aws iam get-instance-profile --instance-profile-name "${APP}-ec2" \
  --query 'InstanceProfile.Roles[].RoleName' --output text
```

`s3:ListBucket` is there on purpose: without it S3 returns 403 rather than 404
for a missing key, which would make a deleted face crop look like a permissions
failure.

---

## 4 · Key pair, security group, instance

```bash
aws ec2 create-key-pair --key-name "${APP}-key" \
  --query 'KeyMaterial' --output text > ~/.ssh/${APP}-key.pem
chmod 400 ~/.ssh/${APP}-key.pem

export VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text)

aws ec2 create-security-group --group-name "$APP" \
  --description "Attendence application POC" --vpc-id "$VPC_ID"
export SG=$(aws ec2 describe-security-groups --group-names "$APP" \
  --query 'SecurityGroups[0].GroupId' --output text)

MYIP=$(curl -s https://checkip.amazonaws.com)
aws ec2 authorize-security-group-ingress --group-id "$SG" \
  --protocol tcp --port 22 --cidr "${MYIP}/32"

# 80 is required for Caddy's ACME HTTP-01 challenge, not just for redirects.
aws ec2 authorize-security-group-ingress --group-id "$SG" \
  --protocol tcp --port 80 --cidr 0.0.0.0/0
aws ec2 authorize-security-group-ingress --group-id "$SG" \
  --protocol tcp --port 443 --cidr 0.0.0.0/0
```

Ubuntu 24.04 LTS, ARM64 — OpenCV, `psycopg[binary]` and every other wheel ship
aarch64 builds, and there is no torch to compile.

```bash
export AMI=$(aws ssm get-parameter \
  --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id \
  --query 'Parameter.Value' --output text)

# Fallback if that SSM path moves:
# export AMI=$(aws ec2 describe-images --owners 099720109477 \
#   --filters "Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*" \
#             "Name=state,Values=available" \
#   --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)

export IID=$(aws ec2 run-instances \
  --image-id "$AMI" --instance-type t4g.small \
  --key-name "${APP}-key" --security-group-ids "$SG" \
  --iam-instance-profile Name="${APP}-ec2" \
  --block-device-mappings \
    '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":30,"VolumeType":"gp3","DeleteOnTermination":false}}]' \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$APP}]" \
  --query 'Instances[0].InstanceId' --output text)

aws ec2 wait instance-running --instance-ids "$IID"
```

Two details that differ from an Amazon Linux launch: the root device is
`/dev/sda1`, not `/dev/xvda`, and the login user is `ubuntu`, not `ec2-user`.

`DeleteOnTermination=false` protects the database volume from an accidental
instance termination. The cost is that terminating leaves an orphaned volume
billing quietly — see §11.

If `run-instances` fails with `Invalid IAM Instance Profile name`, that is
eventual consistency, not a mistake. Wait ten seconds and rerun it unchanged.

### Address and S3 endpoint

```bash
export ALLOC=$(aws ec2 allocate-address --domain vpc --query AllocationId --output text)
aws ec2 associate-address --instance-id "$IID" --allocation-id "$ALLOC"
export EIP=$(aws ec2 describe-addresses --allocation-ids "$ALLOC" \
  --query 'Addresses[0].PublicIp' --output text)
echo "Public IP: $EIP"

export RTB=$(aws ec2 describe-route-tables \
  --filters Name=vpc-id,Values="$VPC_ID" Name=association.main,Values=true \
  --query 'RouteTables[0].RouteTableId' --output text)
aws ec2 create-vpc-endpoint --vpc-id "$VPC_ID" \
  --service-name "com.amazonaws.${AWS_REGION}.s3" --route-table-ids "$RTB" \
  --query 'VpcEndpoint.VpcEndpointId' --output text

cat >> ~/.attendence-env <<EOF
export VPC_ID=${VPC_ID}
export SG=${SG}
export IID=${IID}
export ALLOC=${ALLOC}
export EIP=${EIP}
EOF
```

The gateway endpoint is free and keeps photo traffic off the public internet.

### Domain

Point an `A` record at `$EIP`. With no domain to hand, `sslip.io` resolves any
`<ip>.sslip.io` to that IP and Let's Encrypt will issue for it:

```bash
export DOMAIN=${EIP//./-}.sslip.io        # e.g. 13-235-123-249.sslip.io
sed -i '' "s|^export DOMAIN=.*|export DOMAIN=${DOMAIN}|" ~/.attendence-env
dig +short "$DOMAIN"                       # must return $EIP
```

Good enough for a demo; swapping in a real domain later is one line in the
`Caddyfile` and a container restart.

---

## 5 · Prepare the instance

```bash
ssh-keygen -R "$EIP"        # only needed if reusing an EIP from a prior instance
ssh -i ~/.ssh/${APP}-key.pem ubuntu@$EIP
```

**Before installing anything, confirm the role attached.** If this is empty,
stop — every S3 call later fails with something that looks like a bucket
problem:

```bash
TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/iam/security-credentials/
# expect: Attendence-application-ec2
```

Docker from the official repository, which supplies `docker compose` as a
plugin — no manually downloaded binary:

```bash
sudo apt-get update && sudo apt-get upgrade -y
sudo apt-get install -y ca-certificates curl gnupg git postgresql-client-16

sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=arm64 signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin

sudo usermod -aG docker ubuntu
exit        # reconnect so the docker group applies
```

Back in. Postgres gets a **bind mount to a real path**, not an anonymous Docker
volume — a stray `docker system prune --volumes` must not be able to take the
biometric templates with it.

```bash
docker compose version

sudo mkdir -p /data/{postgres,appdata,caddy,caddy-config}
sudo chown -R ubuntu:ubuntu /data

sudo git clone -b dev https://github.com/KheloIndiaAI/facemark.git /opt/attendence-application
sudo chown -R ubuntu:ubuntu /opt/attendence-application
```

---

## 6 · Configuration files

All three are created on the server. **Do not copy a local `.env` up** — the
Postgres password belongs to a container that does not exist yet, and the S3
credentials should not exist at all.

> Use an editor (`nano`) for `docker-compose.yml`. A multi-line heredoc pasted
> into an interactive SSH session can be mangled by terminal redraw, and YAML
> fails in confusing ways when it is.

### `.env`

```bash
cd /opt/attendence-application

PGPASS=$(openssl rand -base64 24 | tr -d '/+=')
ADMINPASS=$(openssl rand -base64 18 | tr -d '/+=')

cat > .env <<EOF
# --- database ---------------------------------------------------------
POSTGRES_PASSWORD=${PGPASS}
DATABASE_URL=postgresql://attendance:${PGPASS}@db:5432/attendance
DB_POOL_MIN=1
DB_POOL_MAX=10

# --- photo storage ----------------------------------------------------
FACEMARK_STORAGE=s3
S3_BUCKET=attendence-application-media-114171679953
S3_REGION=ap-south-1
S3_ENDPOINT_URL=
S3_PREFIX=
# Left blank on purpose: boto3 falls back to the EC2 instance role.
S3_ACCESS_KEY_ID=
S3_SECRET_ACCESS_KEY=

# --- paths ------------------------------------------------------------
# NOT /app/data - that is where the Dockerfile bakes the ONNX models, and
# mounting a host directory over it would hide them. FACEMARK_MODELS_DIR is
# set separately in the image, so moving the data root leaves models alone.
FACEMARK_DATA_DIR=/app/appdata

# --- application ------------------------------------------------------
FACEMARK_ADMIN_PASSWORD=${ADMINPASS}
COOKIE_SECURE=1
COOKIE_SAMESITE=lax
DETECTION_MODE=fused
EOF

chmod 600 .env
echo "ADMIN PASSWORD: ${ADMINPASS}"      # write it down, then change it in-app
```

### `docker-compose.yml`

```yaml
services:
  db:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: attendance
      POSTGRES_USER: attendance
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}
    volumes:
      - /data/postgres:/var/lib/postgresql/data
    # Defaults assume Postgres owns the machine. It shares 2 GB with uvicorn
    # and two ONNX models. max_connections must exceed DB_POOL_MAX.
    command: >
      postgres -c shared_buffers=256MB -c max_connections=30 -c work_mem=8MB
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U attendance -d attendance"]
      interval: 10s
      timeout: 5s
      retries: 5
    # No ports: reachable only on the compose network.

  app:
    build: .
    restart: unless-stopped
    env_file: .env
    depends_on:
      db:
        condition: service_healthy
    volumes:
      - /data/appdata:/app/appdata
    expose:
      - "8000"
    # One worker: photos stream through the app and annotating a large image
    # allocates several full-size buffers. Overrides the Dockerfile's 2.
    command: >
      uvicorn backend.main:app --host 0.0.0.0 --port 8000
      --workers 1 --timeout-keep-alive 75

  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - /data/caddy:/data
      - /data/caddy-config:/config
    depends_on:
      - app
```

### `Caddyfile`

Replace the hostname with yours. Several names on one line each get their own
certificate, which is how you cut over to a real domain without breaking the
old URL — keep both for a day, then drop one.

> **After any edit to this file, `docker compose restart caddy`.** Editors and
> `sed -i` replace the file rather than writing in place, which breaks the
> single-file bind mount; the container keeps the old inode and `caddy reload`
> reports `config is unchanged` while doing nothing.

```
at.ccki.in, 13-235-123-249.sslip.io {
    encode gzip

    # The app has no upload limit of its own. A 24 MP JPEG decodes to ~72 MB
    # and utils.annotate() allocates several more full-size buffers; on a 2 GB
    # box shared with Postgres that is enough to invite the OOM killer.
    request_body {
        max_size 12MB
    }

    reverse_proxy app:8000 {
        transport http {
            read_timeout 300s
        }
    }
}
```

`.env`, `docker-compose.yml` and `Caddyfile` are untracked, so a later
`git pull` will not clobber them.

---

## 7 · Bring it up

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f app
```

First build takes two to three minutes, mostly the 37 MB of models. The startup
log should contain, in order:

```
db  Postgres pool open (min=1 max=10)
main  Seeded 8 PLACEHOLDER centres ...
main  ... FIRST RUN - super admin account created ...
detector  Detector ready: YuNet (face_detection_yunet_2023mar.onnx)
recognizer  Recognizer: face_recognition_sface_2021dec.onnx (Apache-2.0, 128-d)
storage  Photo storage backend: s3
```

The `setPreferableTarget Targets are not supported by the new graph engine`
warning from OpenCV is expected and harmless.

Confirm the compose file survived editing:

```bash
docker compose config --services        # expect: db, app, caddy
docker inspect $(docker compose ps -q app) --format '{{join .Config.Cmd " "}}'
# expect: ... --workers 1 --timeout-keep-alive 75
```

### Migrating an existing SQLite database

Only if you have real enrolments in a development `data/attendance.db`.

```bash
docker compose exec app python -m scripts.migrate_to_postgres --dry-run
docker compose exec app python -m scripts.migrate_to_postgres --photos
```

It preserves row ids so foreign keys stay valid, resets the `SERIAL` sequences
afterwards (skipping that produces a database that imports cleanly and then
fails on the first enrolment), and deliberately does not carry `auth_sessions`
over — everyone signs in again.

---

## 8 · Verify

### S3 round-trip

The backend reporting `s3` only means the setting was read. This proves the
instance role actually reaches the bucket, and isolates that from every other
possible failure:

```bash
docker compose exec app python -c "
from backend import storage
storage.put('uploads', 'smoke-test.txt', b'hello')
print('read back:', storage.get('uploads', 'smoke-test.txt'))
storage.delete('uploads', 'smoke-test.txt')
print('S3 round-trip OK')
"
```

A `None` from `get` means the role or the bucket name, not the app —
`storage.get()` swallows every exception and returns `None`, so a permissions
failure looks identical to a missing file.

### From your laptop

```bash
# 1. Health over TLS. Reports database and storage rather than 500ing.
curl -fsS https://$DOMAIN/api/health | python3 -m json.tool
# expect: "status":"ok", "database":"ok", "storage":"s3"

# 2. Media routes reject anonymous callers. All four gained auth in 827fc37 —
#    a 200 here means something regressed.
for p in /api/photos/x.jpg /api/uploads/x.jpg /api/photos/history /api/sample-images/x.jpg; do
  printf '%s -> ' "$p"
  curl -s -o /dev/null -w '%{http_code}\n' "https://$DOMAIN$p"
done
# expect: 401, 401, 401, 401

# 3. Session cookie is Secure and HttpOnly
curl -si -X POST https://$DOMAIN/api/auth/login \
  -F username=admin -F password="$ADMINPASS" | grep -i set-cookie
```

If the first gives a TLS error, Caddy is still doing the ACME exchange —
`docker compose logs caddy` shows it.

**4. Open `https://<domain>` on a phone and start the camera.** The check that
cannot be done from a terminal, and the one most likely to fail. If the camera
does not start, the certificate is the reason.

Then, signed in as `admin`:

- **Change the password immediately.** The generated one is in the container
  logs and in `.env`.
- **Centres → Remove demo** to clear the eight `DEMO-` placeholders. They are
  not real Khelo India records.
- **Centres → Import data** for the real ones.
- Enrol someone using **Capture views**, not a single photo.
- Take a group photo and confirm matches appear.

```bash
aws s3 ls "s3://${BUCKET}/students/" | head
aws s3 ls "s3://${BUCKET}/uploads/"  | head

docker compose exec db psql -U attendance -d attendance \
  -c "SELECT count(*) FROM students;" \
  -c "SELECT count(*) FROM templates;" \
  -c "SELECT count(*) FROM attendance;"
```

### 5 · The migration gate

Templates are float32 vectors in a `BYTEA` column. A dtype or byte-order slip
produces embeddings that load cleanly, have the right shape, and match nobody —
a failure that reads as "recognition got worse" rather than "the migration is
broken."

```bash
docker compose exec app python -m scripts.evaluate --sweep
```

**Compare d′ and the per-photo match counts against a pre-migration run. If they
differ, the migration is wrong.**

---

## 9 · Backups

There are no RDS snapshots here — this is yours now.

```bash
sudo tee /opt/attendence-application/backup.sh >/dev/null <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cd /opt/attendence-application
source .env
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
docker compose exec -T db pg_dump -U attendance -d attendance -Fc \
  | aws s3 cp - "s3://${S3_BUCKET}/backups/attendance-${STAMP}.dump" --sse AES256
EOF

sudo chmod +x /opt/attendence-application/backup.sh
sudo chown ubuntu:ubuntu /opt/attendence-application/backup.sh

( crontab -l 2>/dev/null; echo "15 2 * * * /opt/attendence-application/backup.sh >> /var/log/attendance-backup.log 2>&1" ) | crontab -
/opt/attendence-application/backup.sh
aws s3 ls "s3://${BUCKET}/backups/"
```

`aws` is not installed on the instance by default — `sudo snap install aws-cli
--classic` or `sudo apt-get install -y awscli` before the first run. It needs no
credentials; the instance role covers it.

### Restore drill — do this once, now

An untested backup is not a backup, and this repository has already shipped one
of those.

```bash
LATEST=$(aws s3 ls "s3://${BUCKET}/backups/" | sort | tail -1 | awk '{print $4}')

docker compose exec db psql -U attendance -d postgres -c "CREATE DATABASE restore_test;"
aws s3 cp "s3://${BUCKET}/backups/${LATEST}" - \
  | docker compose exec -T db pg_restore -U attendance -d restore_test
docker compose exec db psql -U attendance -d restore_test -c "SELECT count(*) FROM templates;"
docker compose exec db psql -U attendance -d postgres -c "DROP DATABASE restore_test;"
```

Optionally add a daily EBS snapshot through **Data Lifecycle Manager** in the
console — the wider net, covering Caddy's certificates too.

---

## 10 · Operations

```bash
cd /opt/attendence-application
docker compose logs -f app
docker compose restart app
docker compose exec db psql -U attendance -d attendance
docker stats --no-stream
```

**Deploying an update.** The model layer is cached, so only the code layer
rebuilds:

```bash
cd /opt/attendence-application && git pull
docker compose build app && docker compose up -d app
curl -fsS https://$DOMAIN/api/health
```

**Switching storage backend.** Keys are bare filenames under one of two
prefixes, and `Path(value).name` is applied on read, so both old absolute paths
and new bare names resolve. Switching is a file copy, not a schema change:

```bash
aws s3 sync /data/appdata/students "s3://${BUCKET}/students/"
aws s3 sync /data/appdata/uploads  "s3://${BUCKET}/uploads/"
sed -i 's/^FACEMARK_STORAGE=.*/FACEMARK_STORAGE=s3/' .env
docker compose up -d app
```

### Continuous integration and deployment

`.github/workflows/ci.yml` runs on every push: ruff (runtime errors only),
an import check over every `backend/` module, and a Docker build whose container
is started against a real Postgres and asserted on — health reports
`"database":"ok"`, the models load, and all four media routes return 401.

`.github/workflows/deploy.yml` deploys on push to `dev` or manual dispatch, via
**SSM Run Command rather than SSH** — the security group allows port 22 from one
address, and opening it to GitHub's runner ranges to deploy would be a poor
trade. Credentials come from GitHub's OIDC provider, so no AWS key exists in the
repository.

Three things about that setup are non-obvious and each will waste an afternoon
if forgotten:

- **The OIDC subject carries numeric IDs.** This organisation has subject
  customization enabled, so the claim is
  `repo:KheloIndiaAI@318880338/facemark@1352332885:environment:production`,
  *not* `repo:KheloIndiaAI/facemark:environment:production`. The role's trust
  policy matches the full string exactly. The IDs are immutable, which is the
  point — a renamed or recreated repository cannot assume the role. But remove
  `environment: production` from the job, or recreate the repo, and deploys fail
  with an opaque `Not authorized to perform sts:AssumeRoleWithWebIdentity`.
  CloudTrail (`AssumeRoleWithWebIdentity`, `userIdentity.userName`) is the only
  place the real subject is visible.
- **SSM runs commands as root**, and `/opt/attendence-application` is owned by
  `ubuntu`, so git refuses with "dubious ownership" unless the instance has
  `sudo git config --system --add safe.directory /opt/attendence-application`.
- **The instance must show `Online`** in `aws ssm describe-instance-information`
  before a deploy can reach it.
- **`AWS-RunShellScript` runs under `/bin/sh`, which is dash on Ubuntu.**
  `set -o pipefail` is a bash-ism and aborts the whole script with "Illegal
  option -o pipefail" before the first command. The deploy uses `set -eu`.

**Replacing the instance.** The root volume has `DeleteOnTermination=false`, so
terminating leaves an orphaned 30 GB volume billing quietly. Capture the id
first, and delete it only once you are sure the data is elsewhere:

```bash
export VOL=$(aws ec2 describe-instances --instance-ids "$IID" \
  --query 'Reservations[0].Instances[0].BlockDeviceMappings[0].Ebs.VolumeId' --output text)
aws ec2 terminate-instances --instance-ids "$IID"
aws ec2 wait instance-terminated --instance-ids "$IID"
aws ec2 delete-volume --volume-id "$VOL"
```

A new instance on the same Elastic IP presents a different host key, so
`ssh-keygen -R "$EIP"` before reconnecting.

---

## 11 · Troubleshooting

| Symptom | Cause |
|---|---|
| **Camera never starts, no error** | Not a secure context. `getUserMedia` is unavailable over HTTP or with an invalid certificate. |
| **Login succeeds, every later request 401s** | `COOKIE_SECURE=1` without working TLS. Fix TLS; do not unset the flag. |
| **App exits at startup, `DATABASE_URL is not set`** | Working as designed — no SQLite fallback. Check the variable reached the container: `docker compose exec app env \| grep DATABASE_URL`. |
| **Startup fails looking for YuNet** | `/app/data` was mounted over, hiding the baked-in models. Set `FACEMARK_DATA_DIR=/app/appdata` and mount there instead. |
| **`SignatureDoesNotMatch` from S3** | `S3_REGION` left at its `auto` default. Set `ap-south-1`. |
| **All photos 404, uploads appear to succeed** | Instance role missing, or wrong `S3_BUCKET`. Run the §8 round-trip; check `docker compose logs app \| grep "S3 get miss"`. |
| **`Invalid IAM Instance Profile name` on launch** | Eventual consistency. Wait ten seconds, rerun unchanged. |
| **Caddyfile edit has no effect; reload logs `config is unchanged`** | `sed -i` and most editors replace the file rather than writing in place, which creates a new inode and breaks the single-file bind mount — the container still holds the old one. Always `docker compose restart caddy` after editing; `caddy reload` cannot see the change. |
| **`tlsv1 alert internal error` on a new hostname** | Caddy has no certificate for that SNI, because the config carrying the name never loaded. Check `enabling automatic TLS certificate management` in the logs actually lists it. |
| **`FATAL: sorry, too many clients already`** | `DB_POOL_MAX × workers` exceeded Postgres `max_connections`. |
| **Container killed, no stack trace** | OOM while annotating a large photo. `dmesg -T \| grep -i oom`. Confirm the Caddy `max_size` cap is present. |
| **SSH host key mismatch** | Reused Elastic IP on a replaced instance. `ssh-keygen -R "$EIP"`. |
| **Centre search finds nothing for lowercase input** | Known, still open — see §12. |
| **"Who is this?" says the original is unavailable** | An object in `uploads/` was renamed, breaking the `face_<ts>_<idx>` → `group_<ts>` relationship. |

---

## 12 · Known open items

Fixed in `827fc37`: authentication on all four media routes, centre scoping on
`remove_student` / `add_student_photo` / `student_history`, the
`sqlite3.IntegrityError` catch, connection pooling, and `/api/health` no longer
returning 500 when the database is unreachable.

Still outstanding:

- **`init_db()` is not concurrency-safe, and the Dockerfile ships
  `--workers 2`.** `CREATE TABLE IF NOT EXISTS` does not serialise in
  PostgreSQL: two workers both find a table absent, both try to create it, and
  the loser raises `UniqueViolation` on `pg_type` instead of becoming a no-op.
  Uvicorn then kills the parent, so **the image as built crashes on first boot
  against an empty database.** This deployment survives only because the compose
  file overrides the worker count to 1. CI reproduces it on every run. The fix
  is a transaction-scoped advisory lock at the top of `init_db()`
  (`backend/database.py:120`), which makes it correct at any worker count:
  `conn.execute("SELECT pg_advisory_xact_lock(?)", (2749170101,))`.
- **`LIKE` was not changed to `ILIKE`** (`backend/centres.py:125-126, 133`).
  SQLite's `LIKE` is case-insensitive for ASCII; PostgreSQL's is not. Centre
  search now misses lowercase queries — "pune" returns nothing.
- **The confidence histogram is still shifted.** `backend/database.py:659` fixed
  the Decimal-vs-float return type but kept `CAST(x AS INT)`, which **rounds**
  in PostgreSQL where SQLite **truncated**. Needs `FLOOR(a.confidence * 20)`.
- **`scipy` is absent from `requirements.txt`** while
  `backend/metaheuristics.py:178` imports `linear_sum_assignment` from it. The
  fallback at `:186` is a greedy sort, not the Hungarian algorithm, so every
  container runs a different assignment solver than the benchmarks measured —
  silently.
- **The Dockerfile still specifies `--workers 2`**, runs as root, floats on
  `python:3.11-slim`, and copies `scripts/` into the image. The compose
  `command:` overrides the worker count; the rest stands.
- **No upload size limit in the application.** Caddy caps it at the proxy, which
  is the mitigation available without a code change.
- **`vercel.json` still points `/api/*` at a dead Cloudflare quick-tunnel
  hostname.** Unused here, but it should not stay in the repo.

`deploy/aws/user-data.sh` and `deploy/aws/schedule.md` describe the abandoned
architecture — the former leaks the admin password four ways and its S3 backup
block is gated on a variable that is never set. Delete both.

---

## Handling biometric data

This system stores face embeddings and photographs of children. A face template
is not revocable the way a password is. Before any real deployment: obtain
informed consent from guardians, publish a retention period and enforce it
through the lifecycle rules in §2, restrict who holds super-admin accounts, and
check your obligations under the DPDP Act 2023. Nothing in this runbook
discharges those duties.
