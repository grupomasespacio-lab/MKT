"""
DaVinci Resolve Splash Patcher
==============================

Replaces the startup splash screens of DaVinci Resolve (macOS) with your own images.

How it works
------------
The splash images are not loose files: they are compiled into Resolve.exe as Qt
resources. There are two independent resource sets:
  1x  ":/Application/Misc/ResolveSplashScreenN"      1110x490  (+16 unused _Linux variants)
  * 2x  ":/Application/Misc/ResolveSplashScreenN@2x"   2220x980  (used on Retina displays)
If the Mach-O binary is universal (arm64 + x86_64) every slice carries its own copy of the
resources; all copies are patched.
A Qt resource consists of three tables: data (each entry = 4-byte big-endian size + raw
bytes), names (UTF-16BE) and a tree whose file nodes store an offset into the data table.

Instead of squeezing a new PNG into the exact byte budget of the original (the manual
hex-editor approach), the patcher:
  1. locates the tables by signature (no hard-coded offsets -> survives updates),
  2. treats the data of every splash entry as free space, merging neighbouring entries
     into large contiguous regions,
  3. packs the newly rendered PNGs into that space and rewrites the tree offsets.
So images stay lossless in almost every case; palette quantisation is only a fallback.

Before the first patch the ORIGINAL BINARY is copied to
~/Library/Application Support/ResolveSplashPatcher/backups, so "Restore" brings back the
untouched file (with Blackmagic's own signature) and re-patching always starts from it.
After patching, the app bundle is re-signed ad hoc (required by macOS, otherwise the
modified binary is killed on launch).

Usage
-----
  python3 splash_patcher.py            interface (opens in your browser)
  python3 splash_patcher.py --apply    apply the saved configuration (run with sudo)
  python3 splash_patcher.py --auto     like --apply, but only if Resolve is not patched yet
                                       (used by the launch daemon after updates)
  python3 splash_patcher.py --restore  restore the original binary (run with sudo)
  python3 splash_patcher.py --inspect  print what was found in the binary (diagnostics)
"""

import io
import json
import mmap
import os
import plistlib
import re
import shlex
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import zlib
from dataclasses import dataclass, field

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover
    print("Pillow is not installed. Install it with:\n\n  python3 -m pip install --user pillow\n")
    sys.exit(1)

APP_NAME = "ResolveSplashPatcher"
APP_DIR = os.environ.get("RSP_APPDIR") or os.path.join(
    os.path.expanduser("~"), "Library", "Application Support", APP_NAME)
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
IMAGES_DIR = os.path.join(APP_DIR, "images")
BACKUP_DIR = os.path.join(APP_DIR, "backups")
LOG_PATH = os.path.join(APP_DIR, "patcher.log")
DEFAULT_EXE = "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/MacOS/Resolve"
TASK_NAME = "com.resolvesplashpatcher.auto"
DAEMON_PLIST = f"/Library/LaunchDaemons/{TASK_NAME}.plist"
FROZEN = bool(getattr(sys, "frozen", False))      # built with PyInstaller (no Python needed)
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

PNG_SIG = b"\x89PNG\r\n\x1a\n"
IMAGE_SIGS = (PNG_SIG, b"\xff\xd8\xff", b"GIF8", b"BM")
MARKER = b"RSPATCH1"
# resource set id -> name of its first splash entry (used as anchor to find the tables)
GROUPS = (("1x", "ResolveSplashScreen1"), ("2x", "ResolveSplashScreen1@2x"))
WIN_SLOT_RE = re.compile(r"^ResolveSplashScreen(\d+)(?:@2x)?$")
LINUX_SLOT_RE = re.compile(r"^ResolveSplashScreen_Linux(\d+)(?:@2x)?$")
# Colour and shape of the dark panel on the left of the stock splash screens (measured on
# the original images: a near-solid navy panel up to ~30% of the width, faded out by ~62%).
DARK_RGB = (24, 29, 37)
DARK_ALPHA = 0.94
DARK_SOLID, DARK_END = 0.30, 0.62



def log(msg):
    os.makedirs(APP_DIR, exist_ok=True)
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if sys.stdout:
        try:
            print(line)
        except Exception:
            pass


class PatchError(Exception):
    pass


# --------------------------------------------------------------------------------------
# Localisation of messages shown to the user (the interface has its own string table)
# --------------------------------------------------------------------------------------

LANG = "en"
MESSAGES = {
    "zstd": ("zstd-compressed resources are not supported", "zstd-сжатые ресурсы не поддерживаются"),
    "no_data_table": ("Could not find the resource data table", "Не удалось найти таблицу данных ресурсов"),
    "no_resources": ("No splash screen resources found in the Resolve binary. The format may differ on macOS; run --inspect. "
                     "Resolve version.", "В Resolve.exe не найдены ресурсы заставки. Возможно, формат изменился "
                     "в новой версии Resolve."),
    "close_for_restore": ("Close DaVinci Resolve before restoring.", "Закройте DaVinci Resolve перед восстановлением."),
    "no_backup": ("No backup found for this Resolve version. Repair Resolve with its installer.",
                  "Резервная копия для этой версии не найдена. Восстановите Resolve через установщик (Repair)."),
    "restoring": ("Restoring the original splash screens…", "Восстановление оригинальных заставок…"),
    "restore_failed": ("Restore failed: the patch marker is still present",
                       "Восстановление не удалось: метка патча всё ещё на месте"),
    "restored": ("Original splash screens restored", "Оригинальные заставки восстановлены"),
    "bad_image": ("Could not open image “{name}”", "Не удалось открыть изображение «{name}»"),
    "no_marker_space": ("No room left for the patch marker", "Нет места для служебной метки"),
    "not_found": ("File not found: {path}", "Файл не найден: {path}"),
    "resolve_running": ("DaVinci Resolve is running. Close it and try again.",
                        "DaVinci Resolve запущен. Закройте его и попробуйте снова."),
    "no_images": ("No images selected", "Не выбрано ни одного изображения"),
    "analyzing": ("Reading Resolve…", "Анализ Resolve…"),
    "patched_no_backup": ("Resolve is already patched but the backup is missing. "
                          "Reinstall Resolve.",
                          "Resolve.exe уже пропатчен, но резервная копия не найдена. "
                          "Восстановите Resolve через установщик (Repair)."),
    "rollback": ("Returning to the original layout…", "Возврат к исходной раскладке…"),
    "rollback_failed": ("Could not return to the original resource layout",
                        "Не удалось вернуть исходную раскладку ресурсов"),
    "all_original": ("Every slot is set to Original, nothing to apply",
                     "Все слоты оставлены оригинальными, применять нечего"),
    "preparing": ("Preparing {name} ({group})", "Подготовка {name} ({group})"),
    "no_space": ("The images don't fit even after compression. Use fewer images.",
                 "Изображения не помещаются даже после сжатия. Выберите меньше картинок."),
    "quantizing": ("Low on space, reducing colours: {name}", "Мало места, сжимаю палитрой: {name}"),
    "writing": ("Writing Resolve…", "Запись в Resolve…"),
    "verifying": ("Verifying…", "Проверка…"),
    "verify_marker": ("Verification failed: no marker in set {group}",
                      "Проверка не пройдена: нет метки в наборе {group}"),
    "verify_slot": ("Verification failed for slot {num} ({group})", "Проверка не пройдена для слота {num} ({group})"),
    "done": ("Done", "Готово"),
    "task_failed": ("Could not create the scheduled task: {err}", "Не удалось создать задачу: {err}"),
    "uac_denied": ("Administrator permission was declined", "Запрос прав администратора отклонён"),
    "uac_failed": ("Could not start the process as administrator",
                   "Не удалось запустить процесс с правами администратора"),
    "exe_missing": ("Resolve binary not found. Choose its location.", "Resolve не найден. Укажите путь к нему."),
    "busy": ("Another operation is already running", "Уже выполняется другая операция"),
    "waiting_admin": ("Waiting for administrator permission…", "Ожидание прав администратора…"),
    "op_failed": ("The operation failed (see patcher.log)", "Операция завершилась с ошибкой (см. patcher.log)"),
    "sign_failed": ("Re-signing the app failed: {err}", "Не удалось переподписать приложение: {err}"),
    "pick_exe": ("Choose the Resolve binary", "Выберите бинарный файл Resolve"),
}


