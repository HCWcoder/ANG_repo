"""Encrypt session secrets with Windows DPAPI for the current Windows user."""

import ctypes
import json
import os
import tempfile
from ctypes import wintypes
from pathlib import Path

from .errors import SessionError

DEFAULT_SESSION_PATH = Path(__file__).resolve().parents[1] / ".anghami" / "session.dpapi"


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise SessionError("The encrypted session store requires Windows and the user who saved it.")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = _Blob()
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(_Blob), ctypes.c_void_p if decrypt else wintypes.LPCWSTR,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_Blob),
    ]
    function.restype = wintypes.BOOL
    description = None if decrypt else "Anghami local session"
    # CRYPTPROTECT_UI_FORBIDDEN; no machine-wide flag, so this is user-bound.
    if not function(ctypes.byref(source), description, None, None, None, 1, ctypes.byref(output)):
        raise SessionError("Windows could not unlock or protect the session. Use the original Windows user.")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(output.data)


def save_protected_bytes(data: bytes, path: Path) -> None:
    """Replace a DPAPI file atomically without creating a plaintext temporary file."""
    encrypted = _crypt(data)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".dpapi.tmp", delete=False) as temp:
            temporary_path = Path(temp.name)
            temp.write(encrypted)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def load_protected_bytes(path: Path) -> bytes:
    return _crypt(Path(path).read_bytes(), decrypt=True)


def save_session(data: dict, path: Path = DEFAULT_SESSION_PATH) -> None:
    save_protected_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8"), path)


def load_session(path: Path = DEFAULT_SESSION_PATH) -> dict:
    raw = load_protected_bytes(path)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise SessionError("The saved session is not valid. Capture a new login.") from None
    if not isinstance(data, dict):
        raise SessionError("The saved session format is not supported.")
    return data
