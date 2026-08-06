#!/usr/bin/env bash
# The five-step walkthrough from docs/demo.md, end to end.
#
#   1, 2  POST /v1/jobs                     submit, get a job id back
#   3     GET  /v1/jobs/{job_id}            poll until the status is terminal
#         GET  /v1/jobs                     everything this caller has submitted
#   4     GET  /v1/artifacts?job_id=...     what that job produced
#         GET  /v1/artifacts                the same listing with the filter taken off
#   5     GET  /v1/artifacts/{id}/content   the bytes, written to a file
#   6     sha256 the file and name which committed lesson it is
#
# Usage: scripts/demo.sh [base-url]        default http://localhost:8000
# Env:   USER_ID (default u_demo), OUT (default lesson.mp4), POLL_ATTEMPTS, POLL_SECONDS
#
# No jq: every id in this system carries its own type prefix, so `grep -oE 'job_[0-9A-...]{26}'`
# reads one out of a response without a JSON parser. There is no Idempotency-Key header any
# more (scope override item 2) -- two runs of this script are two jobs.
#
# Step 6 is the part that makes this a proof rather than a demonstration. A downloaded file of
# roughly the right size proves nothing: the assertion is the whole sha256 against the fixture
# the mock backend serves from, and the script says which of the three things happened -- it
# matched, it matched neither, or there was nothing on this machine to compare against.
set -euo pipefail

BASE_URL="${1:-http://localhost:8000}"
BASE_URL="${BASE_URL%/}"
USER_ID="${USER_ID:-u_demo}"
OUT="${OUT:-lesson.mp4}"
POLL_ATTEMPTS="${POLL_ATTEMPTS:-60}"
POLL_SECONDS="${POLL_SECONDS:-2}"

ID_BODY='[0-9A-HJKMNP-TV-Z]{26}'

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
FIXTURE_DIR="${SCRIPT_DIR}/../src/app/generation/backends/fixtures"
LESSONS='lesson_a.mp4 lesson_b.mp4'

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

# primary_id -- the artifact id of the row whose role is PRIMARY, or empty.
#
# Not `first_id art_`. A successful job publishes three learner rows -- POSTER, PRIMARY and
# TRANSCRIPT -- and the listing is ordered by (created_at, artifact_id), which for three rows
# written in one transaction is ordered by id alone. So the first id in the response is
# whichever of the three sorted lowest, and taking it downloads a PNG about a third of the time.
# `tr '{' '\n'` puts each item object on its own line, which is enough structure to pair a role
# with the id beside it without a JSON parser.
primary_id() {
    tr '{' '\n' <"$BODY" |
        grep -E '"role"[[:space:]]*:[[:space:]]*"PRIMARY"' |
        grep -oE "art_${ID_BODY}" |
        head -n 1
}

die() {
    printf 'demo: %s\n' "$1" >&2
    exit 1
}

# sha256_of FILE -- the hex digest on stdout, or nothing and a non-zero status when this
# machine has no hashing tool. Three spellings because the one that exists differs per
# platform: sha256sum on Linux, shasum on macOS, openssl wherever curl already came from.
sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d' ' -f1
    elif command -v openssl >/dev/null 2>&1; then
        openssl dgst -sha256 "$1" | sed 's/.*= *//'
    else
        return 1
    fi
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

ARTIFACT_ID="$(primary_id)"
[ -n "$ARTIFACT_ID" ] || fail "the job succeeded but published no PRIMARY artifact"
printf 'primary artifact id: %s\n' "$ARTIFACT_ID"

# The operator log is one of the four parts custody wrote and it is not in the response above:
# the listing is `LEARNER` and `CLEAN` only, and that predicate lives in the index (D073, A6).
printf 'roles listed: %s\n' \
    "$(tr '{' '\n' <"$BODY" | grep -oE '"role"[[:space:]]*:[[:space:]]*"[A-Z]+"' |
        grep -oE '[A-Z]+"$' | tr -d '"' | tr '\n' ' ')"

say '4b. GET /v1/artifacts -- the same endpoint with the filter taken off'
http GET /v1/artifacts
[ "$STATUS" = "200" ] || fail "listing artifacts unfiltered failed with HTTP $STATUS"
cat "$BODY"
printf '\n'

# Not "the caller's artifacts". X-User-Id is sent on this call and then ignored: the listing
# applies no ownership predicate, so this is every learner-facing artifact in the database,
# whoever made it. That is a known hole, marked @audit at orchestration/service.py::ListArtifacts,
# and the same one the job listing has. What the response does exclude is quarantined and
# operator-audience rows, because that predicate is in the index rather than in an ownership
# check (D073, A6).
#
# One "artifact_id" key per item, so counting the key counts the page without a JSON parser.
# A page, not the table: the default limit is 20 and next_cursor names the rest.
printf 'artifacts in this page: %s (default limit 20; next_cursor holds any more)\n' \
    "$(grep -oE '"artifact_id"' "$BODY" | wc -l | tr -d ' ' || true)"

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

# --------------------------------------------------------------------------- 6. is it a lesson

say '6. is what came back one of the committed lessons?'

digest="$(sha256_of "$OUT" || true)"
if [ -z "$digest" ]; then
    printf 'demo: no sha256 tool on PATH (sha256sum, shasum or openssl).\n'
    printf 'demo: %s downloaded, bytes NOT compared against a fixture.\n' "$OUT"
    exit 0
fi
printf 'sha256: %s\n' "$digest"

if [ ! -d "$FIXTURE_DIR" ]; then
    # Running from somewhere other than a checkout, e.g. against a remote host. The download
    # worked and there is nothing here to compare it to; saying so is the honest answer.
    printf 'demo: no fixture directory at %s.\n' "$FIXTURE_DIR"
    printf 'demo: %s downloaded, bytes NOT compared against a fixture.\n' "$OUT"
    exit 0
fi

matched=''
compared=0
for lesson in $LESSONS; do
    [ -f "${FIXTURE_DIR}/${lesson}" ] || continue
    compared=$((compared + 1))
    if [ "$(sha256_of "${FIXTURE_DIR}/${lesson}")" = "$digest" ]; then
        matched="$lesson"
        break
    fi
done

if [ "$compared" -eq 0 ]; then
    printf 'demo: found no lesson fixtures in %s.\n' "$FIXTURE_DIR"
    printf 'demo: %s downloaded, bytes NOT compared against a fixture.\n' "$OUT"
    exit 0
fi

if [ -n "$matched" ]; then
    printf '\ndemo: MATCH. %s is byte for byte %s.\n' "$OUT" "src/app/generation/backends/fixtures/${matched}"
    printf 'demo: a job submitted over HTTP came back as that video.\n'
    exit 0
fi

printf '\ndemo: NO MATCH. %s (%s bytes) is neither committed lesson.\n' "$OUT" "$bytes" >&2
printf 'demo: the request path worked; what it served is not the file the mock backend holds.\n' >&2
exit 1
