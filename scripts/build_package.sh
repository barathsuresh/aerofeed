#!/usr/bin/env bash
# Stage the Lambda deployment package for Terraform to zip and hash.
#
# Terraform's archive_file zips a directory; it cannot pip-install. So the
# staging happens here and Terraform owns the zip, which keeps source_code_hash
# consistent with what is actually deployed.
#
# `requests` is not in the python3.13 Lambda runtime and
# core/airplanes_live_client imports it. boto3 IS in the runtime and is
# deliberately not vendored — it would add ~15MB for no benefit.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/.build"
DEPS="$BUILD/deps"
LEGACY_PKG="$BUILD/pkg"
FUNCTIONS=(poller processor connect subscribe default disconnect grace-check)

rm -rf "$DEPS" "$LEGACY_PKG" "$BUILD/lambdas"
mkdir -p "$DEPS" "$LEGACY_PKG" "$BUILD/lambdas"

python3 -m pip install --target "$DEPS" --quiet --no-compile requests

for dir in core local aws lambdas; do
  cp -r "$ROOT/$dir" "$LEGACY_PKG/"
done

cp -a "$DEPS"/. "$LEGACY_PKG/"

for function in "${FUNCTIONS[@]}"; do
  pkg="$BUILD/lambdas/$function"
  mkdir -p "$pkg"
  cp -a "$DEPS"/. "$pkg/"
  for dir in core local aws lambdas; do
    cp -r "$ROOT/$dir" "$pkg/"
  done
done

# Deterministic contents: __pycache__ and dist-info carry timestamps and paths
# that change between builds and would churn the package hash for no reason.
find "$BUILD/lambdas" "$LEGACY_PKG" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
find "$BUILD/lambdas" "$LEGACY_PKG" -name "*.dist-info" -type d -exec rm -rf {} + 2>/dev/null || true
find "$BUILD/lambdas" "$LEGACY_PKG" -name "*.pyc" -delete 2>/dev/null || true

for function in "${FUNCTIONS[@]}"; do
  echo "staged $(find "$BUILD/lambdas/$function" -type f | wc -l) files in $BUILD/lambdas/$function"
done
