#!/usr/bin/env bash
# The five-step walkthrough from docs/demo.md, end to end.
#
#   1, 2  POST /v1/jobs                     submit, get a job id back
#   3     GET  /v1/jobs/{job_id}            poll until the status is terminal
#         GET  /v1/jobs                     everything this caller has submitted
#   4     GET  /v1/artifacts?job_id=...     what that job produced
#   5     GET  /v1/artifacts/{id}/content   the bytes, written to a file
#
# Usage: scripts/demo.sh [base-url]        default http://localhost:8000
# Env:   USER_ID (default u_demo), OUT (default lesson.mp4), POLL_ATTEMPTS, POLL_SECONDS
#
# No jq: every id in this system carries its own type prefix, so `grep -oE 'job_[0-9A-...]{26}'`
# reads one out of a response without a JSON parser. There is no Idempotency-Key header any
# more (scope override item 2) -- two runs of this script are two jobs.
set -euo pipefail

BASE_URL="${1:-http://localhost:8000}"
BASE_URL="${BASE_URL%/}"
USER_ID="${USER_ID:-u_demo}"
OUT="${OUT:-lesson.mp4}"
POLL_ATTEMPTS="${POLL_ATTEMPTS:-60}"
POLL_SECONDS="${POLL_SECONDS:-2}"

ID_BODY='[0-9A-HJKMNP-TV-Z]{26}'

command -v curl >/dev/null 2>&1 || {
    printf 'demo: curl is required and was not found on PATH\n' >&2
    exit 1
}

BODY="$(mktemp)"
trap 'rm -f "$BODY"' EXIT

STATUS=""

say() {
    printf '\n=== %s\n' "$1"
}

# http METHOD PATH [JSON] -- body lands in $BODY, HTTP status in $STATUS.
# X-User-Id is optional (scope override item 1: there is no authentication, and an absent
# header becomes APP_DEFAULT_PRINCIPAL_ID). It is sent anyway so the script shows where
# identity goes: at the credential position, never in the body.
http() {
    local method="$1" path="$2" data="${3-}"
    if [ -n "$data" ]; then
        STATUS="$(curl -sS -o "$BODY" -w '%{http_code}' \
            -X "$method" \
            -H "X-User-Id: ${USER_ID}" \
            -H 'Content-Type: application/json' \
            -d "$data" \
            "${BASE_URL}${path}")"
    else
        STATUS="$(curl -sS -o "$BODY" -w '%{http_code}' \
            -X "$method" \
            -H "X-User-Id: ${USER_ID}" \
            "${BASE_URL}${path}")"
    fi
    printf '%s %s -> %s\n' "$method" "$path" "$STATUS"
}

# json_string KEY -- first string value for that key, or empty.
json_string() {
    grep -oE "\"$1\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" "$BODY" |
        head -n 1 |
        sed 's/.*"\([^"]*\)"$/\1/'
}

# json_number KEY -- first numeric value for that key, or empty.
json_number() {
    grep -oE "\"$1\"[[:space:]]*:[[:space:]]*-?[0-9]+" "$BODY" |
        head -n 1 |
        grep -oE -- '-?[0-9]+$'
}

first_id() {
    grep -oE "$1${ID_BODY}" "$BODY" | head -n 1
}

die() {
    printf 'demo: %s\n' "$1" >&2
    exit 1
}

fail() {
    printf 'demo: last response was\n' >&2
    cat "$BODY" >&2
    printf '\n' >&2
    die "$1"
}

printf 'demo: base url %s, caller %s\n' "$BASE_URL" "$USER_ID"

# --------------------------------------------------------------------------- 0. is it up

say '0. GET /health'
http GET /health
[ "$STATUS" = "200" ] || fail "the service is not healthy (is \`make up\` finished?)"
cat "$BODY"
printf '\n'

# --------------------------------------------------------------------------- 1, 2. submit

say '1, 2. POST /v1/jobs'
http POST /v1/jobs '{"instruction":"why do atoms form covalent bonds",
     "context":[{"kind":"LEVEL","text":"grade 9"}]}'
cat "$BODY"
printf '\n'

case "$STATUS" in
    202 | 201 | 200) ;;
    429) fail "admission refused this job: three are already QUEUED or RUNNING (D090)" ;;
    *) fail "submit failed with HTTP $STATUS" ;;
esac

JOB_ID="$(first_id 'job_')"
[ -n "$JOB_ID" ] || fail "no job id in the response"
printf 'job id: %s\n' "$JOB_ID"

# --------------------------------------------------------------------------- 3. poll

say "3. GET /v1/jobs/${JOB_ID} until it is terminal"

JOB_STATUS=""
attempt=0
while [ "$attempt" -lt "$POLL_ATTEMPTS" ]; do
    attempt=$((attempt + 1))
    http GET "/v1/jobs/${JOB_ID}"
    [ "$STATUS" = "200" ] || fail "reading the job failed with HTTP $STATUS"

    JOB_STATUS="$(json_string status)"
    stage="$(json_string stage)"
    percent="$(json_number percent)"
    printf '  attempt %s: %s %s %s%%\n' "$attempt" "$JOB_STATUS" "$stage" "${percent:-0}"

    case "$JOB_STATUS" in
        SUCCEEDED | FAILED | CANCELLED) break ;;
    esac
    sleep "$POLL_SECONDS"
done

cat "$BODY"
printf '\n'

case "$JOB_STATUS" in
    SUCCEEDED) ;;
    FAILED | CANCELLED) fail "the job ended $JOB_STATUS; the document above holds failure.code" ;;
    *) fail "the job was still $JOB_STATUS after $POLL_ATTEMPTS polls" ;;
esac

say '3b. GET /v1/jobs -- everything this caller has submitted'
http GET /v1/jobs
[ "$STATUS" = "200" ] || fail "listing jobs failed with HTTP $STATUS"
cat "$BODY"
printf '\n'

# --------------------------------------------------------------------------- 4. artifacts

say "4. GET /v1/artifacts?job_id=${JOB_ID}"
http GET "/v1/artifacts?job_id=${JOB_ID}"
[ "$STATUS" = "200" ] || fail "listing artifacts failed with HTTP $STATUS"
cat "$BODY"
printf '\n'

ARTIFACT_ID="$(first_id 'art_')"
[ -n "$ARTIFACT_ID" ] || fail "the job succeeded but produced no artifact"
printf 'artifact id: %s\n' "$ARTIFACT_ID"

# --------------------------------------------------------------------------- 5. the video

say "5. GET /v1/artifacts/${ARTIFACT_ID}/content -> ${OUT}"
content_status="$(curl -sS -o "$OUT" -w '%{http_code}' \
    -H "X-User-Id: ${USER_ID}" \
    "${BASE_URL}/v1/artifacts/${ARTIFACT_ID}/content")"
printf 'GET /v1/artifacts/%s/content -> %s\n' "$ARTIFACT_ID" "$content_status"
if [ "$content_status" != "200" ]; then
    # The error envelope, not the video, is now sitting in $OUT.
    head -c 512 "$OUT" >&2
    printf '\n' >&2
    rm -f "$OUT"
    die "downloading the artifact failed with HTTP $content_status"
fi

bytes="$(wc -c <"$OUT" | tr -d ' ')"
printf '\ndemo: wrote %s (%s bytes). Play it.\n' "$OUT" "$bytes"
