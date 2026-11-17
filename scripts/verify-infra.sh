#!/usr/bin/env bash
#
# scripts/verify-infra.sh — Acceptance check for the emulated-AWS plane.
#
# Confirms, from the HOST (cross-container, never only from inside `infra`):
#   - 3 buckets: raw / artifacts / results
#   - 2 queues: jobs / jobs-dlq, with redrive policy maxReceiveCount=3
#   - 2 tables: jobs / alerts

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
for b in raw artifacts results; do
    if awsx s3 ls 2>/dev/null | awk '{print $3}' | grep -qx "$b"; then
        pass "bucket '$b' exists"
    else
        fail "bucket '$b' missing"
    fi
done

# --- queues + redrive policy ---
queues_json="$(awsx sqs list-queues 2>/dev/null || echo '{}')"
for q in jobs jobs-dlq; do
    if echo "$queues_json" | grep -q "/$q\""; then
        pass "queue '$q' exists"
    else
        fail "queue '$q' missing"
    fi
done

dlq_arn="$(awsx sqs get-queue-attributes --queue-url "$ENDPOINT/000000000000/jobs-dlq" \
    --attribute-names QueueArn --query 'Attributes.QueueArn' --output text 2>/dev/null)"
redrive="$(awsx sqs get-queue-attributes --queue-url "$ENDPOINT/000000000000/jobs" \
    --attribute-names RedrivePolicy --query 'Attributes.RedrivePolicy' --output text 2>/dev/null)"

if echo "$redrive" | grep -q "$dlq_arn"; then
    pass "jobs -> jobs-dlq redrive policy wired (dlq arn: $dlq_arn)"
else
    fail "redrive policy missing or not pointing at jobs-dlq (got: $redrive)"
fi
if echo "$redrive" | grep -Eq '"maxReceiveCount": *3'; then
    pass "maxReceiveCount = 3"
else
    fail "maxReceiveCount != 3 (got: $redrive)"
fi

# --- tables ---
tables_json="$(awsx dynamodb list-tables --query 'TableNames' --output text 2>/dev/null || echo '')"
for t in jobs alerts; do
    if echo "$tables_json" | grep -qw "$t"; then
        pass "table '$t' exists"
    else
        fail "table '$t' missing"
    fi
done

echo
if ((FAIL)); then
    echo "RESULT: FAILED"
    exit 1
fi
echo "RESULT: OK"
