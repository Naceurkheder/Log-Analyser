#!/usr/bin/env bash
#
# scripts/verify-infra.sh — Acceptance check for the emulated-AWS plane.
#
# Confirms, from the HOST (cross-container, never only from inside `infra`):
#   - 4 buckets: incoming / raw-chunks / artifacts / results
#   - 3 queues with DLQs and redrive policy maxReceiveCount=3
#   - 3 tables: SentinelRuns / SentinelJobs / SentinelAlerts
#   - run_id GSIs on SentinelJobs and SentinelAlerts
#
# Exit code: 0 when every check passes, 1 otherwise.

set -uo pipefail

ENDPOINT="${AWS_ENDPOINT_URL:-http://10.20.0.10:4566}"
REGION="${AWS_REGION:-us-east-1}"
FAIL=0

awsx() {
    AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION="$REGION" \
        aws --endpoint-url "$ENDPOINT" --no-cli-pager "$@"
}

pass() { echo "  PASS  $*"; }
fail() { echo "  FAIL  $*"; FAIL=1; }

echo "== verifying emulated AWS at $ENDPOINT =="

# --- buckets ---
buckets="$(awsx s3api list-buckets --query 'Buckets[].Name' --output text 2>/dev/null || echo '')"
for b in incoming raw-chunks artifacts results; do
    if echo "$buckets" | tr '\t' '\n' | grep -qx "$b"; then
        pass "bucket '$b' exists"
    else
        fail "bucket '$b' missing"
    fi
done

# --- queues + redrive policy ---
for q in ingest-queue jobs-gpu jobs-cpu; do
    case "$q" in
        ingest-queue) dlq="ingest-dlq" ;;
        *)            dlq="$q-dlq" ;;
    esac
    url="$(awsx sqs get-queue-url --queue-name "$q" --query 'QueueUrl' --output text 2>/dev/null || true)"
    if [[ -z "$url" ]]; then
        fail "queue '$q' missing"
        continue
    fi
    pass "queue '$q' exists"

    dlq_url="$(awsx sqs get-queue-url --queue-name "$dlq" --query 'QueueUrl' --output text 2>/dev/null || true)"
    if [[ -z "$dlq_url" ]]; then
        fail "queue '$dlq' missing"
        continue
    fi
    pass "queue '$dlq' exists"

    dlq_arn="$(awsx sqs get-queue-attributes --queue-url "$dlq_url" \
        --attribute-names QueueArn --query 'Attributes.QueueArn' --output text 2>/dev/null || true)"
    redrive="$(awsx sqs get-queue-attributes --queue-url "$url" \
        --attribute-names RedrivePolicy --query 'Attributes.RedrivePolicy' --output text 2>/dev/null || true)"

    if [[ -n "$dlq_arn" ]] && echo "$redrive" | grep -q "$dlq_arn"; then
        pass "  $q -> $dlq redrive wired"
    else
        fail "  redrive policy missing or not pointing at $dlq (got: $redrive)"
    fi
    if echo "$redrive" | grep -Eq '"maxReceiveCount": *3'; then
        pass "  maxReceiveCount = 3"
    else
        fail "  maxReceiveCount != 3 (got: $redrive)"
    fi
done

# --- tables + indexes ---
tables_json="$(awsx dynamodb list-tables --query 'TableNames' --output text 2>/dev/null || echo '')"
for t in SentinelRuns SentinelJobs SentinelAlerts; do
    if ! echo "$tables_json" | tr '\t' '\n' | grep -qx "$t"; then
        fail "table '$t' missing"
        continue
    fi
    pass "table '$t' exists"

    if [[ "$t" == "SentinelRuns" ]]; then
        continue
    fi

    gsis="$(awsx dynamodb describe-table --table-name "$t" \
        --query 'Table.GlobalSecondaryIndexes[].IndexName' --output text 2>/dev/null || true)"
    if echo "$gsis" | tr '\t' '\n' | grep -qx "run_id-index"; then
        pass "  $t.run_id-index exists"
    else
        fail "  $t.run_id-index missing (got: $gsis)"
    fi
done

echo
if ((FAIL)); then
    echo "RESULT: FAILED"
    exit 1
fi
echo "RESULT: OK"
