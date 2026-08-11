#!/usr/bin/env bash
#
# Register the GDAL actor image as a PERSISTENT Tapis Abaco actor (one-time
# setup). dso-geo references this actor by id via GEO_ACTOR_ID — it does NOT
# register an actor per call.
#
# This performs an EXTERNAL WRITE (creates a Tapis actor). Run it yourself with
# a fresh Tapis token. Re-run only to (re)create the actor (e.g. after an image
# update — Abaco can also be told to pull :latest; pin a version tag for repro).
#
# Usage:
#   export TAPIS_TOKEN=$(../../../ckan-docker/scripts/tapis-oauth/get-jwt.sh <user> <pass>)
#   ./register-actor.sh
#
# Override the image (recommended: pin a version tag, not :latest):
#   ACTOR_IMAGE=ghcr.io/in-for-disaster-analytics/mcp-suite/gdal-actor:v0.1.0 ./register-actor.sh
#
# On success it prints the actor id; set it in your environment / .mcp.json:
#   export GEO_ACTOR_ID=<printed id>

set -euo pipefail

BASE="${TAPIS_BASE:-https://portals.tapis.io}"
# Must match what build-gdal-actor.yml actually pushes: ghcr.io/<github.repository>/gdal-actor,
# i.e. the repo owner (In-For-Disaster-Analytics), not a personal namespace.
ACTOR_IMAGE="${ACTOR_IMAGE:-ghcr.io/in-for-disaster-analytics/mcp-suite/gdal-actor:latest}"
ACTOR_NAME="${ACTOR_NAME:-dso-geo-gdal}"
: "${TAPIS_TOKEN:?Set TAPIS_TOKEN to a fresh Tapis JWT}"

hdr=(-H "X-Tapis-Token: ${TAPIS_TOKEN}" -H "Content-Type: application/json")

echo "Registering persistent actor '${ACTOR_NAME}' from ${ACTOR_IMAGE} ..."
resp=$(curl -s -m 30 "${hdr[@]}" -X POST "${BASE}/v3/actors" \
  -d "{\"image\":\"${ACTOR_IMAGE}\",\"name\":\"${ACTOR_NAME}\",\"description\":\"dso-geo GDAL actor (metadata + transforms over /vsicurl)\",\"stateless\":true}")

ACTOR_ID=$(echo "${resp}" | python3 -c "import sys,json;print(json.load(sys.stdin).get('result',{}).get('id',''))" 2>/dev/null || echo "")
if [ -z "${ACTOR_ID}" ]; then
  echo "ERROR: no actor id returned. Response:"; echo "${resp}" | head -c 600; echo
  echo "(If an actor named '${ACTOR_NAME}' already exists, list with: curl ${hdr[*]} ${BASE}/v3/actors )"
  exit 1
fi

echo
echo "  Registered actor id: ${ACTOR_ID}"
echo
echo "Set this for the dso-geo server:"
echo "  export GEO_ACTOR_ID=${ACTOR_ID}"
echo
echo "Waiting for READY ..."
for _ in $(seq 1 20); do
  st=$(curl -s -m 15 "${hdr[@]}" "${BASE}/v3/actors/${ACTOR_ID}" | python3 -c "import sys,json;print(json.load(sys.stdin).get('result',{}).get('status',''))" 2>/dev/null || echo "")
  echo "  status=${st}"; [ "${st}" = "READY" ] && break; sleep 6
done