def tr(key, **kw):
    en, ru = MESSAGES[key]
    return (ru if LANG == "ru" else en).format(**kw)


def set_lang(lang):
    global LANG
    LANG = "ru" if lang == "ru" else "en"


# --------------------------------------------------------------------------------------
# Qt resource parsing
# --------------------------------------------------------------------------------------

def qt_hash(name):
    h = 0
    for ch in name:
        h = ((h << 4) + ord(ch)) & 0xFFFFFFFF
        h ^= (h & 0xF0000000) >> 23
        h &= 0x0FFFFFFF
    return h


def read_name_entry(buf, pos):
    """Returns (name, entry_length) if a valid Qt name entry starts at pos, else None."""
    if pos < 0 or pos + 6 > len(buf):
        return None
    length, h = struct.unpack(">HI", buf[pos:pos + 6])
    if length == 0 or length > 512 or pos + 6 + 2 * length > len(buf):
        return None
    try:
        name = buf[pos + 6:pos + 6 + 2 * length].decode("utf-16-be")
    except UnicodeDecodeError:
        return None
    if qt_hash(name) != h:
        return None
    return name, 6 + 2 * length


@dataclass
class Node:
    index: int
    pos: int          # absolute file offset of the tree node
    name: str
    flags: int
    data_off: int     # offset relative to the data table (file nodes only)


@dataclass
class Layout:
    group: str
    tree: int
    names: int
    data: int
    node_size: int
    node_count: int
    files: list = field(default_factory=list)

    def entry_start(self, node):
        return self.data + node.data_off

    def entry_size(self, buf, node):
        s = self.entry_start(node)
        return 4 + struct.unpack(">I", buf[s:s + 4])[0]

    def read(self, buf, node):
        start = self.entry_start(node)
        size = struct.unpack(">I", buf[start:start + 4])[0]
        raw = bytes(buf[start + 4:start + 4 + size])
        if node.flags & 1:  # zlib: 4-byte uncompressed size + zlib stream
            return zlib.decompress(raw[4:])
        if node.flags & 4:
            raise PatchError(tr("zstd"))
        return raw

    def _slots(self, rx):
        out = {}
        for n in self.files:
            m = rx.match(n.name)
            if m:
                out[int(m.group(1))] = n
        return dict(sorted(out.items()))

    def win_slots(self):
        return self._slots(WIN_SLOT_RE)

    def linux_slots(self):
        return self._slots(LINUX_SLOT_RE)

    def tree_range(self):
        return self.tree, self.tree + self.node_size * self.node_count


def _walk_tree(buf, tree, node_size, limit=100000):
    """Walk a Qt resource tree; returns list of (index, pos, name_off, flags, data_off|None)."""
    def raw(i):
        p = tree + node_size * i
        if p + node_size > len(buf):
            raise ValueError
        name_off, flags = struct.unpack(">IH", buf[p:p + 6])
        if flags & 2:
            count, first = struct.unpack(">II", buf[p + 6:p + 14])
            return p, name_off, flags, (count, first)
        _country, _lang, data_off = struct.unpack(">HHI", buf[p + 6:p + 14])
        return p, name_off, flags, data_off

    out, stack, seen = [], [0], set()
    while stack:
        i = stack.pop()
        if i in seen or i > limit:
            raise ValueError
        seen.add(i)
        p, name_off, flags, extra = raw(i)
        if flags & ~0x7:
            raise ValueError
        if flags & 2:
            count, first = extra
            if count == 0 or count > 10000 or first <= i or first + count > limit:
                raise ValueError
            stack.extend(range(first, first + count))
            out.append((i, p, name_off, flags, None))
        else:
            out.append((i, p, name_off, flags, extra))
    return out


def _validate_data(buf, data, names, by_off):
    for i, n in enumerate(by_off):
        start = data + n.data_off
        if start + 8 > names:
            return False
        size = struct.unpack(">I", buf[start:start + 4])[0]
        limit = data + by_off[i + 1].data_off if i + 1 < len(by_off) else names
        if start + 4 + size > limit:
            return False
        head = bytes(buf[start + 4:start + 12])
        if n.flags & 1:
            if size < 6 or buf[start + 8] != 0x78:
                return False
        elif not head.startswith(IMAGE_SIGS):
            if WIN_SLOT_RE.match(n.name) or LINUX_SLOT_RE.match(n.name):
                return False
    return True


