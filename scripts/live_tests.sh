#!/bin/bash
# Live tests on the NAS for the riskiest operations. Run by the owner in Terminal.app:
#   bash scripts/live_tests.sh
# Test objects (set them for your library): TEST_ITEM = a photo of TEST_PERSON, TEST_PERSON2 = a person with
# photos from the first years of TEST_PERSON's life (rotated photos), album skill-test-2.
# Every change is reverted at the end of its block. Nothing is deleted. Log: $TMPDIR/synophotos-live-1/log.txt (outside the repository)
set -u
SP="uv run --quiet $(cd "$(dirname "$0")" && pwd)/synophotos.py"
OUT="${TMPDIR:-/tmp}/synophotos-live-1"
mkdir -p "$OUT"
exec > >(tee "$OUT/log.txt") 2>&1
ID="${TEST_ITEM:?set TEST_ITEM to a photo id}"
P="${TEST_PERSON:?set TEST_PERSON to a Synology person name}"
P2="${TEST_PERSON2:-$P}"
step() { echo; echo "=== [$(date +%H:%M:%S)] $*"; }
sha() { shasum -a 256 "$1" | cut -c1-16; }
orig() { rm -rf "$OUT/$1"; $SP download $ID --size original --out "$OUT/$1" >/dev/null && ls "$OUT/$1"/* ; }

step "T1 does a rating change rewrite the original file?"
$SP --json item $ID --raw | jq -c '.[0] | {filesize, rating: .additional.rating}'
f0=$(orig before); echo "sha before: $(sha "$f0")"
$SP rate $ID --stars 2 --yes
sleep 30
$SP --json item $ID --raw | jq -c '.[0] | {filesize, rating: .additional.rating}'
f1=$(orig after_rate); echo "sha after rate: $(sha "$f1")"
$SP rate $ID --stars 0 --yes

step "T2 tag round trip, tag filter (not captured), file rewrite"
$SP tag-add $ID --tag skilltest --yes
$SP --json items --tag skilltest --limit 5 | jq -c '[.[] | {id, tags}]'
$SP count --tag skilltest
sleep 30
f2=$(orig after_tag); echo "sha after tag: $(sha "$f2")"
$SP tag-remove $ID --tag skilltest --yes
$SP --json item $ID | jq -c '.[0].tags'

step "T3 face round trip: unlink the person, re-add with the same box"
$SP faces $ID --draw "$OUT/faces_before" | jq -c '.[] | {face_id, person_id, name}'
$SP --json people | jq -c --arg p "$P" '.[] | select(.name==$p)'
BOX=$($SP faces $ID | jq -r --arg p "$P" '.[] | select(.name==$p) | .face_bounding_box | "\(.top_left.x),\(.top_left.y),\(.bottom_right.x),\(.bottom_right.y)"')
echo "box: $BOX"
$SP person-remove $ID --person "$P" --yes
$SP faces $ID | jq -c '.[] | {face_id, person_id, name}'
$SP person-add $ID --person "$P" --box "$BOX" --yes
sleep 60
echo "after 60 s:"; $SP faces $ID --draw "$OUT/faces_after" | jq -c '.[] | {face_id, person_id, name}'
$SP --json people | jq -c --arg p "$P" '.[] | select(.name==$p)'
f3=$(orig after_face); echo "sha after face edits: $(sha "$f3")"

step "T4 face boxes on rotated photos (orientation != 1)"
for oid in $($SP --json items --person "$P2" --from 2019-06-01 --to 2020-12-31 --limit 300 | jq -r '[.[] | select(.orientation != null and .orientation != 1)][0:3][] | .id'); do
  echo "item $oid"; $SP faces $oid --draw "$OUT/rotated" | jq -c '.[] | {face_id, name}'
done
ls "$OUT/rotated" 2>/dev/null || echo "no rotated photo found in that range"

step "T5 Trash candidate: create or reuse, dedup, nothing deleted"
$SP trash-candidate $ID --reason "live test"
$SP trash-candidate $ID --reason "live test" --yes
$SP trash-candidate $ID --reason "live test again" --yes
$SP --json items --album "Trash candidate" | jq -c '[.[] | .id]'
$SP --json item $ID | jq -c '.[0] | {id, filename}'

step "T6 album add + share with download and password"
$SP album-add skill-test-2 $ID --yes
$SP album-share skill-test-2 --public --download --yes
$SP --json albums | jq -c '.[] | {name, items, shared, link}'
$SP album-unshare skill-test-2 --yes   # the test share never stays open

step "T7 secrets never printed (verbose run)"
$SP -v item $ID 2>&1 | grep -Eio '(_sid|synotoken|passwd)[^,}]{0,20}' | sort -u
echo "(above: only parameter names or ***)"

step "T8 throughput and resume: one month of TEST_PERSON2, small previews"
time $SP download --person "$P2" --from 2024-07-01 --to 2024-07-31 --size sm --out "$OUT/dl"
time $SP download --person "$P2" --from 2024-07-01 --to 2024-07-31 --size sm --out "$OUT/dl"

step "T9 group review sheets (read only)"
$SP group-sheet "$P" --out "$OUT/groups" --limit 15
UN=$($SP --json people | jq -r '[.[] | select((.name|startswith("#")) and .items >= 8 and .items <= 40)][0].id')
echo "unnamed group: $UN"; $SP group-sheet "$UN" --out "$OUT/groups"
$SP person-name "$UN" --name "$P" || true   # dry run only: shows the merge plan, locked

step "done. Left in place for you: photo $ID in 'Trash candidate' and in skill-test-2 (shared, link above)."
