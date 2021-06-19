#

import sys
import os
import types
import traceback
import multiprocessing

import wandb


def is_likely_multiprocessing_process() -> bool:
    """Heuristic to guess if current process started by multiprocessing"""

    if hasattr(multiprocessing, "parent_process"):  # py38+ only
        return multiprocessing.parent_process() is not None
    proc = multiprocessing.current_process()
    if not proc:
        return False  # not sure when this might happen, lets guess not mp
    if proc.name == "MainProcess":
        return False
    if proc.name.startswith("Process-"):
        return True
    # most people probably dont name their process, but lets not assume it
    return False


old_exit_handler = None


def custom_mp_exit_handler(code: int = None) -> None:
    print("EXIT", code)
    traceback.print_stack()

    if old_exit_handler:
        old_exit_handler(code)


old_osexit_handler = None


def custom_mp_osexit_handler(code: int = None) -> None:
    print("OS_EXIT", code)
    traceback.print_stack()

    if old_osexit_handler:
        old_osexit_handler(code)


def install_exit_handler() -> None:
    print("INSTALL1")
    if not isinstance(sys.exit, types.BuiltinFunctionType):
        wandb.termerror("Not installing mp exit handler since it has already been set.")
        return
    print("INSTALL2")

    # if sys.excepthook == sys.__excepthook__:
    #     pass

    global old_exit_handler
    old_exit_handler = sys.exit
    sys.exit = custom_mp_exit_handler

    global old_osexit_handler
    old_osexit_handler = os._exit
    os._exit = custom_mp_osexit_handler
    print("INSTALL3")


def uninstall_multiprocess_exit_handler() -> None:
    pass