def _find_data_table(buf, names, files):
    """The last image signature before the names table belongs to some entry k, so
    data = pos - 4 - k.data_off; the candidate is validated against the whole table."""
    lo = max(0, names - 256 * 1024 * 1024)
    by_off = sorted({n.data_off: n for n in files}.values(), key=lambda n: n.data_off)
    for sig in (PNG_SIG, b"\xff\xd8\xff"):
        p = buf.rfind(sig, lo, names)
        if p < 0:
            continue
        for cand in sorted(files, key=lambda n: -n.data_off):
            data = p - 4 - cand.data_off
            if data >= 0 and _validate_data(buf, data, names, by_off):
                return data
    raise PatchError(tr("no_data_table"))


def locate(buf, group, anchor_name, start=0):
    """Locate the Qt resource containing `anchor_name` (searching from `start`). Works on
    original and on previously patched executables (name and tree tables never move)."""
    anchor = anchor_name.encode("utf-16-be")
    search = start
    while True:
        p = buf.find(anchor, search)
        if p < 0:
            return None
        search = p + 1
        entry = p - 6
        r = read_name_entry(buf, entry)
        if not r or r[0] != anchor_name:
            continue
        q = entry
        while True:
            r = read_name_entry(buf, q)
            if not r:
                break
            q += r[1]
        names_end = q
        for node_size in (22, 14):
            for tree in range(names_end, names_end + 512):
                if struct.unpack(">IH", buf[tree:tree + 6]) != (0, 2):
                    continue
                if struct.unpack(">I", buf[tree + 10:tree + 14])[0] != 1:
                    continue
                try:
                    nodes = _walk_tree(buf, tree, node_size)
                except (ValueError, struct.error):
                    continue
                for cand in (n for n in nodes if n[4] is not None):
                    names = entry - cand[2]
                    if names < 0:
                        continue
                    named, ok = [], True
                    for (i, pos, name_off, flags, data_off) in nodes:
                        if i == 0:
                            continue
                        r = read_name_entry(buf, names + name_off)
                        if not r:
                            ok = False
                            break
                        named.append(Node(i, pos, r[0], flags, data_off))
                    if not ok or not any(n.name == anchor_name for n in named):
                        continue
                    files = [n for n in named if n.data_off is not None]
                    data = _find_data_table(buf, names, files)
                    return Layout(group=group, tree=tree, names=names, data=data,
                                  node_size=node_size, node_count=max(n[0] for n in nodes) + 1,
                                  files=files)


def locate_all(buf):
    """{group id: Layout}. A universal Mach-O contains one copy of each set per architecture;
    extra copies get ids like "1x#2"."""
    out = {}
    for gid, anchor in GROUPS:
        start, n = 0, 0
        while True:
            lay = locate(buf, gid if n == 0 else f"{gid}#{n + 1}", anchor, start)
            if not lay:
                break
            if lay.win_slots():
                out[lay.group] = lay
                n += 1
            start = lay.tree_range()[1]
            if n >= 4:
                break
    if not out:
        raise PatchError(tr("no_resources"))
    return out


# --------------------------------------------------------------------------------------
# Executable helpers
# --------------------------------------------------------------------------------------

def app_bundle(exe_path):
    """Path of the .app bundle that contains the executable (or None)."""
    p = os.path.abspath(exe_path)
    while p and p != os.path.dirname(p):
        if p.endswith(".app"):
            return p
        p = os.path.dirname(p)
    return None


def file_version(path):
    b = app_bundle(path)
    if b:
        try:
            with open(os.path.join(b, "Contents", "Info.plist"), "rb") as f:
                pl = plistlib.load(f)
            v = pl.get("CFBundleShortVersionString") or "unknown"
            bv = pl.get("CFBundleVersion")
            return f"{v}.{bv}" if bv and bv != v else v
        except Exception:
            pass
    return "unknown"


def backup_key(exe_path, version=None):
    # the executable's size changes after ad hoc re-signing, so the key is the version only
    return re.sub(r"[^0-9A-Za-z._-]", "_", version or file_version(exe_path))


def backup_dir(key, group):
    # the 1x set lives in the root of the version folder (compatible with older backups)
    d = os.path.join(BACKUP_DIR, key)
    return d if group == "1x" else os.path.join(d, group.replace("#", "_"))


def full_backup_path(key):
    return os.path.join(BACKUP_DIR, key, "Resolve.orig")


def resolve_running():
    try:
        r = subprocess.run(["pgrep", "-x", "Resolve"], capture_output=True, text=True)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def is_admin():
    return os.geteuid() == 0


def chown_app_dir():
    """When running as root for a normal user, give APP_DIR back to that user."""
    uid = os.environ.get("RSP_UID")
    if os.geteuid() != 0 or not uid:
        return
    try:
        gid = int(os.environ.get("RSP_GID", uid))
        for root, dirs, files in os.walk(APP_DIR):
            os.chown(root, int(uid), gid)
            for n in files:
                os.chown(os.path.join(root, n), int(uid), gid)
    except Exception:
        pass


