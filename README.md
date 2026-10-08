# Resolve Splash Patcher (macOS port)

Replace the DaVinci Resolve splash screens with your own images. Port of
[turboenotak/davinci-resolve-splash-patcher](https://github.com/turboenotak/davinci-resolve-splash-patcher)
(MIT) from Windows to macOS.

> Unofficial tool, not affiliated with Blackmagic Design. It modifies the Resolve executable
> inside `/Applications`; use it at your own risk. **This port has been tested only on a synthetic
> binary, not on a real Resolve for Mac** — see "First run" below.

## Install and run

1. Python 3.10+ (`python3 --version`; if missing: `xcode-select --install` or python.org).
2. Unzip, then double-click **`Start.command`** (first time: right-click → Open, because macOS
   quarantines downloaded scripts). It installs Pillow the first time.
3. The interface opens in your browser (as an app-style window if Chrome/Edge/Brave is installed).
4. Add images, frame them, **close DaVinci Resolve**, click **Apply**. macOS asks for your
   administrator password because the app lives in `/Applications`.

## First run (important)

Before the first Apply, open Terminal in this folder and run:

```bash
python3 splash_patcher.py --inspect
```

It prints what it finds in your Resolve binary (sets, image sizes, universal binary or not).
If it says `locate failed`, the Mac build stores the splash differently and the tool cannot patch
it yet; send me that output and the port can be adjusted.

## What changed vs. the Windows version

| Windows | macOS |
|---|---|
| `Resolve.exe` in Program Files | `/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/MacOS/Resolve` |
| UAC prompt | macOS administrator password prompt |
| Backup of the resource bytes | Backup of the **whole original binary** (kept per Resolve version); Restore puts it back, original Blackmagic signature included |
| — | After patching, the app is **re-signed ad hoc** (`codesign -s -`). Without it macOS kills the modified binary |
| Universal binary | Every architecture slice (arm64 / x86_64) is patched |
| Task Scheduler | Root launch daemon `/Library/LaunchDaemons/com.resolvesplashpatcher.auto.plist` (re-applies after updates) |
| Edge window | Browser window |

Data lives in `~/Library/Application Support/ResolveSplashPatcher` (config, images, backups, log).

## Command line

```bash
sudo python3 splash_patcher.py --apply     # apply the saved settings
sudo python3 splash_patcher.py --restore   # restore the original binary
python3 splash_patcher.py --inspect        # diagnostics
```

## Things that can go wrong on macOS

- **"Operation not permitted" while writing**: macOS (13+) protects app bundles. Give Terminal (or
  the app you launch from) *Full Disk Access* or *App Management* in System Settings → Privacy &
  Security, then try again.
- **Resolve will not open after patching**: run **Restore original** (or reinstall Resolve).
- **Resolve updates**: the update restores the stock splash. Click Apply again, or enable
  "Re-apply after updates" (installs the launch daemon).
- Re-signing replaces Blackmagic's signature with an ad hoc one. Resolve itself should run, but
  features that rely on the original signature (for example some third-party plugins that check
  the host's signature) could be affected. Restore returns everything to the original.

## How it works

The splash screens are Qt resources compiled into the Resolve binary (1x 1110×490 and @2x
2220×980). The patcher finds the resource tables by signature, treats the old splash PNGs as free
space, writes your lossless PNGs there and repoints the table. See the original project for details.

## License

MIT, same as the original project.
