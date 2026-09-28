#!/usr/bin/env bash
set -Eeuo pipefail

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

for command_name in aws jq grep tr mktemp; do
    command -v "$command_name" >/dev/null 2>&1 || fail "Required command not found: $command_name"
done

AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
[[ -n "$AWS_REGION" ]] || fail "Set AWS_REGION before running this script."
[[ "$AWS_REGION" =~ ^[a-z0-9-]+$ ]] || fail "Invalid AWS_REGION: $AWS_REGION"
export AWS_DEFAULT_REGION="$AWS_REGION"

SNS_TOPIC_NAME="${DW_SNS_TOPIC_NAME:-driftwatch-alerts}"
LOG_GROUP_NAME="${DW_CW_LOG_GROUP:-/driftwatch/alerts}"
RETENTION_DAYS=7
METRIC_NAMESPACE="${DW_CW_METRIC_NAMESPACE:-DriftWatch}"
IAM_USER_NAME="${DW_IAM_USER_NAME:-driftwatch-publisher}"
IAM_POLICY_NAME="${DW_IAM_POLICY_NAME:-DriftWatchPublisherPolicy}"
ALERT_EMAIL="${DW_ALERT_EMAIL:-}"
ALERT_SMS="${DW_ALERT_SMS:-}"

[[ "$SNS_TOPIC_NAME" =~ ^[A-Za-z0-9_-]{1,256}$ ]] || fail "Invalid DW_SNS_TOPIC_NAME."
[[ "$LOG_GROUP_NAME" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail "Invalid DW_CW_LOG_GROUP."
[[ "$METRIC_NAMESPACE" =~ ^[A-Za-z0-9._/-]{1,255}$ ]] || fail "Invalid DW_CW_METRIC_NAMESPACE."
[[ "$METRIC_NAMESPACE" == "DriftWatch" ]] || fail "DW_CW_METRIC_NAMESPACE must be DriftWatch."
[[ "$IAM_USER_NAME" =~ ^[A-Za-z0-9+=,.@_-]{1,64}$ ]] || fail "Invalid DW_IAM_USER_NAME."
[[ "$IAM_POLICY_NAME" =~ ^[A-Za-z0-9+=,.@_-]{1,128}$ ]] || fail "Invalid DW_IAM_POLICY_NAME."
if [[ -n "$ALERT_EMAIL" && ! "$ALERT_EMAIL" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]]; then
    fail "DW_ALERT_EMAIL must be a valid email address."
fi
if [[ -n "$ALERT_SMS" && ! "$ALERT_SMS" =~ ^\+[1-9][0-9]{7,14}$ ]]; then
    fail "DW_ALERT_SMS must be an E.164 phone number, for example +14155550123."
fi

awsr() {
    aws --region "$AWS_REGION" "$@"
}

identity="$(awsr sts get-caller-identity --output json)"
account_id="$(jq -er '.Account' <<< "$identity")"
caller_arn="$(jq -er '.Arn' <<< "$identity")"
[[ "$account_id" =~ ^[0-9]{12}$ ]] || fail "Could not determine the AWS account ID."
partition="${caller_arn#arn:}"
partition="${partition%%:*}"

topic_arn="$(awsr sns create-topic --name "$SNS_TOPIC_NAME" --query TopicArn --output text)"

if ! awsr logs describe-log-groups \
    --log-group-name-prefix "$LOG_GROUP_NAME" \
    --query 'logGroups[].logGroupName' --output text |
    tr '\t' '\n' | grep -Fqx -- "$LOG_GROUP_NAME"; then
    awsr logs create-log-group --log-group-name "$LOG_GROUP_NAME" >/dev/null
fi
awsr logs put-retention-policy \
    --log-group-name "$LOG_GROUP_NAME" \
    --retention-in-days "$RETENTION_DAYS" >/dev/null

policy_file="$(mktemp)"
trap 'rm -f "$policy_file"' EXIT
cat > "$policy_file" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "sns:Publish",
      "Resource": "$topic_arn"
    },
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogStreams"
      ],
      "Resource": [
        "arn:$partition:logs:$AWS_REGION:$account_id:log-group:$LOG_GROUP_NAME",
        "arn:$partition:logs:$AWS_REGION:$account_id:log-group:$LOG_GROUP_NAME:log-stream:*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": "cloudwatch:PutMetricData",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "cloudwatch:namespace": "$METRIC_NAMESPACE"
        }
      }
    }
  ]
}
JSON

