#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "[security-scan] not a git repository" >&2
  exit 2
fi

/usr/bin/python3 - <<'PY'
import ipaddress
import re
import subprocess
import sys
from pathlib import Path

root = Path(".")

patterns = [
    ("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("github_classic_pat", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "password_literal",
        re.compile(
            r"""(?ix)
            (?:password|passwd|pwd)\s*[:=]\s*
            (?!<password>|["']?<password>["']?)
            (?!\*{3,})
            (?!["']?\$?\{?[A-Z0-9_]+\}?["']?)
            ["']?[^\s"'`]{6,}["']?
            """
        ),
    ),
]

ip_pattern = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
allow_ip_literals = {
    "127.0.0.1",
    "0.0.0.0",
    "255.255.255.255",
}

try:
    output = subprocess.check_output(["git", "ls-files"], text=True)
except subprocess.CalledProcessError as exc:
    print(f"[security-scan] git ls-files failed: {exc}", file=sys.stderr)
    sys.exit(2)

files = [line.strip() for line in output.splitlines() if line.strip()]
issues = []

def is_probably_text(path: Path) -> bool:
    try:
        sample = path.read_bytes()[:4096]
    except Exception:
        return False
    return b"\x00" not in sample

for rel in files:
    rel_path = Path(rel)
    if rel_path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".zip", ".sqlite", ".db"}:
        continue
    if rel_path.parts and rel_path.parts[0] in {".state", "logs", "reports"}:
        continue
    path = root / rel_path
    if not path.exists() or not path.is_file() or not is_probably_text(path):
        continue

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue
    except Exception:
        continue

    for lineno, line in enumerate(text.splitlines(), 1):
        for rule, regex in patterns:
            if regex.search(line):
                issues.append((str(rel_path), lineno, rule, line.strip()[:220]))
        for match in ip_pattern.finditer(line):
            raw = match.group(0)
            if raw in allow_ip_literals:
                continue
            try:
                ip = ipaddress.ip_address(raw)
            except ValueError:
                continue
            if ip.is_private or ip.is_loopback or ip.is_multicast or ip.is_reserved or ip.is_link_local:
                continue
            issues.append((str(rel_path), lineno, "public_ip", line.strip()[:220]))

if issues:
    print("[security-scan] FAILED: possible sensitive data found:")
    for file, lineno, rule, snippet in issues:
        print(f"- {file}:{lineno} [{rule}] {snippet}")
    print("[security-scan] Fix or mask these before pushing.")
    sys.exit(1)

print("[security-scan] OK")
PY
