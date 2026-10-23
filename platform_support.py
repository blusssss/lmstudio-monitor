"""Windows host telemetry and local LM Studio CLI integration."""
import ctypes
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request


def lms_connection_args(port=None):
    """Use the published SDK port, which differs from the REST API port."""
    if port is not None:
        return ['--port', str(port)]
    home = Path.home() / '.lmstudio'
    for internal in (home / '.internal', home / 'apps' / 'bionic' / '.internal'):
        try:
            info = json.loads((internal / 'http-server.json').read_text(encoding='utf-8'))
            port = info.get('port')
            if type(port) is not int or not 0 < port < 65536:
                continue
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/lmstudio-greeting', timeout=1) as response:
                if json.load(response).get('lmstudio') is True:
                    return ['--port', str(port)]
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return []


@contextmanager
def lms_environment(connection_args):
    """Keep old Windows CLIs in Bionic's own home and authentication scope.

    Explicit --host makes lms a remote unprivileged client. Only set --port;
    let lms read its normal credentials from the selected app's directory.
    No credentials are read or copied by the monitor.
    """
    env = os.environ.copy()
    bionic = Path.home() / '.lmstudio' / 'apps' / 'bionic'
    try:
        info = json.loads((bionic / '.internal' / 'http-server.json').read_text(encoding='utf-8'))
        selected = connection_args == ['--port', str(info['port'])]
    except (OSError, ValueError, TypeError, KeyError):
        selected = False
    if selected:
        with tempfile.TemporaryDirectory(prefix='lmstudio-cli-') as folder:
            (Path(folder) / '.lmstudio-home-pointer').write_text(str(bionic),encoding='utf-8')
            env['USERPROFILE'] = folder
            yield env
    else:
        yield env


def find_lms():
    override = os.environ.get('LMS_BIN')
    if override:
        return override
    name = 'lms.exe'
    installed = Path.home() / '.lmstudio' / 'bin' / name
    return str(installed) if installed.is_file() else (shutil.which(name) or name)


def find_nvidia_smi():
    found = shutil.which('nvidia-smi')
    if found:
        return found
    candidates = [Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'nvidia-smi.exe',
                  Path(os.environ.get('ProgramW6432', r'C:\Program Files')) / 'NVIDIA Corporation' / 'NVSMI' / 'nvidia-smi.exe']
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return 'nvidia-smi.exe'


def child_options():
    # Keep the monitor console visible, hide only periodic CLI helper windows.
    return {'creationflags': subprocess.CREATE_NO_WINDOW}


class MemoryStatus(ctypes.Structure):
    _fields_ = [('length', ctypes.c_uint32), ('load', ctypes.c_uint32)] + [
        (name, ctypes.c_uint64) for name in ('total_phys', 'avail_phys', 'total_page',
        'avail_page', 'total_virtual', 'avail_virtual', 'avail_extended')]


class FileTime(ctypes.Structure):
    _fields_ = [('low', ctypes.c_uint32), ('high', ctypes.c_uint32)]

    @property
    def ticks(self):
        return (self.high << 32) | self.low


def windows_system():
    """Return (total CPU ticks, idle ticks, physical RAM MiB, used RAM MiB)."""
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.GetSystemTimes.argtypes = [ctypes.POINTER(FileTime)] * 3
    api.GetSystemTimes.restype = ctypes.c_int
    api.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
    api.GlobalMemoryStatusEx.restype = ctypes.c_int
    idle, kernel, user = FileTime(), FileTime(), FileTime()
    memory = MemoryStatus()
    memory.length = ctypes.sizeof(memory)
    if not api.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not api.GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise ctypes.WinError(ctypes.get_last_error())
    # Windows kernel ticks already include idle time.
    return (kernel.ticks + user.ticks, idle.ticks, memory.total_phys / 1048576,
            (memory.total_phys - memory.avail_phys) / 1048576)
