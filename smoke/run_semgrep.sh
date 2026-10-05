#!/usr/bin/env bash
# Marketplace semgrep gate — mirrors what Frappe Cloud's app review actually runs
# (github.com/frappe/semgrep-rules: frappe_correctness.yml + security/). It FAILS on any
# marketplace-blocking finding not suppressed by an inline `# nosemgrep: <rule> -- <reason>`
# or the repo-root .semgrepignore, so a rejection is caught here instead of at submission.
#
# History: an earlier version ran `--severity ERROR` only and MISSED the WARNING-level
# frappe-manual-commit (68) and the security-category frappe-subprocess-exec that the
# marketplace blocked on (Oct 2026). The lesson: the gate must run the marketplace's rule
# set, not a severity slice. Suppression is inline nosemgrep (with a real reason) or
# .semgrepignore for dev-only dirs (smoke/, tests/) — never a severity filter.
#
# Usage:
#   smoke/run_semgrep.sh [APP_PACKAGE_DIR]      # default: this app's package
#   FRAPPE_SEMGREP_RULES=/path/to/rules smoke/run_semgrep.sh
#
# Covers every UK MTD VAT Cloud-publishing app — point it at each package dir:
#   base:   .../zikpro-uk-vat/zikpro_uk_vat
#   pro:    .../zikpro-uk-vat-pro/zikpro_uk_vat_pro
#   broker: .../hmrc_broker_repo/hmrc_broker
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "${1:-}" ]; then
	APP_DIR="$1"
else
	# Auto-detect the app package: the subdir of the repo root that holds hooks.py.
	# Keeps this one script identical across base / Pro / broker.
	ROOT_GUESS="$(cd "$HERE/.." && pwd)"
	HOOK="$(find "$ROOT_GUESS" -maxdepth 2 -name hooks.py -not -path '*/node_modules/*' 2>/dev/null | head -1)"
	[ -z "$HOOK" ] && { echo "FAIL: could not auto-detect the app package (no hooks.py under $ROOT_GUESS)." >&2; exit 2; }
	APP_DIR="$(dirname "$HOOK")"
fi
APP_DIR="$(cd "$APP_DIR" && pwd)"
REPO_ROOT="$(cd "$APP_DIR/.." && pwd)"   # .semgrepignore lives at the repo root
PKG="$(basename "$APP_DIR")"
CACHE="${FRAPPE_SEMGREP_RULES:-$HOME/.cache/frappe-semgrep-rules}"
SEMGREP="${SEMGREP_BIN:-semgrep}"
command -v "$SEMGREP" >/dev/null 2>&1 || SEMGREP="/home/mkb_cma/my-bench/env/bin/semgrep"

if ! command -v "$SEMGREP" >/dev/null 2>&1; then
	echo "FAIL: semgrep not installed (pip install semgrep). The gate fails rather than skip." >&2
	exit 2
fi
if [ ! -d "$CACHE/rules" ]; then
	echo "Fetching frappe/semgrep-rules -> $CACHE"
	rm -rf "$CACHE"
	git clone --depth 1 https://github.com/frappe/semgrep-rules.git "$CACHE" >/dev/null 2>&1 || {
		echo "FAIL: could not fetch frappe/semgrep-rules and none cached at $CACHE." >&2
		exit 2
	}
fi

OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT
# Scan from the repo root so the repo-root .semgrepignore is honored; target the package.
( cd "$REPO_ROOT" && "$SEMGREP" scan \
	--config "$CACHE/rules/frappe_correctness.yml" \
	--config "$CACHE/rules/security" \
	--metrics=off --timeout 120 --json "$PKG" ) >"$OUT" 2>/dev/null || true

python3 - "$OUT" <<'PY'
import json, sys, collections
# whitelisted.yml rules that the marketplace's reviewer config does NOT block on (as observed
# Oct 2026). They are real quality items tracked in the ledger, but advisory for the gate —
# failing on 80+ type-hints would make the gate unusable without over-fixing. Everything else
# (manual-commit, single-value, subprocess, sql, rce, ...) is marketplace-blocking -> FAIL.
ADVISORY = {"missing-argument-type-hint", "guest-whitelisted-method", "whitelisted-side-effect-on-get"}
res = json.load(open(sys.argv[1])).get("results", [])
blocking, advisory = [], collections.Counter()
for r in res:
	rule = r["check_id"].split(".")[-1]
	if rule in ADVISORY:
		advisory[rule] += 1
		continue
	blocking.append((rule, r["path"], r["start"]["line"]))
note = f" (advisory, not marketplace-blocking: {dict(advisory)})" if advisory else ""
if blocking:
	print(f"SEMGREP GATE FAIL: {len(blocking)} marketplace-blocking finding(s){note}:")
	for rule, path, line in blocking:
		print(f"  [{rule}] {path}:{line}")
	sys.exit(1)
print(f"SEMGREP GATE PASS: 0 marketplace-blocking findings{note}.")
PY