policy_arn="arn:$partition:iam::$account_id:policy/$IAM_POLICY_NAME"
expected_policy="$(jq -cS . "$policy_file")"
if awsr iam get-policy --policy-arn "$policy_arn" >/dev/null 2>&1; then
    default_version="$(awsr iam get-policy --policy-arn "$policy_arn" \
        --query 'Policy.DefaultVersionId' --output text)"
    current_policy="$(awsr iam get-policy-version --policy-arn "$policy_arn" \
        --version-id "$default_version" --query 'PolicyVersion.Document' --output json | jq -cS .)"
    if [[ "$current_policy" != "$expected_policy" ]]; then
        nondefault_versions="$(awsr iam list-policy-versions --policy-arn "$policy_arn" \
            --query 'Versions[?IsDefaultVersion==`false`].VersionId' --output text)"
        read -r -a version_ids <<< "$nondefault_versions"
        if (( ${#version_ids[@]} >= 4 )); then
            oldest_version="$(awsr iam list-policy-versions --policy-arn "$policy_arn" \
                --query 'Versions[?IsDefaultVersion==`false`] | sort_by(@, &CreateDate)[0].VersionId' \
                --output text)"
            [[ "$oldest_version" == "None" ]] ||
                awsr iam delete-policy-version --policy-arn "$policy_arn" \
                    --version-id "$oldest_version" >/dev/null
        fi
        awsr iam create-policy-version --policy-arn "$policy_arn" \
            --policy-document "file://$policy_file" --set-as-default >/dev/null
    fi
else
    awsr iam create-policy --policy-name "$IAM_POLICY_NAME" \
        --description 'DriftWatch alert publisher permissions' \
        --policy-document "file://$policy_file" >/dev/null
fi

if ! awsr iam get-user --user-name "$IAM_USER_NAME" >/dev/null 2>&1; then
    awsr iam create-user --user-name "$IAM_USER_NAME" \
        --tags Key=Application,Value=DriftWatch >/dev/null
fi
awsr iam attach-user-policy --user-name "$IAM_USER_NAME" \
    --policy-arn "$policy_arn" >/dev/null

subscribe_if_missing() {
    local protocol="$1"
    local endpoint="$2"
    local existing_endpoints

    existing_endpoints="$(awsr sns list-subscriptions-by-topic --topic-arn "$topic_arn" \
        --query "Subscriptions[?Protocol=='$protocol'].Endpoint" --output text)"
    if printf '%s\n' "$existing_endpoints" | tr '\t' '\n' | grep -Fqx -- "$endpoint"; then
        printf 'Existing %s subscription found.\n' "$protocol"
    else
        awsr sns subscribe --topic-arn "$topic_arn" --protocol "$protocol" \
            --notification-endpoint "$endpoint" >/dev/null
        printf 'Requested %s subscription; confirmation may be required.\n' "$protocol"
    fi
}

if [[ -n "$ALERT_EMAIL" ]]; then
    subscribe_if_missing email "$ALERT_EMAIL"
fi
if [[ -n "$ALERT_SMS" ]]; then
    subscribe_if_missing sms "$ALERT_SMS"
fi

printf '\nAWS setup complete in %s.\n' "$AWS_REGION"
printf 'SNS topic ARN: %s\n' "$topic_arn"
printf 'Log group: %s (retention: %s days)\n' "$LOG_GROUP_NAME" "$RETENTION_DAYS"
printf 'Dedicated IAM user: %s\n' "$IAM_USER_NAME"
printf 'Set DW_SNS_TOPIC_ARN to the topic ARN in your ignored local .env file.\n'
[[ -z "$ALERT_EMAIL" ]] || printf 'Confirm the SNS subscription from the email inbox.\n'