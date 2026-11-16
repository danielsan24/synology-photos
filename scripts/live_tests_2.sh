#!/bin/bash
# Round 2: checks the fixes from round 1. Read-only except nothing. Run in Terminal.app:
#   bash scripts/live_tests_2.sh      Log: $TMPDIR/synophotos-live-2/log.txt (outside the repository)
set -u
SP="uv run --quiet $(cd "$(dirname "$0")" && pwd)/synophotos.py"
OUT="${TMPDIR:-/tmp}/synophotos-live-2"; mkdir -p "$OUT"
exec > >(tee "$OUT/log.txt") 2>&1
P="${TEST_PERSON:?set TEST_PERSON to a Synology person name}"; P2="${TEST_PERSON2:-$P}"
step() { echo; echo "=== [$(date +%H:%M:%S)] $*"; }

step "R1 whole-space filters (round 1 returned 1 photo)"
$SP count --person "$P" --from 2024-07-01 --to 2024-07-31
$SP count --from 2024-07-01 --to 2024-07-31
$SP count --person "$P" --person "$P2" --all-people --from 2024-07-01 --to 2024-07-31
$SP count --rating 4
$SP --json items --person "$P" --from 2024-07-01 --to 2024-07-31 --limit 3 | jq -c '.[] | {id, date, people}'

step "R2 tag filter on a tag that exists"
TAG=$($SP --json tags | jq -r '.[0].name // empty'); echo "tag: $TAG"
[ -n "$TAG" ] && $SP count --tag "$TAG"

step "R3 rotated photos: face boxes (orientation_original != 1)"
for oid in $($SP --json items --person "$P" --from 2019-06-01 --to 2021-12-31 --limit 1500 | jq -r '[.[] | select((.orientation_original // 1) != 1 or (.orientation // 1) != 1)][0:3][] | .id'); do
  $SP --json item $oid | jq -c '.[0] | {id, filename, orientation, orientation_original}'
  $SP faces $oid --draw "$OUT/rotated" | jq -c '.[] | {face_id, name}'
done
ls "$OUT/rotated" 2>/dev/null || echo "no rotated photo found"

step "R4 throughput: one month of TEST_PERSON, small previews, then resume"
time $SP download --person "$P" --from 2024-07-01 --to 2024-07-31 --size sm --out "$OUT/dl" --limit 200
time $SP download --person "$P" --from 2024-07-01 --to 2024-07-31 --size sm --out "$OUT/dl" --limit 200
step "done"
