# AWS Alert Setup

This provisions the DriftWatch SNS topic, CloudWatch Logs group, and a dedicated IAM publisher user with a scoped managed policy. Run it from a trusted publisher machine; it does not create or print access keys.

## Prerequisites

- AWS CLI v2, `jq`, and Bash (Linux/macOS, WSL, or Git Bash).
- An AWS CLI profile/credential chain with permission to create SNS topics and subscriptions, CloudWatch log groups and retention policies, IAM users and customer-managed policies, and to call `sts:GetCallerIdentity`.
- An email address you can access to confirm the SNS subscription. SMS is optional and may incur charges.
- The account and region where DriftWatch will publish. Keep `AWS_REGION` consistent with the application's local `.env`.

Never put access keys in this repository or share them in chat. Use the AWS CLI credential chain (`aws configure`, IAM Identity Center, or an instance/assumed role). The setup identity needs provisioning permissions; the created publisher identity gets only the policy described below.

## Provision

Set the region and subscription endpoints in your shell. The resource names shown are defaults and can be overridden with the listed environment variables.

```bash
export AWS_REGION=ap-south-1
export DW_ALERT_EMAIL=you@example.com
# Optional; E.164 format, for example +14155550123. SMS delivery can cost extra.
# export DW_ALERT_SMS=+14155550123
# Optional overrides:
# export DW_SNS_TOPIC_NAME=driftwatch-alerts
# export DW_CW_LOG_GROUP=/driftwatch/alerts
# export DW_IAM_USER_NAME=driftwatch-publisher
# export DW_IAM_POLICY_NAME=DriftWatchPublisherPolicy
# export DW_CW_METRIC_NAMESPACE=DriftWatch

bash infra/setup.sh
```

The script can be rerun. It reuses the topic, group, user, and policy; enforces log retention; updates the policy only if its contents change; and checks for existing email/SMS subscriptions before requesting new ones. SNS email subscriptions are pending until confirmed. Open the confirmation email and follow its link.

Copy the printed topic ARN into your ignored local `.env` file:

```dotenv
AWS_REGION=ap-south-1
DW_SNS_TOPIC_ARN=arn:aws:sns:ap-south-1:123456789012:driftwatch-alerts
DW_CW_LOG_GROUP=/driftwatch/alerts
DW_CW_METRIC_NAMESPACE=DriftWatch
```

Replace the example ARN with the value printed by the script. Do not commit `.env` or paste the ARN into a public issue; the ARN is not an access key, but it identifies your account and resource.

The script creates IAM user `driftwatch-publisher` (by default) but intentionally does not generate an access key. For a local smoke test, create a programmatic credential for that dedicated user in the AWS Console, store it in a named AWS CLI profile using `aws configure --profile driftwatch-publisher`, and then select that profile:

```bash
export AWS_PROFILE=driftwatch-publisher
```

Prefer an IAM role/short-lived credentials when the publisher machine supports them. Do not attach unrelated permissions to the dedicated publisher user.

## Verify

Run these commands with the publisher profile selected and the email subscription confirmed. They send a real test email and create one test log event and custom metric.

```bash
aws --region "$AWS_REGION" sns publish \
  --topic-arn "$DW_SNS_TOPIC_ARN" \
  --subject 'DriftWatch setup smoke test' \
  --message 'DriftWatch SNS delivery is working.'
```

Confirm the email arrives. Then create a unique log stream and write one event:

```bash
LOG_STREAM="setup-smoke-$(date +%s)"
TIMESTAMP_MS="$(($(date +%s) * 1000))"
aws --region "$AWS_REGION" logs create-log-stream \
  --log-group-name "$DW_CW_LOG_GROUP" --log-stream-name "$LOG_STREAM"
aws --region "$AWS_REGION" logs put-log-events \
  --log-group-name "$DW_CW_LOG_GROUP" --log-stream-name "$LOG_STREAM" \
  --log-events "timestamp=$TIMESTAMP_MS,message=DriftWatch setup smoke test"
aws --region "$AWS_REGION" logs filter-log-events \
  --log-group-name "$DW_CW_LOG_GROUP" --log-stream-names "$LOG_STREAM" \
  --filter-pattern 'DriftWatch setup smoke test' \
  --query 'events[].message' --output text
```

Publish a metric under the required namespace:

```bash
aws --region "$AWS_REGION" cloudwatch put-metric-data \
  --namespace DriftWatch \
  --metric-data "MetricName=SetupSmokeTest,Value=1,Unit=Count,Timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

Verify the log event in CloudWatch Logs and `SetupSmokeTest` under CloudWatch Metrics → All metrics → DriftWatch. Custom metrics can take several minutes to appear. If delivery or writes fail, check the selected AWS profile, region, email confirmation, and attached publisher policy. Before posting screenshots to the issue, blur account IDs, email addresses, and other identifying details.

## Tear Down

Use the same region and resource-name overrides as provisioning. First remove all access keys for the dedicated publisher user (if any), then detach its policy, delete non-default policy versions, and remove the dedicated IAM resources:

```bash
export AWS_REGION=ap-south-1
export AWS_DEFAULT_REGION="$AWS_REGION"
DW_IAM_USER_NAME="${DW_IAM_USER_NAME:-driftwatch-publisher}"
DW_IAM_POLICY_NAME="${DW_IAM_POLICY_NAME:-DriftWatchPublisherPolicy}"
ACCOUNT_ID="$(aws --region "$AWS_REGION" sts get-caller-identity --query Account --output text)"
POLICY_ARN="arn:aws:iam::$ACCOUNT_ID:policy/$DW_IAM_POLICY_NAME"
: "${DW_SNS_TOPIC_ARN:?Set DW_SNS_TOPIC_ARN to the topic ARN before teardown}"

for key_id in $(aws iam list-access-keys --user-name "$DW_IAM_USER_NAME" \
  --query 'AccessKeyMetadata[].AccessKeyId' --output text); do
  aws iam delete-access-key --user-name "$DW_IAM_USER_NAME" --access-key-id "$key_id"
done
aws iam detach-user-policy --user-name "$DW_IAM_USER_NAME" --policy-arn "$POLICY_ARN"
for version_id in $(aws iam list-policy-versions --policy-arn "$POLICY_ARN" \
  --query 'Versions[?IsDefaultVersion==`false`].VersionId' --output text); do
  aws iam delete-policy-version --policy-arn "$POLICY_ARN" --version-id "$version_id"
done
aws iam delete-user --user-name "$DW_IAM_USER_NAME"
aws iam delete-policy --policy-arn "$POLICY_ARN"
aws --region "$AWS_REGION" logs delete-log-group \
  --log-group-name "${DW_CW_LOG_GROUP:-/driftwatch/alerts}"
aws --region "$AWS_REGION" sns delete-topic \
  --topic-arn "$DW_SNS_TOPIC_ARN"
```

Deleting the log group permanently removes its stored events. Deleting the topic removes its subscriptions. Review the names and account before running teardown commands.