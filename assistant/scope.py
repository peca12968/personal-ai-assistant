"""Verify explicitly selected folder boundaries before reading file contents."""
import os
import stat
from pathlib import Path


def is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def select_root(path):
    path = Path(path).expanduser().absolute()
    if is_link(path.lstat()):
        raise ValueError('請直接選取實際資料夾，不使用連結或接合點。')
    root = path.resolve(strict=True)
    if not root.is_dir():
        raise ValueError('搜尋範圍必須是資料夾。')
    return root


def check_path(path, root):
    path, root = Path(path).absolute(), Path(root)
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError('文件不在選定資料夾內。') from None
    current = root
    for part in (None, *relative.parts):
        if part is not None:
            current /= part
        if is_link(current.lstat()):
            raise ValueError('不搜尋連結、接合點或重新解析點。')
    if not path.resolve(strict=True).is_relative_to(root):
        raise ValueError('文件解析後超出搜尋範圍。')
    return path


def opened_path(stream):
    if os.name == 'nt':
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        function = kernel.GetFinalPathNameByHandleW
        function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        function.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = function(msvcrt.get_osfhandle(stream.fileno()), buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise OSError('無法確認已開啟文件的實際位置。')
        value = buffer.value
        if value.startswith('\\\\?\\UNC\\'):
            value = '\\\\' + value[8:]
        elif value.startswith('\\\\?\\'):
            value = value[4:]
        return Path(value)
    # Linux development/tests. Fail closed on platforms lacking handle resolution.
    return Path(os.readlink('/proc/self/fd/' + str(stream.fileno())))


def verify_open_file(stream, root):
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
        raise ValueError('搜尋只讀取一般文件，不包含硬連結。')
    actual = opened_path(stream)
    if not actual.is_absolute() or not actual.is_relative_to(root):
        raise ValueError('已開啟文件超出選定資料夾。')
    check_path(actual, root)