def ensure_full_backup(exe_path, key):
    """Copy of the pristine binary (made once per Resolve version, before the first patch)."""
    dst = full_backup_path(key)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.isfile(dst):
        return dst
    with open(exe_path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        patched = any(find_marker(mm, lay) is not None for lay in locate_all(mm).values())
    if patched:
        raise PatchError(tr("patched_no_backup"))
    shutil.copyfile(exe_path, dst + ".tmp")
    os.replace(dst + ".tmp", dst)
    with open(os.path.join(os.path.dirname(dst), "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"version": key, "size": os.path.getsize(dst),
                   "created": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
    log(f"Full backup created: {dst}")
    return dst


def reset_to_original(exe_path, key):
    """Put the pristine binary (and its original signature) back."""
    src = full_backup_path(key)
    if not os.path.isfile(src):
        raise PatchError(tr("no_backup"))
    shutil.copyfile(src, exe_path)
    os.chmod(exe_path, 0o755)


def resign(exe_path):
    """Re-sign the app bundle ad hoc. No hardened runtime flag, so library validation stays
    off and Blackmagic's own libraries keep loading."""
    b = app_bundle(exe_path)
    if not b:
        return
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", b], capture_output=True)
    r = subprocess.run(["codesign", "--force", "--sign", "-", "--preserve-metadata=entitlements", b],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise PatchError(tr("sign_failed", err=(r.stderr or r.stdout).strip()))
    log("App re-signed (ad hoc)")


def find_marker(buf, layout):
    p = buf.find(MARKER, layout.data, layout.names)
    if p < 0:
        return None
    length = struct.unpack(">I", buf[p + 8:p + 12])[0]
    try:
        return json.loads(bytes(buf[p + 12:p + 12 + length]).decode("utf-8"))
    except Exception:
        return {}


@dataclass
class ExeInfo:
    path: str
    version: str
    key: str
    layouts: dict            # group -> Layout
    markers: dict            # group -> marker JSON or None

    @property
    def state(self):
        n = sum(1 for m in self.markers.values() if m is not None)
        return "original" if n == 0 else ("patched" if n == len(self.markers) else "partial")

    def backup_ok(self, group):
        return os.path.isfile(full_backup_path(self.key))

    @property
    def patched_time(self):
        return next((m.get("time") for m in self.markers.values() if m), None)


def inspect_exe(path):
    version = file_version(path)
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        layouts = locate_all(mm)
        markers = {g: find_marker(mm, lay) for g, lay in layouts.items()}
    return ExeInfo(path, version, backup_key(path, version), layouts, markers)


# --------------------------------------------------------------------------------------
# Backup / restore
# --------------------------------------------------------------------------------------

def _splash_spans(buf, layout):
    nodes = list(layout.win_slots().values()) + list(layout.linux_slots().values())
    return [(layout.entry_start(n), layout.entry_start(n) + layout.entry_size(buf, n)) for n in nodes]


def create_backup(buf, layout, key, version, size):
    d = backup_dir(key, layout.group)
    os.makedirs(d, exist_ok=True)
    t0, t1 = layout.tree_range()
    spans = _splash_spans(buf, layout)
    d0, d1 = min(s for s, _ in spans), max(e for _, e in spans)
    with open(os.path.join(d, "tree.bin"), "wb") as f:
        f.write(buf[t0:t1])
    with open(os.path.join(d, "data.bin"), "wb") as f:
        f.write(buf[d0:d1])
    for num, node in layout.win_slots().items():
        with open(os.path.join(d, f"orig_{num:02d}.png"), "wb") as f:
            f.write(layout.read(buf, node))
    meta = {"version": version, "size": size, "group": layout.group,
            "tree": [t0, t1], "data": [d0, d1], "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    log(f"Backup created: {d}")


def load_backup(key, group, size):
    d = backup_dir(key, group)
    try:
        with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        with open(os.path.join(d, "tree.bin"), "rb") as f:
            tree = f.read()
        with open(os.path.join(d, "data.bin"), "rb") as f:
            data = f.read()
    except OSError:
        return None
    if meta["size"] != size:
        return None
    return meta, tree, data


def write_backup_into(fh, backup):
    meta, tree, data = backup
    fh.seek(meta["tree"][0])
    fh.write(tree)
    fh.seek(meta["data"][0])
    fh.write(data)


def original_slot_pngs(info):
    """{slot: png bytes} of the original 1x splash screens (from the pristine backup if patched)."""
    group = "1x" if "1x" in info.layouts else next(iter(info.layouts))
    layout = info.layouts[group]
    path = info.path
    if info.markers[group] is not None:
        path = full_backup_path(info.key)
        if not os.path.isfile(path):
            return {}
    out = {}
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        lay = locate_all(mm).get(group)
        if lay:
            for num, node in lay.win_slots().items():
                out[num] = lay.read(mm, node)
    return out


def restore_original(exe_path, progress=lambda m, f=None: None):
    if resolve_running():
        raise PatchError(tr("close_for_restore"))
    info = inspect_exe(exe_path)
    if info.state == "original":
        log("Resolve is not patched – nothing to restore")
        return {"restored": 0}
    progress(tr("restoring"), 0.4)
    reset_to_original(exe_path, info.key)
    if inspect_exe(exe_path).state != "original":
        raise PatchError(tr("restore_failed"))
    log("Original binary restored")
    progress(tr("restored"), 1.0)
    return {"restored": len(info.layouts)}


# --------------------------------------------------------------------------------------
# Rendering (the interface mirrors this math in JavaScript for the live preview)
# --------------------------------------------------------------------------------------

def load_source(path, max_side=5000):
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (0, 0, 0, 255))
        bg.alpha_composite(im)
        im = bg
    im = im.convert("RGB")
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    return im


def template_geometry(template):
    """Bounding box of the opaque photo area inside the original splash (shadow excluded)."""
    a = template.getchannel("A")
    box = a.point(lambda v: 255 if v == 255 else 0).getbbox()
    return box or (0, 0) + template.size


def crop_box(sw, sh, w, h, fx, fy, zoom):
    aspect = w / h
    if sw / sh > aspect:
        ch, cw = sh, sh * aspect
    else:
        cw, ch = sw, sw / aspect
    zoom = max(1.0, zoom)
    cw, ch = cw / zoom, ch / zoom
    left, top = (sw - cw) * fx, (sh - ch) * fy
    return left, top, left + cw, top + ch


def darken_alpha(t):
    """Opacity of the left panel at relative x position t (0..1). Mirrored in ui.html."""
    if t <= DARK_SOLID:
        return DARK_ALPHA
    if t >= DARK_END:
        return 0.0
    u = (t - DARK_SOLID) / (DARK_END - DARK_SOLID)
    return DARK_ALPHA * (1 - u * u * (3 - 2 * u))   # smoothstep fade


def render_splash(src, template, fx=0.5, fy=0.5, zoom=1.0, darken=True):
    """src: RGB image. template: original RGBA splash (gives size, shadow and rounded corners)."""
    template = template.convert("RGBA")
    x0, y0, x1, y1 = template_geometry(template)
    w, h = x1 - x0, y1 - y0
    photo = src.resize((w, h), Image.LANCZOS, box=crop_box(*src.size, w, h, fx, fy, zoom))
    if darken:
        grad = Image.new("L", (w, 1))
        grad.putdata([round(255 * darken_alpha((x + 0.5) / w)) for x in range(w)])
        grad = grad.resize((w, h))
        photo = Image.composite(Image.new("RGB", (w, h), DARK_RGB), photo, grad)
    out = template.copy()
    out.paste(photo, (x0, y0))
    out.putalpha(template.getchannel("A"))
    return out


def encode_png(img, quantize=False):
    if quantize:
        img = img.quantize(256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.FLOYDSTEINBERG)
    bio = io.BytesIO()
    img.save(bio, "PNG", compress_level=7)
    return bio.getvalue()


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------

def default_config():
    return {"exe": DEFAULT_EXE, "images": [], "slots": {}, "darken": True, "lang": "en"}


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        base = default_config()
        base.update(cfg)
        set_lang(base.get("lang"))
        return base
    except (OSError, ValueError):
        return default_config()


def save_config(cfg):
    os.makedirs(APP_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)


def import_image_bytes(data, filename):
    """Stores a user image in the app folder so the auto-patcher still has it later."""
    os.makedirs(IMAGES_DIR, exist_ok=True)
    img_id = uuid.uuid4().hex[:10]
    ext = os.path.splitext(filename)[1].lower() or ".png"
    dst = os.path.join(IMAGES_DIR, img_id + ext)
    with open(dst, "wb") as f:
        f.write(data)
    try:
        with Image.open(dst) as im:
            im.verify()
    except Exception:
        os.remove(dst)
        raise PatchError(tr("bad_image", name=filename))
    return {"id": img_id, "file": dst, "name": filename, "fx": 0.5, "fy": 0.5, "zoom": 1.0}


def import_image(path):
    with open(path, "rb") as f:
        return import_image_bytes(f.read(), os.path.basename(path))


def effective_slots(cfg, slot_numbers):
    """slot -> image id (None = keep original). 'auto' slots are filled round-robin."""
    ids = [im["id"] for im in cfg["images"]]
    out = {}
    for i, num in enumerate(slot_numbers):
        v = cfg["slots"].get(str(num), "auto")
        if v == "auto":
            out[num] = ids[i % len(ids)] if ids else None
        elif v in ids:
            out[num] = v
        else:
            out[num] = None
    return out


# --------------------------------------------------------------------------------------
# Patching
# --------------------------------------------------------------------------------------

def _free_regions(buf, layout, keep_nodes):
    """Contiguous runs of splash entries that may be overwritten."""
    keep_offs = {n.data_off for n in keep_nodes}
    spans = sorted({(layout.entry_start(n), layout.entry_start(n) + layout.entry_size(buf, n), n.name)
                    for n in layout.files})
    regions, cur = [], None
    for start, end, name in spans:
        free = (WIN_SLOT_RE.match(name) or LINUX_SLOT_RE.match(name)) and \
               (start - layout.data) not in keep_offs
        if free:
            if cur and start <= cur[1] + 16:
                cur[1] = max(cur[1], end)
            else:
                cur = [start, end]
                regions.append(cur)
        else:
            cur = None
    return [tuple(r) for r in regions]


def _pack(blobs, regions):
    """First-fit decreasing. blobs: {id: bytes}. Returns {id: abs_pos} or None."""
    free = [[s, e] for s, e in regions]
    placed = {}
    for bid, blob in sorted(blobs.items(), key=lambda kv: -len(kv[1])):
        need = 4 + len(blob)
        for r in sorted(free, key=lambda r: r[1] - r[0]):
            if r[1] - r[0] >= need:
                placed[bid] = r[0]
                r[0] += need
                break
        else:
            return None
    return placed


def _write_group(fh, layout, assignment, regions, blobs, placed, meta):
    win, lin = layout.win_slots(), layout.linux_slots()
    for s, e in regions:                      # no stale PNGs left behind
        fh.seek(s)
        fh.write(b"\0" * (e - s))
    for img_id, pos in placed.items():
        fh.seek(pos)
        fh.write(struct.pack(">I", len(blobs[img_id])) + blobs[img_id])

    def point(node, flags, data_off):
        fh.seek(node.pos + 4)
        fh.write(struct.pack(">H", flags))
        fh.seek(node.pos + 10)
        fh.write(struct.pack(">I", data_off))

    first_used = next(i for i in assignment.values() if i is not None)
    for num, node in win.items():
        if assignment[num] is not None:
            point(node, 0, placed[assignment[num]] - layout.data)
    for num, node in lin.items():
        # Linux variants are never shown on Windows; point them at the matching slot
        if num in win and assignment[num] is None:
            point(node, win[num].flags, win[num].data_off)
        else:
            point(node, 0, placed[assignment.get(num) or first_used] - layout.data)

    rec = MARKER + struct.pack(">I", len(meta)) + meta
    used_end = {pos: pos + 4 + len(blobs[i]) for i, pos in placed.items()}
    for s, e in regions:
        ends = [x for p, x in used_end.items() if s <= p < e]
        free_from = max(ends) if ends else s
        if e - free_from >= len(rec):
            fh.seek(free_from)
            fh.write(rec)
            return
    raise PatchError(tr("no_marker_space"))


def apply_patch(cfg, progress=lambda msg, frac=None: None):
    exe = cfg["exe"]
    if not os.path.isfile(exe):
        raise PatchError(tr("not_found", path=exe))
    if resolve_running():
        raise PatchError(tr("resolve_running"))
    if not cfg["images"]:
        raise PatchError(tr("no_images"))
    progress(tr("analyzing"), 0.02)
    version = file_version(exe)
    key = backup_key(exe, version)

    # 1. keep a pristine copy of the binary; always start from it
    ensure_full_backup(exe, key)
    progress(tr("rollback"), 0.05)
    reset_to_original(exe, key)

    # 2. read templates and free space of every set
    plan = {}
    with open(exe, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        layouts = locate_all(mm)
        for g, lay in layouts.items():
            if find_marker(mm, lay) is not None:
                raise PatchError(tr("rollback_failed"))
            win = lay.win_slots()
            template = Image.open(io.BytesIO(lay.read(mm, next(iter(win.values()))))).convert("RGBA")
            assignment = effective_slots(cfg, list(win))
            keep = [win[n] for n, v in assignment.items() if v is None]
            plan[g] = {"layout": lay, "template": template, "assignment": assignment,
                       "regions": _free_regions(mm, lay, keep)}

    images = {im["id"]: im for im in cfg["images"]}
    used = sorted({i for p in plan.values() for i in p["assignment"].values() if i is not None},
                  key=lambda i: list(images).index(i))
    if not used:
        raise PatchError(tr("all_original"))

    # 3. render every used image for every set
    total_steps = len(used) * len(plan)
    step = 0
    for k, img_id in enumerate(used):
        im = images[img_id]
        src = load_source(im["file"])
        for g, p in plan.items():
            step += 1
            progress(tr("preparing", name=im["name"], group=g), 0.08 + 0.72 * step / total_steps)
            if img_id not in p["assignment"].values():
                continue
            out = render_splash(src, p["template"], im["fx"], im["fy"], im["zoom"], cfg.get("darken", True))
            p.setdefault("rendered", {})[img_id] = out
            p.setdefault("blobs", {})[img_id] = encode_png(out)

    # 4. pack (quantise the heaviest images only if they don't fit)
    quantized = set()
    for g, p in plan.items():
        p["placed"] = _pack(p["blobs"], p["regions"])
        while p["placed"] is None:
            cand = [i for i in sorted(p["blobs"], key=lambda i: -len(p["blobs"][i]))
                    if (g, i) not in quantized]
            if not cand:
                raise PatchError(tr("no_space"))
            progress(tr("quantizing", name=images[cand[0]]["name"]), 0.82)
            p["blobs"][cand[0]] = encode_png(p["rendered"][cand[0]], quantize=True)
            quantized.add((g, cand[0]))
            p["placed"] = _pack(p["blobs"], p["regions"])

    # 5. write
    progress(tr("writing"), 0.88)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(exe, "r+b") as fh:
        for g, p in plan.items():
            meta = json.dumps({"tool": APP_NAME, "time": stamp, "version": version, "group": g,
                               "slots": {str(k): v for k, v in p["assignment"].items()}}).encode()
            _write_group(fh, p["layout"], p["assignment"], p["regions"], p["blobs"], p["placed"], meta)

    # 6. verify: every slot must decode to an image of the original size
    progress(tr("verifying"), 0.93)
    with open(exe, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        layouts = locate_all(mm)
        for g, lay in layouts.items():
            if find_marker(mm, lay) is None:
                raise PatchError(tr("verify_marker", group=g))
            for num, node in lay.win_slots().items():
                img = Image.open(io.BytesIO(lay.read(mm, node)))
                img.load()
                if img.size != plan[g]["template"].size:
                    raise PatchError(tr("verify_slot", num=num, group=g))

    progress("Re-signing…", 0.97)
    try:
        resign(exe)
    except PatchError:
        reset_to_original(exe, key)        # never leave a broken, unsigned binary behind
        raise

    stats = {g: {"bytes": sum(len(b) for b in p["blobs"].values()),
                 "free": sum(e - s for s, e in p["regions"]), "size": list(p["template"].size)}
             for g, p in plan.items()}
    log(f"Patched {exe} ({version}): {len(used)} image(s), " +
        ", ".join(f"{g}: {s['bytes'] / 1e6:.1f}/{s['free'] / 1e6:.1f} MB" for g, s in stats.items()) +
        f", quantized={len(quantized)}")
    progress(tr("done"), 1.0)
    return {"images": len(used), "sets": stats,
            "quantized": sorted({images[i]["name"] for _, i in quantized})}


# --------------------------------------------------------------------------------------
# Scheduled task (auto re-patch after Resolve updates)
# --------------------------------------------------------------------------------------

def _python(windowless=True):
    return sys.executable


def _self_cmd():
    """Command that runs this program again (as the elevated worker or the launch daemon)."""
    return [sys.executable] if FROZEN else [sys.executable, os.path.abspath(__file__)]


def task_installed():
    return os.path.isfile(DAEMON_PLIST)


def _plist_for(exe):
    env = {"RSP_APPDIR": APP_DIR, "RSP_UID": str(os.environ.get("RSP_UID") or os.getuid()),
           "RSP_GID": str(os.environ.get("RSP_GID") or os.getgid())}
    watch = [exe]
    b = app_bundle(exe)
    if b:
        watch.append(os.path.join(b, "Contents", "Info.plist"))
    return plistlib.dumps({
        "Label": TASK_NAME,
        "ProgramArguments": [*_self_cmd(), "--auto"],
        "RunAtLoad": True,
        "WatchPaths": watch,
        "ThrottleInterval": 60,
        "EnvironmentVariables": env,
        "StandardErrorPath": os.path.join(APP_DIR, "daemon.err"),
    })


def install_task():
    """Root launch daemon: re-applies the saved splash screens after Resolve updates."""
    cfg = load_config()
    with open(DAEMON_PLIST, "wb") as f:
        f.write(_plist_for(cfg["exe"]))
    os.chmod(DAEMON_PLIST, 0o644)
    subprocess.run(["launchctl", "bootout", "system", DAEMON_PLIST], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", "system", DAEMON_PLIST], capture_output=True, text=True)
    if r.returncode != 0:
        raise PatchError(tr("task_failed", err=(r.stderr or r.stdout).strip()))
    return {"task": True}


def remove_task():
    subprocess.run(["launchctl", "bootout", "system", DAEMON_PLIST], capture_output=True)
    try:
        os.remove(DAEMON_PLIST)
    except OSError:
        pass
    return {"task": False}


# --------------------------------------------------------------------------------------
# CLI (also used as the elevated worker of the interface)
# --------------------------------------------------------------------------------------

def _progress_writer(path):
    def write(msg, frac=None, **extra):
        if not path:
            log(msg)
            return
        data = {"msg": msg, "frac": frac, **extra}
        tmp = path + ".tmp"
        # the interface may be reading the file at this very moment (Windows sharing
        # violation), so retry; progress reporting must never break the operation itself
        for _ in range(40):
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp, path)
                return
            except OSError:
                time.sleep(0.05)
        if extra.get("done"):
            log("could not write final progress file")
    return write


def cli(argv):
    pfile = argv[argv.index("--progress") + 1] if "--progress" in argv else None
    progress = _progress_writer(pfile)
    cfg = load_config()
    try:
        if "--restore" in argv:
            res = restore_original(cfg["exe"], progress)
        elif "--task-on" in argv:
            res = install_task()
        elif "--task-off" in argv:
            res = remove_task()
        elif "--inspect" in argv:
            print(inspect_report(cfg["exe"]))
            return 0
        elif "--auto" in argv:
            if not cfg["images"]:
                return 0
            for _ in range(20):          # the installer may still hold the file
                if os.path.isfile(cfg["exe"]) and not resolve_running():
                    try:
                        info = inspect_exe(cfg["exe"])
                        break
                    except (OSError, PatchError):
                        pass
                time.sleep(15)
            else:
                log("auto: Resolve.exe not accessible, giving up")
                return 1
            if info.state == "patched":
                return 0
            log(f"auto: Resolve {info.version} is not fully patched – patching")
            res = apply_patch(cfg, progress)
        else:
            res = apply_patch(cfg, progress)
        progress(tr("done"), 1.0, done=True, result=res)
        return 0
    except Exception as e:
        log("ERROR: " + "".join(traceback.format_exception(e)))
        progress(str(e), None, done=True, error=str(e))
        return 1
    finally:
        chown_app_dir()


def inspect_report(exe):
    """Diagnostics: what does the binary contain? (useful if the format differs on macOS)"""
    out = [f"exe: {exe}", f"exists: {os.path.isfile(exe)}", f"bundle: {app_bundle(exe)}",
           f"version: {file_version(exe)}"]
    if not os.path.isfile(exe):
        return "\n".join(out)
    out.append(f"size: {os.path.getsize(exe)}")
    with open(exe, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        out.append(f"magic: {bytes(mm[:4]).hex()}  (cafebabe/bebafeca = universal)")
        try:
            for g, lay in locate_all(mm).items():
                w = lay.win_slots()
                first = Image.open(io.BytesIO(lay.read(mm, next(iter(w.values())))))
                out.append(f"set {g}: {len(w)} slots, {len(lay.linux_slots())} linux slots, "
                           f"image {first.size}, data@{lay.data} names@{lay.names} tree@{lay.tree}, "
                           f"marker={find_marker(mm, lay) is not None}")
        except PatchError as e:
            out.append(f"locate failed: {e}")
        names, pos = set(), 0
        needle = "Splash".encode("utf-16-be")
        while len(names) < 60:
            p = mm.find(needle, pos)
            if p < 0:
                break
            pos = p + 1
            for back in range(6, 200, 2):
                r = read_name_entry(mm, p - back)
                if r and "Splash" in r[0]:
                    names.add(r[0])
                    break
        out.append("names containing 'Splash': " + (", ".join(sorted(names)) or "none"))
    return "\n".join(out)


# --------------------------------------------------------------------------------------
# Interface: local HTTP server + browser window
# --------------------------------------------------------------------------------------

def _applescript_quote(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


def run_worker(args, progress_file):
    """Runs this script with `args` as root (macOS password prompt) and waits. Exit code."""
    full = [*_self_cmd(), *args, "--progress", progress_file]
    env = {"RSP_APPDIR": APP_DIR, "RSP_UID": str(os.getuid()), "RSP_GID": str(os.getgid())}
    if is_admin():
        return subprocess.run(full, env={**os.environ, **env}).returncode
    cmd = "env " + " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items()) + " " + \
          " ".join(shlex.quote(a) for a in full)
    osa = f'do shell script "{_applescript_quote(cmd)}" with administrator privileges'
    r = subprocess.run(["osascript", "-e", osa], capture_output=True, text=True)
    if r.returncode != 0 and "-128" in (r.stderr or ""):
        raise PatchError(tr("uac_denied"))
    return r.returncode


def find_chromium_app():
    for name in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
        if os.path.isdir(f"/Applications/{name}.app"):
            return name
    return None


class App:
    def __init__(self):
        self.cfg = load_config()
        self.lock = threading.RLock()
        self.info = None
        self.info_error = None
        self.orig = {}
        self.template_png = None
        self.box = None
        self.src_cache = {}
        self.job = {"running": False}
        self.last_ping = time.time()
        self.bye_at = None
        self.refresh()

    # ---- exe state
    def refresh(self):
        with self.lock:
            try:
                self.info = inspect_exe(self.cfg["exe"])
                self.info_error = None
                self.orig = original_slot_pngs(self.info)
                first = self.orig[min(self.orig)] if self.orig else None
                if first:
                    tpl = Image.open(io.BytesIO(first)).convert("RGBA")
                    self.box = list(template_geometry(tpl))
                    bio = io.BytesIO()
                    tpl.save(bio, "PNG")
                    self.template_png = bio.getvalue()
            except Exception as e:
                self.info, self.orig = None, {}
                self.info_error = str(e) if os.path.isfile(self.cfg["exe"]) else \
                    tr("exe_missing")

    def state(self):
        info = self.info
        slots = list(info.layouts[next(iter(info.layouts))].win_slots()) if info else []
        sets = []
        if info:
            for g, lay in info.layouts.items():
                first = next(iter(lay.win_slots().values()))
                sets.append({"id": g, "slots": len(lay.win_slots()), "patched": info.markers[g] is not None})
        return {
            "exe": self.cfg["exe"],
            "error": self.info_error,
            "version": info.version if info else None,
            "state": info.state if info else None,
            "patchedTime": info.patched_time if info else None,
            "backupOk": bool(info) and all(info.backup_ok(g) for g, m in info.markers.items() if m),
            "sets": sets,
            "slots": slots,
            "box": self.box,
            "images": [{k: im[k] for k in ("id", "name", "fx", "fy", "zoom")} for im in self.cfg["images"]],
            "darken": self.cfg.get("darken", True),
            "lang": self.cfg.get("lang", "en"),
            "slotCfg": self.cfg["slots"],
            "task": task_installed(),
            "running": resolve_running(),
            "admin": is_admin(),
        }

    def source_jpeg(self, img_id):
        with self.lock:
            if img_id not in self.src_cache:
                im = next(i for i in self.cfg["images"] if i["id"] == img_id)
                src = load_source(im["file"], max_side=3000)
                bio = io.BytesIO()
                src.save(bio, "JPEG", quality=92)
                self.src_cache[img_id] = bio.getvalue()
            return self.src_cache[img_id]

    # ---- jobs
    def start_job(self, kind, args):
        if self.job.get("running"):
            raise PatchError(tr("busy"))
        save_config(self.cfg)
        pfile = os.path.join(tempfile.gettempdir(), f"rsp_{uuid.uuid4().hex[:8]}.json")
        self.job = {"running": True, "kind": kind, "msg": tr("waiting_admin"), "frac": 0}

        def worker():
            try:
                code = run_worker(args, pfile)
                data = {}
                try:
                    with open(pfile, encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, ValueError):
                    pass
                if code != 0 and not data.get("error"):
                    data["error"] = tr("op_failed")
                self.job = {"running": False, "kind": kind, "msg": data.get("msg", ""), "frac": 1,
                            "error": data.get("error"), "result": data.get("result")}
            except Exception as e:
                self.job = {"running": False, "kind": kind, "msg": str(e), "error": str(e)}
            finally:
                try:
                    os.remove(pfile)
                except OSError:
                    pass
                self.refresh()

        def poll():
            while self.job.get("running"):
                try:
                    with open(pfile, encoding="utf-8") as f:
                        data = json.load(f)
                    if self.job.get("running"):
                        self.job.update(msg=data.get("msg", ""), frac=data.get("frac"))
                except (OSError, ValueError):
                    pass
                time.sleep(0.25)

        threading.Thread(target=worker, daemon=True).start()
        threading.Thread(target=poll, daemon=True).start()


def pick_exe_dialog(initial):
    """Native macOS file dialog."""
    script = ('POSIX path of (choose file with prompt "%s" default location (POSIX file "%s"))'
              % (_applescript_quote(tr("pick_exe")),
                 _applescript_quote(os.path.dirname(initial) if initial and os.path.isdir(os.path.dirname(initial)) else "/Applications")))
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    p = r.stdout.strip()
    if p.endswith(".app"):
        p = os.path.join(p, "Contents", "MacOS", "Resolve")
    return os.path.normpath(p) if p else None


def run_server(open_window=True):
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    from urllib.parse import unquote

    app = App()
    token = secrets.token_urlsafe(12)
    dialog_lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            if a and str(a[-1]).startswith(("4", "5")):
                log("HTTP " + (fmt % a))

        def send(self, code, body=b"", ctype="application/json", cache=False):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
            self.end_headers()
            self.wfile.write(body)

        def route(self):
            parts = self.path.split("?")[0].strip("/").split("/")
            if not parts or parts[0] != token:
                return None
            return [unquote(p) for p in parts[1:]]

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def do_GET(self):
            r = self.route()
            if r is None:
                return self.send(404, {"error": "not found"})
            try:
                if r == [] or r == [""]:
                    with open(os.path.join(HERE, "ui.html"), encoding="utf-8") as f:
                        return self.send(200, f.read(), "text/html; charset=utf-8")
                if r == ["api", "state"]:
                    return self.send(200, app.state())
                if r == ["api", "job"]:
                    return self.send(200, app.job)
                if r == ["api", "template.png"]:
                    return self.send(200, app.template_png or b"", "image/png")
                if len(r) == 3 and r[:2] == ["api", "orig"]:
                    data = app.orig.get(int(r[2]))
                    return self.send(200, data, "image/png", cache=True) if data else self.send(404)
                if len(r) == 3 and r[:2] == ["api", "src"]:
                    return self.send(200, app.source_jpeg(r[2]), "image/jpeg", cache=True)
                return self.send(404, {"error": "not found"})
            except Exception as e:
                return self.send(500, {"error": str(e)})

        def do_POST(self):
            r = self.route()
            if r is None:
                return self.send(404, {"error": "not found"})
            app.last_ping = time.time()
            try:
                if r == ["api", "ping"]:
                    app.bye_at = None
                    return self.send(200, {"ok": True})
                if r == ["api", "bye"]:
                    app.bye_at = time.time()
                    return self.send(200, {"ok": True})
                if r == ["api", "upload"]:
                    name = unquote(self.headers.get("X-Filename", "image.png"))
                    im = import_image_bytes(self.body(), os.path.basename(name))
                    with app.lock:
                        app.cfg["images"].append(im)
                        save_config(app.cfg)
                    return self.send(200, {"id": im["id"]})
                if r == ["api", "config"]:
                    data = json.loads(self.body() or b"{}")
                    with app.lock:
                        by_id = {i["id"]: i for i in app.cfg["images"]}
                        if "images" in data:   # order + per-image settings
                            new = []
                            for d in data["images"]:
                                im = by_id.get(d["id"])
                                if im:
                                    for k in ("fx", "fy", "zoom"):
                                        im[k] = float(d[k])
                                    new.append(im)
                            app.cfg["images"] = new
                        if "slots" in data:
                            app.cfg["slots"] = data["slots"]
                        if "darken" in data:
                            app.cfg["darken"] = bool(data["darken"])
                        if data.get("lang") in ("en", "ru"):
                            app.cfg["lang"] = data["lang"]
                            set_lang(data["lang"])
                        save_config(app.cfg)
                    return self.send(200, {"ok": True})
                if len(r) == 3 and r[:2] == ["api", "delete"]:
                    with app.lock:
                        im = next((i for i in app.cfg["images"] if i["id"] == r[2]), None)
                        if im:
                            app.cfg["images"].remove(im)
                            app.cfg["slots"] = {k: ("auto" if v == im["id"] else v)
                                                for k, v in app.cfg["slots"].items()}
                            save_config(app.cfg)
                            app.src_cache.pop(im["id"], None)
                            try:
                                os.remove(im["file"])
                            except OSError:
                                pass
                    return self.send(200, {"ok": True})
                if r == ["api", "exe"]:
                    data = json.loads(self.body() or b"{}")
                    path = data.get("path")
                    if data.get("browse"):
                        with dialog_lock:
                            path = pick_exe_dialog(app.cfg["exe"])
                    if path:
                        with app.lock:
                            app.cfg["exe"] = path
                            save_config(app.cfg)
                        app.refresh()
                    return self.send(200, app.state())
                if r == ["api", "refresh"]:
                    app.refresh()
                    return self.send(200, app.state())
                if r == ["api", "apply"]:
                    app.start_job("apply", ["--apply"])
                    return self.send(200, app.job)
                if r == ["api", "restore"]:
                    app.start_job("restore", ["--restore"])
                    return self.send(200, app.job)
                if r == ["api", "task"]:
                    on = json.loads(self.body() or b"{}").get("on")
                    app.start_job("task", ["--task-on" if on else "--task-off"])
                    return self.send(200, app.job)
                return self.send(404, {"error": "not found"})
            except PatchError as e:
                return self.send(400, {"error": str(e)})
            except Exception as e:
                log("ERROR: " + "".join(traceback.format_exception(e)))
                return self.send(500, {"error": str(e)})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    url = f"http://127.0.0.1:{srv.server_address[1]}/{token}/"
    log(f"Interface at {url}")

    def watchdog():
        while True:
            time.sleep(2)
            if app.job.get("running"):
                continue
            # console build: the server stays up until the Terminal window is closed (Ctrl+C);
            # browsers throttle background tabs, so idle time says nothing here
            continue

    threading.Thread(target=watchdog, daemon=True).start()
    if open_window:
        print(f"\nInterface: {url}\n(Leave this window open while you use the program.)", flush=True)
        try:
            subprocess.Popen(["open", url])          # default browser
        except Exception as e:
            log(f"could not open the browser: {e}")
    srv.serve_forever()


def main():
    try:
        _main()
    except SystemExit:
        raise
    except BaseException:
        err = traceback.format_exc()
        log("FATAL: " + err)
        print(err, flush=True)
        sys.exit(1)


def _main():
    argv = sys.argv[1:]
    log(f"start argv={argv} frozen={FROZEN} python={sys.version.split()[0]} appdir={APP_DIR}")
    if any(a in argv for a in ("--apply", "--auto", "--restore", "--task-on", "--task-off", "--inspect")):
        sys.exit(cli(argv))
    run_server(open_window="--no-window" not in argv)


if __name__ == "__main__":
    main()
