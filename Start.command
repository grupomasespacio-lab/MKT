#!/bin/bash
# Resolve Splash Patcher launcher (macOS): checks Python and Pillow, then opens the interface
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.10+ is required. Install it from https://www.python.org/downloads/macos/"
  echo "(or run: xcode-select --install)"
  read -n 1 -s -r -p "Press any key to close"; exit 1
fi

python3 -c "import sys; sys.exit(sys.version_info < (3, 10))" || {
  echo "Python 3.10 or newer is required. Update it from https://www.python.org/downloads/macos/"
  read -n 1 -s -r -p "Press any key to close"; exit 1
}

python3 -c "import PIL" 2>/dev/null || {
  echo "Installing Pillow..."
  python3 -m pip install --user -r requirements.txt || { echo "Failed to install Pillow"; read -n 1 -s -r -p "Press any key to close"; exit 1; }
}

python3 splash_patcher.py
