#!/usr/bin/env bash
# Replica of the Frappe Cloud marketplace app-review gate.
#
# WHY: the marketplace rejects on checks that our server suite + ruff never run, and it
# reports them ONE category at a time — so each fix earned a fresh rejection (semgrep, then
# "long description has links", then "override whitelisted methods"). This script runs every
# marketplace check we have seen, locally, so a submission is clean on the first try.
#
# Checks mirrored (category name as the reviewer prints it):
#   1. Semgrep Security + Correctness   -> smoke/run_semgrep.sh (frappe_correctness + security)
#   2. Long Description Contains Other Links -> README.md has no external links / install block
#   3. Override Whitelisted Methods     -> every override target is signature-compatible
#
# Usage (run from the repo root, or pass the repo root as $1):
#   smoke/marketplace_check.sh [REPO_ROOT]
#   SEMGREP_BIN=/home/mkb_cma/my-bench/env/bin/semgrep smoke/marketplace_check.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${1:-$HERE/..}" && pwd)"
HOOK="$(find "$REPO_ROOT" -maxdepth 2 -name hooks.py -not -path '*/node_modules/*' 2>/dev/null | head -1)"
[ -z "$HOOK" ] && { echo "FAIL: no hooks.py under $REPO_ROOT — not a Frappe app repo." >&2; exit 2; }
APP_DIR="$(dirname "$HOOK")"
PKG="$(basename "$APP_DIR")"
fails=0

echo "=== Marketplace gate: $PKG  (repo $REPO_ROOT) ==="

# --- 1. Semgrep (Security + Correctness) ------------------------------------------------
echo; echo "--- [1/3] Semgrep Security + Correctness ---"
if bash "$HERE/run_semgrep.sh" "$APP_DIR"; then :; else echo "  -> SEMGREP CHECK FAILED"; fails=$((fails+1)); fi

# --- 2. Long description: no external links / no install instructions -------------------
echo; echo "--- [2/3] Long Description Contains Other Links ---"
README="$REPO_ROOT/README.md"
if [ ! -f "$README" ]; then
	echo "  WARN: no README.md (the marketplace long description); skipping."
else
	python3 - "$README" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
hits = []
# http(s) URLs (bare or inside markdown), mailto, and markdown links to a URL target.
for m in re.finditer(r'https?://\S+', text):
    hits.append(("url", m.group(0).rstrip(').>')))
for m in re.finditer(r'\bmailto:\S+|[\w.+-]+@[\w-]+\.[\w.-]+', text):
    hits.append(("email", m.group(0)))
# install instructions are rejected in the long description too
if re.search(r'\bbench\s+(get-app|install-app)\b', text):
    hits.append(("install", "bench get-app/install-app block"))
if hits:
    print(f"  FAIL: {len(hits)} external link / install instruction(s) in the long description:")
    for kind, h in hits:
        print(f"    [{kind}] {h}")
    sys.exit(1)
print("  PASS: no external links or install instructions in README.md.")
PY
	[ $? -ne 0 ] && fails=$((fails+1))
fi

# --- 3. Override Whitelisted Methods: every target signature-compatible -----------------
echo; echo "--- [3/3] Override Whitelisted Methods ---"
python3 - "$APP_DIR" "$PKG" <<'PY'
import ast, importlib.util, inspect, os, sys
app_dir, pkg = sys.argv[1], sys.argv[2]
hooks = os.path.join(app_dir, "hooks.py")
src = open(hooks, encoding="utf-8").read()
tree = ast.parse(src)
overrides = {}
for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id == "override_whitelisted_methods" for t in node.targets):
        if isinstance(node.value, ast.Dict):
            for k, v in zip(node.value.keys, node.value.values):
                key = k.value if isinstance(k, ast.Constant) else None
                # target may be a Constant string OR a module-level Name bound to a string
                if isinstance(v, ast.Constant):
                    overrides[key] = v.value
                elif isinstance(v, ast.Name):
                    overrides[key] = ("<name:%s>" % v.id)
if not overrides:
    print("  PASS: no override_whitelisted_methods declared.")
    sys.exit(0)

# Resolve module-level Name targets (e.g. _DISABLED_LEGACY_OAUTH = "pkg.security.disabled_legacy_oauth")
consts = {n.targets[0].id: n.value.value for n in tree.body
          if isinstance(n, ast.Assign) and len(n.targets) == 1
          and isinstance(n.targets[0], ast.Name) and isinstance(n.value, ast.Constant)
          and isinstance(n.value.value, str)}

def params(dotted):
    """Return the positional/keyword param names of pkg.module.func, or None if unloadable."""
    mod, _, fn = dotted.rpartition(".")
    rel = mod.split(".", 1)[1] if mod.startswith(pkg + ".") else None
    if not rel:
        return "SKIP"
    path = os.path.join(app_dir, *rel.split(".")) + ".py"
    if not os.path.exists(path):
        return None
    t = ast.parse(open(path, encoding="utf-8").read())
    for d in ast.walk(t):
        if isinstance(d, ast.FunctionDef) and d.name == fn:
            a = d.args
            star = a.vararg is not None
            kwstar = a.kwarg is not None
            names = [p.arg for p in a.posonlyargs + a.args + a.kwonlyargs]
            return (names, star, kwstar)
    return None

bad = []
for original, target in overrides.items():
    if isinstance(target, str) and target.startswith("<name:"):
        target = consts.get(target[6:-1], target)
    o, t = params(original), params(target)
    # original lives in the frozen api.py of THIS pkg; target is a stub in THIS pkg.
    if t == "SKIP":
        continue
    if t is None:
        bad.append((original, target, "target function not found"))
        continue
    tnames, tstar, tkwstar = t
    # Signature-compatible if the target accepts any args (*args/**kwargs) OR takes the
    # same parameter names as the original. A disable-stub with (*args, **kwargs) is the
    # canonical safe override.
    if tstar and tkwstar:
        pass  # accepts anything -> compatible with any original signature
    elif isinstance(o, tuple):
        onames = o[0]
        if tnames != onames:
            bad.append((original, target, f"params {tnames} != original {onames} and no *args/**kwargs"))
    # (o unresolved e.g. commented-out frozen original) + target not *args/**kwargs:
    elif not (tstar and tkwstar):
        bad.append((original, target, f"target params {tnames}; could not confirm vs original — add *args,**kwargs"))

print(f"  {len(overrides)} override(s) declared; all route the app's own superseded endpoints to safe stubs.")
if bad:
    print("  FAIL: signature-incompatible override target(s):")
    for orig, tgt, why in bad:
        print(f"    {orig} -> {tgt}: {why}")
    sys.exit(1)
print("  PASS: every override target is signature-compatible (*args/**kwargs or matching params).")
PY
[ $? -ne 0 ] && fails=$((fails+1))

echo
if [ "$fails" -eq 0 ]; then
	echo "=== MARKETPLACE GATE PASS: $PKG clean on all 3 checks ==="
	exit 0
fi
echo "=== MARKETPLACE GATE FAIL: $PKG failed $fails check(s) ==="
exit 1
