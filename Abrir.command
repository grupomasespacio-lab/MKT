#!/bin/bash
# Doble clic para abrir Resolve Splash Patcher (no requiere Python)
cd "$(dirname "$0")"
xattr -cr . 2>/dev/null
chmod +x ./ResolveSplashPatcher

echo "Resolve Splash Patcher"
echo "Para modificar DaVinci Resolve se necesita la contraseña de tu Mac (no se muestra al escribirla)."
sudo -v || { echo "No se pudo obtener permiso de administrador."; read -n 1 -s -r -p "Presiona una tecla para cerrar"; exit 1; }
# mantener viva la sesión de administrador mientras este programa esté abierto
( while kill -0 $$ 2>/dev/null; do sudo -n true 2>/dev/null; sleep 40; done ) &

export RSP_SUDO=1
./ResolveSplashPatcher
echo
read -n 1 -s -r -p "Presiona una tecla para cerrar"
