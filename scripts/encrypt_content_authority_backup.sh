#!/usr/bin/env bash
#
# Encrypt the private Content Pack authority backup at rest.
#
# The authority backup is the one file in this project whose loss is
# unrecoverable: it holds the signing identity every private release is signed
# with, and losing it means the shelf can never issue another release for the
# packs Story already installed. It currently sits as plain `authority.json` +
# `signing-key.pem` on disk, so any copy of this machine's home directory — a
# Time Machine snapshot, a synced folder, a stolen laptop — carries the key.
#
# This encrypts the whole backup directory into one AES-256 archive. The
# passphrase is read interactively (or from `CONTENT_BACKUP_PASSPHRASE`), written
# only to a 0600 temp file for openssl and removed on exit; it is never stored,
# never echoed, and never part of the output.
#
# It does NOT delete the plaintext backup — that is the owner's decision, and
# only after he has decrypted the archive once to prove he can.
#
# Usage:
#   scripts/encrypt_content_authority_backup.sh                 # newest backup
#   scripts/encrypt_content_authority_backup.sh encrypt <dir>
#   scripts/encrypt_content_authority_backup.sh decrypt <file.tar.enc> <into-dir>
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUPS="$ROOT/backups"
OPENSSL="${OPENSSL:-/opt/homebrew/bin/openssl}"
[ -x "$OPENSSL" ] || OPENSSL="$(command -v openssl)"
ITERATIONS=600000

die() { printf 'error: %s\n' "$*" >&2; exit 2; }

read_passphrase() {
  if [ -n "${CONTENT_BACKUP_PASSPHRASE:-}" ]; then
    printf '%s' "$CONTENT_BACKUP_PASSPHRASE"
    return
  fi
  [ -t 0 ] || die "no terminal for a passphrase prompt; set CONTENT_BACKUP_PASSPHRASE"
  local first='' second=''
  read -r -s -p 'Passphrase: ' first || true; printf '\n' >&2
  read -r -s -p 'Confirm:    ' second || true; printf '\n' >&2
  [ "$first" = "$second" ] || die "passphrases do not match"
  # Length is the only strength rule worth enforcing here; the passphrase is the
  # whole defence, so a short one is refused rather than warned about.
  [ "${#first}" -ge 12 ] || die "use at least 12 characters"
  printf '%s' "$first"
}

encrypt() {
  local source="${1:-}"
  if [ -z "$source" ]; then
    source="$(ls -dt "$BACKUPS"/content-authority-* 2>/dev/null | head -1 || true)"
  fi
  [ -n "$source" ] || die "no content-authority backup found under $BACKUPS"
  [ -d "$source" ] || die "not a directory: $source"
  [ -f "$source/authority.json" ] || die "$source does not look like an authority backup"

  local output="${BACKUP_ENCRYPTED_OUT:-$source.tar.enc}"
  [ -e "$output" ] && die "refusing to overwrite $output"

  local passphrase passfile tar_tmp
  passphrase="$(read_passphrase)"
  tar_tmp="$(mktemp "${TMPDIR:-/tmp}/kh-authority-XXXXXX.tar")"
  passfile="$(mktemp "${TMPDIR:-/tmp}/kh-pass-XXXXXX")"
  chmod 600 "$tar_tmp" "$passfile"
  # shellcheck disable=SC2064
  trap "rm -f '$tar_tmp' '$passfile'" EXIT

  printf '%s' "$passphrase" > "$passfile"
  unset passphrase CONTENT_BACKUP_PASSPHRASE

  tar -C "$(dirname "$source")" -cf "$tar_tmp" "$(basename "$source")"
  "$OPENSSL" enc -aes-256-cbc -pbkdf2 -iter "$ITERATIONS" -salt \
    -pass "file:$passfile" -in "$tar_tmp" -out "$output"
  chmod 600 "$output"

  # Verify by decrypting *the file just written* and comparing the archive digest,
  # so a truncated or miswritten output is caught here and not at restore time.
  local expected actual
  expected="$(shasum -a 256 "$tar_tmp" | cut -d' ' -f1)"
  actual="$("$OPENSSL" enc -d -aes-256-cbc -pbkdf2 -iter "$ITERATIONS" \
    -pass "file:$passfile" -in "$output" | shasum -a 256 | cut -d' ' -f1)"
  [ "$expected" = "$actual" ] || die "verification failed: the written archive does not decrypt to the same bytes"

  printf 'encrypted %s\n' "$output"
  printf 'covers      %s\n' "$(basename "$source")"
  printf 'sha256      %s\n' "$expected"
  printf 'size        %s bytes\n' "$(wc -c < "$output" | tr -d ' ')"
  printf '\nStore the passphrase somewhere that survives this Mac (a password\n'
  printf 'manager, or paper). Without it this archive cannot be opened, and the\n'
  printf 'plaintext backup is still on disk until you decide to remove it.\n'
}

decrypt() {
  local archive="${1:-}" target="${2:-}"
  [ -n "$archive" ] && [ -n "$target" ] || die "usage: $0 decrypt <file.tar.enc> <into-dir>"
  [ -f "$archive" ] || die "no such archive: $archive"
  [ -e "$target" ] && die "refusing to write into existing $target"

  local passphrase passfile
  passphrase="$(read_passphrase)"
  passfile="$(mktemp "${TMPDIR:-/tmp}/kh-pass-XXXXXX")"
  chmod 600 "$passfile"
  # shellcheck disable=SC2064
  trap "rm -f '$passfile'" EXIT
  printf '%s' "$passphrase" > "$passfile"
  unset passphrase CONTENT_BACKUP_PASSPHRASE

  mkdir -p "$target"
  "$OPENSSL" enc -d -aes-256-cbc -pbkdf2 -iter "$ITERATIONS" \
    -pass "file:$passfile" -in "$archive" | tar -C "$target" -xf -
  printf 'decrypted into %s\n' "$target"
  ls -la "$target"/*/ 2>/dev/null || ls -la "$target"
}

case "${1:-encrypt}" in
  encrypt) shift || true; encrypt "${1:-}" ;;
  decrypt) shift; decrypt "${1:-}" "${2:-}" ;;
  -h|--help|help) sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
  *) die "unknown action '${1}'; use encrypt or decrypt" ;;
esac
