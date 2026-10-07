#!/usr/bin/env bash
# Controller-owned Linux tools for synthetic Langfuse validation.
set -euo pipefail
validation_tmp=$(mktemp -d)
trap 'rm -rf "$validation_tmp"' EXIT
validation_release=https://github.com/golang-migrate/migrate/releases/download/v4.20.1
curl --fail --location --retry 3 "$validation_release/migrate.linux-amd64.tar.gz" -o "$validation_tmp/migrate.linux-amd64.tar.gz"
curl --fail --location --retry 3 "$validation_release/sha256sum.txt" -o "$validation_tmp/sha256sum.txt"
(cd "$validation_tmp" && sha256sum --check --strict --ignore-missing sha256sum.txt)
tar --extract --gzip --file "$validation_tmp/migrate.linux-amd64.tar.gz" --directory "$validation_tmp" migrate
test -f "$validation_tmp/migrate"
test ! -L "$validation_tmp/migrate"
sudo install -m 0755 "$validation_tmp/migrate" /usr/local/bin/migrate
migrate -version
