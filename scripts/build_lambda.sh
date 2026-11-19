#!/usr/bin/env bash
# Builds build/receiver: the receiver's Lambda bundle for arm64, Python 3.12.
# Third-party wheels are fetched for the Lambda platform by uv, so no Docker is needed.
# boto3 is left out on purpose: the Lambda runtime provides it.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT=build/receiver
rm -rf "$OUT"
mkdir -p "$OUT"

uv export --frozen --no-dev --no-hashes --no-emit-project --quiet -o build/requirements.txt
uv pip install --quiet \
  --target "$OUT" \
  --python-platform aarch64-manylinux2014 \
  --python-version 3.12 \
  --only-binary :all: \
  -r build/requirements.txt

rsync -a --exclude '__pycache__' receiver/ "$OUT/receiver/"
rsync -a --exclude '__pycache__' src/token_keeper/ "$OUT/token_keeper/"
echo "built $OUT ($(du -sh "$OUT" | cut -f1))"
