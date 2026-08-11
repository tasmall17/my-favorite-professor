#!/usr/bin/env bash
#
# my-favorite-professor — installer
#
#   git clone https://github.com/tasmall17/my-favorite-professor.git
#   cd my-favorite-professor
#   ./install.sh
#
# Sets up a self-contained environment in .venv, puts `mfp` and
# `my-favorite-professor` on your PATH, and opens the app in your browser.
#
# Flags:
#   --no-launch   set up but don't open the app afterwards
#   --no-shims    skip putting commands on your PATH (use .venv/bin directly)
#   --force       replace an existing `mfp` command without asking
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
LAUNCH=1
SHIMS=1
FORCE=0

for arg in "$@"; do
  case "$arg" in
    --no-launch) LAUNCH=0 ;;
    --no-shims)  SHIMS=0 ;;
    --force)     FORCE=1 ;;
    # Print the header comment as the help text, stopping at the first line
    # that isn't a comment -- so editing the header can't desync the help.
    -h|--help)   awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
                 exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  B=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
  RED=$'\033[31m'; OFF=$'\033[0m'
else
  B=""; DIM=""; GREEN=""; YELLOW=""; RED=""; OFF=""
fi

say()  { printf '  %s\n' "$*"; }
step() { printf '\n%s==>%s %s%s%s\n' "$GREEN" "$OFF" "$B" "$*" "$OFF"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$OFF" "$*"; }
die()  { printf '\n  %sx%s %s\n\n' "$RED" "$OFF" "$*" >&2; exit 1; }

printf '\n%smy-favorite-professor%s\n' "$B" "$OFF"
printf '%slearn from material you chose, with a Claude that learns how to explain it to you%s\n' \
  "$DIM" "$OFF"

# ---------------------------------------------------------------- collision
# On macOS the filesystem is case-insensitive, so a checkout at
# ~/code/my-favorite-professor IS ~/code/My-Favorite-Professor -- the default
# location of the material library. Installing into your own library is a mess
# to unpick, so stop before doing any work.
if [ -d "$REPO/.mfp" ] || ls -d "$REPO"/*-professor >/dev/null 2>&1; then
  die "This checkout is sitting inside a material library.

  That happens on macOS when the repo is cloned as 'my-favorite-professor'
  next to a library called 'My-Favorite-Professor' -- the filesystem treats
  those as the same directory.

  Move the checkout somewhere else and run this again:

      cd .. && mv my-favorite-professor my-favorite-professor-app
      cd my-favorite-professor-app && ./install.sh"
fi

# ------------------------------------------------------------------- python
step "Looking for Python 3.12 or newer"

PY=""
for candidate in python3.14 python3.13 python3.12 python3 python; do
  command -v "$candidate" >/dev/null 2>&1 || continue
  if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)' 2>/dev/null; then
    PY="$candidate"; break
  fi
done

if [ -z "$PY" ]; then
  die "Python 3.12+ is required and wasn't found.

  macOS:  brew install python@3.12
  Debian: sudo apt install python3.12 python3.12-venv
  Or:     https://www.python.org/downloads/"
fi
say "$($PY --version) at $(command -v "$PY")"

# ---------------------------------------------------------------- the venv
step "Building the environment"
say "this pulls in a headless browser for saving web pages — give it a minute"

cd "$REPO"
if command -v uv >/dev/null 2>&1; then
  say "using uv"
  uv venv --python "$PY" .venv >/dev/null
  # patchright is the optional stealth tier for stubborn sites; a failure
  # there must not fail the install, since capture degrades without it.
  uv pip install --quiet -e . >/dev/null
  uv pip install --quiet patchright >/dev/null 2>&1 || warn "optional stealth tier skipped"
else
  say "using $PY -m venv (install uv for a faster setup)"
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip >/dev/null
  .venv/bin/python -m pip install --quiet -e . >/dev/null
  .venv/bin/python -m pip install --quiet patchright >/dev/null 2>&1 \
    || warn "optional stealth tier skipped"
fi
say "dependencies installed"

# --------------------------------------------------------------- chromium
step "Installing the headless browser"
say "used to save web pages with their images, and to build PDFs"
if .venv/bin/playwright install chromium >/dev/null 2>&1; then
  say "chromium ready"
else
  warn "chromium install failed — uploads still work, saving web pages may not."
  warn "retry later with: .venv/bin/playwright install chromium"
fi

# ------------------------------------------------------------------- shims
if [ "$SHIMS" = "1" ]; then
  step "Putting the commands on your PATH"
  mkdir -p "$BIN_DIR"

  # An earlier standalone `mfp` (the capture tool this app grew out of) may
  # already own this name. Replacing it is intended -- the app vendors it --
  # but not silently.
  if [ -e "$BIN_DIR/mfp" ] && ! grep -q "$REPO" "$BIN_DIR/mfp" 2>/dev/null; then
    warn "$BIN_DIR/mfp already exists and points somewhere else."
    if [ "$FORCE" = "0" ] && [ -t 0 ]; then
      printf '    Replace it? [y/N] '
      read -r reply </dev/tty || reply=""
      case "$reply" in
        [yY]*) ;;
        *) SHIMS=0; warn "left it alone — use .venv/bin/mfp instead" ;;
      esac
    elif [ "$FORCE" = "0" ]; then
      SHIMS=0
      warn "left it alone — rerun with --force to replace it"
    fi
  fi
fi

if [ "$SHIMS" = "1" ]; then
  for name in mfp my-favorite-professor; do
    cat > "$BIN_DIR/$name" <<EOF
#!/usr/bin/env bash
# my-favorite-professor — installed from $REPO
exec "$REPO/.venv/bin/$name" "\$@"
EOF
    chmod +x "$BIN_DIR/$name"
  done
  say "installed mfp and my-favorite-professor into $BIN_DIR"

  case ":${PATH}:" in
    *":${BIN_DIR}:"*) ON_PATH=1 ;;
    *) ON_PATH=0 ;;
  esac

  if [ "$ON_PATH" = "0" ]; then
    shell_rc="${HOME}/.zshrc"
    [ -n "${BASH_VERSION:-}" ] && shell_rc="${HOME}/.bashrc"
    warn "$BIN_DIR isn't on your PATH. Add it:"
    printf '\n      echo '"'"'export PATH="$HOME/.local/bin:$PATH"'"'"' >> %s\n' "$shell_rc"
    printf '      source %s\n' "$shell_rc"
  fi
fi

# -------------------------------------------------------------------- done
step "Done"
cat <<EOF

  ${B}Open the app${OFF}
      my-favorite-professor serve

  ${B}Save a page while you're reading it${OFF}
      mfp -py https://realpython.com/primer-on-python-decorators/
      mfp -sh https://zsh.sourceforge.io/Guide/

  You'll be asked for an Anthropic API key the first time the app opens.
  It's stored at ~/.config/my-favorite-professor/config.json and never
  leaves your machine except to Anthropic. Get one at:
      ${DIM}https://console.anthropic.com/settings/keys${OFF}

EOF

if [ "$LAUNCH" = "1" ]; then
  say "starting the app — ctrl-c to stop"
  echo
  exec "$REPO/.venv/bin/my-favorite-professor" serve
fi
