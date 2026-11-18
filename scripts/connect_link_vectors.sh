#!/usr/bin/env bash
# Generates tests/vectors/connect_links.json with printf + openssl, independently of the
# Python implementation, so the connect-link tests check our code against something else.
#
# Each canonical string below is written out by hand following spec §6.1: parameters
# except sig, sorted by name, RFC 3986 percent-encoding (unreserved A-Z a-z 0-9 - . _ ~
# left as is, space as %20, uppercase hex, UTF-8), joined with "&".
# The secret is a fixed, fake test value.
set -euo pipefail

SECRET="test-connect-link-secret-not-real"
OUT="$(dirname "$0")/../tests/vectors/connect_links.json"

hmac() { printf '%s' "$1" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}'; }

# name | params as JSON | canonical string
CASES=(
  'basic|{"app_id":"demo","external_user_id":"u_demo","return_to":"http://localhost:8000/","exp":"1700000600"}|app_id=demo&exp=1700000600&external_user_id=u_demo&return_to=http%3A%2F%2Flocalhost%3A8000%2F'
  'space_plus_and_unicode|{"app_id":"demo","external_user_id":"user name+1 é","return_to":"https://example.test/return path","exp":"1700000600"}|app_id=demo&exp=1700000600&external_user_id=user%20name%2B1%20%C3%A9&return_to=https%3A%2F%2Fexample.test%2Freturn%20path'
  'unreserved_untouched|{"app_id":"demo","external_user_id":"A-z.0_9~","return_to":"https://example.test/cb","exp":"1700000600"}|app_id=demo&exp=1700000600&external_user_id=A-z.0_9~&return_to=https%3A%2F%2Fexample.test%2Fcb'
)

{
  printf '{\n  "generated_by": "scripts/connect_link_vectors.sh",\n'
  printf '  "secret": "%s",\n  "cases": [\n' "$SECRET"
  for i in "${!CASES[@]}"; do
    IFS='|' read -r name params canonical <<<"${CASES[$i]}"
    sep=","; [[ $i -eq $((${#CASES[@]} - 1)) ]] && sep=""
    printf '    {"name": "%s", "params": %s, "canonical": "%s", "sig": "%s"}%s\n' \
      "$name" "$params" "$canonical" "$(hmac "$canonical")" "$sep"
  done
  printf '  ]\n}\n'
} >"$OUT"
echo "wrote $OUT"
