import os
import sys
import threading
import unittest


def _unraisablehook(args):
    try:
        sys.__unraisablehook__(args)
        sys.stderr.flush()
    finally:
        os._exit(1)


def _thread_excepthook(args):
    if args.exc_type is SystemExit:
        return
    try:
        threading.__excepthook__(args)
        sys.stderr.flush()
    finally:
        os._exit(1)


if __name__ == "__main__":
    sys.unraisablehook = _unraisablehook
    threading.excepthook = _thread_excepthook
    unittest.main(module=None)
