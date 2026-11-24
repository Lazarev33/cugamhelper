"""Не передавать память процесса системному сборщику дампов."""

import ctypes
import os
import sys


def disable_core_dumps():
    if sys.platform == "linux":
        pr_set_dumpable = 4
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(pr_set_dumpable, 0, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "Не удалось отключить дампы процесса")
