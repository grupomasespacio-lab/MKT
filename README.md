# Resolve Splash Patcher (versión para macOS)

Cambia la pantalla de inicio (splash) de DaVinci Resolve por tus propias imágenes.
Adaptación para Mac de [turboenotak/davinci-resolve-splash-patcher](https://github.com/turboenotak/davinci-resolve-splash-patcher) (licencia MIT).

> Herramienta no oficial, sin relación con Blackmagic Design. Modifica el ejecutable de Resolve dentro de
> `/Applications`; úsala bajo tu responsabilidad. Se probó con un binario sintético, no con un Resolve real de Mac.

## Cómo cambiar el splash (resumen)

1. Cierra DaVinci Resolve por completo.
2. Abre `Abrir.command` (doble clic; la primera vez, clic derecho → Abrir). Se abre la interfaz en tu navegador.
   Deja abierta la ventana de Terminal mientras la uses.
3. Pulsa **Agregar imágenes** y elige tus fotos o logos. Sirve cualquier tamaño.
4. Acomoda cada imagen: arrástrala para moverla y usa la rueda para acercar.
5. Deja activado **Oscurecer el lado izquierdo** para que el texto de Resolve se lea bien.
6. Pulsa **Aplicar** e ingresa tu contraseña de Mac. Espera el mensaje **Listo**.
7. Abre DaVinci Resolve: verás tu imagen al iniciar.

Para volver al original, pulsa **Restaurar original**. Si Resolve se actualiza, el splash original regresa: pulsa Aplicar de nuevo.
La interfaz también tiene un botón **Guía** con estos pasos.

## Si macOS bloquea la escritura

En Ajustes del Sistema → Privacidad y seguridad, da a Terminal «Acceso total al disco» o «Administración de apps».

## Diferencias con la versión de Windows

- Edita `/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/MacOS/Resolve`.
- Guarda una copia del binario original completo (por versión) y «Restaurar» la devuelve tal cual.
- Después de parchear, vuelve a firmar la app con firma ad hoc (`codesign -s -`); macOS lo exige.
- Si el binario es universal (arm64 + x86_64), parchea ambas arquitecturas.
- La reaplicación automática usa un daemon de launchd en lugar del Programador de tareas.
- Los datos están en `~/Library/Application Support/ResolveSplashPatcher` (ajustes, imágenes, respaldos y `patcher.log`).

## Línea de comandos

```bash
sudo ./ResolveSplashPatcher --apply     # aplicar los ajustes guardados
sudo ./ResolveSplashPatcher --restore   # restaurar el original
./ResolveSplashPatcher --inspect        # diagnóstico: qué encuentra en tu Resolve
```

## Licencia

MIT, igual que el proyecto original.
