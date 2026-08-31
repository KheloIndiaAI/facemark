# Start and stop the instance on a schedule

The GPU costing assumes the instance runs during centre hours only — roughly
220 hours a month against 730, which is where most of the saving comes from
($127/month instead of $423). Attendance happens at fixed training times, so a
GPU idling overnight buys nothing.

Two EventBridge Scheduler rules do it. Replace `i-0123456789abcdef0` with your
instance id, and pick the times your centre actually trains.

## Permissions

One role EventBridge can assume:

```bash
aws iam create-role --role-name FaceMarkScheduler \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow",
      "Principal":{"Service":"scheduler.amazonaws.com"},
      "Action":"sts:AssumeRole"}]}'

aws iam put-role-policy --role-name FaceMarkScheduler \
  --policy-name StartStopInstance \
  --policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow",
      "Action":["ec2:StartInstances","ec2:StopInstances"],
      "Resource":"arn:aws:ec2:ap-south-1:*:instance/i-0123456789abcdef0"}]}'
```

Scoping the resource to the one instance id matters — a wildcard here would let
anything that can reach this role stop every instance in the account.

## The two rules

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ROLE="arn:aws:iam::${ACCOUNT}:role/FaceMarkScheduler"

# Start 07:00 IST, weekdays
aws scheduler create-schedule --name facemark-start \
  --schedule-expression "cron(0 7 ? * MON-SAT *)" \
  --schedule-expression-timezone "Asia/Kolkata" \
  --flexible-time-window '{"Mode":"OFF"}' \
  --target "{
    \"Arn\":\"arn:aws:scheduler:::aws-sdk:ec2:startInstances\",
    \"RoleArn\":\"${ROLE}\",
    \"Input\":\"{\\\"InstanceIds\\\":[\\\"i-0123456789abcdef0\\\"]}\"}"

# Stop 19:00 IST, weekdays
aws scheduler create-schedule --name facemark-stop \
  --schedule-expression "cron(0 19 ? * MON-SAT *)" \
  --schedule-expression-timezone "Asia/Kolkata" \
  --flexible-time-window '{"Mode":"OFF"}' \
  --target "{
    \"Arn\":\"arn:aws:scheduler:::aws-sdk:ec2:stopInstances\",
    \"RoleArn\":\"${ROLE}\",
    \"Input\":\"{\\\"InstanceIds\\\":[\\\"i-0123456789abcdef0\\\"]}\"}"
```

`Asia/Kolkata` is set explicitly. EventBridge cron defaults to UTC, and a rule
that fires at 07:00 UTC starts the instance at 12:30 IST — after the morning
session it was meant to cover.

## What this costs you

The instance is **unreachable outside those hours**. Anyone opening the app at
20:00 gets a connection error, not a friendly message, because nothing is
running to serve one. Three ways to soften that, in increasing effort:

- Widen the window. Every extra hour is about $0.58 on `g4dn.xlarge`.
- Put the frontend on S3 + CloudFront (see `DEPLOYMENT.md`). The UI then still
  loads and can show a proper "outside centre hours" message when `/api` fails,
  rather than the browser's error page.
- Keep a `t3.micro` always on for the API and database, and start the GPU box
  only for inference. More moving parts than this project warrants.

## Verify before relying on it

```bash
aws scheduler get-schedule --name facemark-start
aws ec2 describe-instances --instance-ids i-0123456789abcdef0 \
  --query 'Reservations[].Instances[].State.Name' --output text
```

Watch the first real start and stop rather than assuming. A schedule that
silently fails to *stop* the instance costs $423/month instead of $127 — so set
a billing alarm as well (`DEPLOYMENT.md` has the command).
