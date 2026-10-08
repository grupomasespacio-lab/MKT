#!/bin/bash
# Doble clic para abrir Resolve Splash Patcher (no requiere Python)
cd "$(dirname "$0")"
xattr -cr . 2>/dev/null
chmod +x ./ResolveSplashPatcher
./ResolveSplashPatcher
echo
read -n 1 -s -r -p "Presiona una tecla para cerrar"
