#!/usr/bin/env bash
set -euo pipefail

cmd="${1:-}"
infile="${2:-}"
outfile="${3:-}"

if [[ -z "$cmd" || -z "$infile" ]]; then
  echo "Usage: crypt (enc|dec) <input> [output]"
  echo ""
  echo "  enc — encrypt with AES-256-CBC + salt"
  echo "  dec — decrypt"
  exit 1
fi

if [[ "$cmd" == "enc" ]]; then
  out="${outfile:-$infile.enc}"
  openssl enc -aes-256-cbc -salt -pbkdf2 -iter 600000 -in "$infile" -out "$out"
  echo "→ $out"
elif [[ "$cmd" == "dec" ]]; then
  out="${outfile:-${infile%.enc}.dec}"
  openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -in "$infile" -out "$out"
  echo "→ $out"
else
  echo "unknown command: $cmd" >&2
  exit 1
fi
