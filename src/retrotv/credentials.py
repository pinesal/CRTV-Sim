"""Application credentials; secrets never enter profiles or settings snapshots."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import os

TARGET = "RetroTV/BreezeBlue"


class _Credential(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]


def _api():
    if os.name != "nt":
        raise RuntimeError("Saving credentials requires Windows Credential Manager; use a provider environment variable on other platforms")
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.POINTER(ctypes.POINTER(_Credential))]
    api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredFree.argtypes = [ctypes.c_void_p]
    return api


def saved_breeze_key(target=TARGET):
    if os.name != "nt":
        return ""
    api = _api(); pointer = ctypes.POINTER(_Credential)()
    if not api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        if ctypes.get_last_error() == 1168:
            return ""
        raise RuntimeError("Windows Credential Manager could not read the saved credential")
    try:
        return ctypes.string_at(pointer.contents.CredentialBlob,
                                pointer.contents.CredentialBlobSize).decode("utf-8")
    finally:
        api.CredFree(pointer)


def configured_breeze_api_key():
    value = os.environ.get("BREEZE_API_KEY", "").strip()
    if not value and os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value = str(winreg.QueryValueEx(key, "BREEZE_API_KEY")[0]).strip()
        except OSError:
            pass
    if value:
        return value
    try:
        return saved_breeze_key()
    except RuntimeError:
        return ""


def save_breeze_key(value, target=TARGET):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Enter an API key")
    blob = value.strip().encode("utf-8")
    if len(blob) > 2560:
        raise ValueError("API key is too long")
    buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    credential = _Credential(Type=1, TargetName=target, CredentialBlobSize=len(blob),
                             CredentialBlob=buffer, Persist=2, UserName="RetroTV")
    if not _api().CredWriteW(ctypes.byref(credential), 0):
        raise RuntimeError("Windows Credential Manager could not save the credential")


def remove_breeze_key(target=TARGET):
    if not _api().CredDeleteW(target, 1, 0) and ctypes.get_last_error() != 1168:
        raise RuntimeError("Windows Credential Manager could not remove the credential")


def saved_openai_key():
    return saved_breeze_key("RetroTV/OpenAI")


def configured_openai_api_key():
    value = os.environ.get("OPENAI_API_KEY", "").strip()
    if not value and os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value = str(winreg.QueryValueEx(key, "OPENAI_API_KEY")[0]).strip()
        except OSError:
            pass
    if value:
        return value
    try:
        return saved_openai_key()
    except RuntimeError:
        return ""


def save_openai_key(value):
    save_breeze_key(value, "RetroTV/OpenAI")


def remove_openai_key():
    remove_breeze_key("RetroTV/OpenAI")
