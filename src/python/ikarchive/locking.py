"""Nonblocking process lock on macOS, Linux and Windows."""
import os

def acquire(file):
    if os.name=='nt':
        import msvcrt
        file.seek(0);file.write('0');file.flush();file.seek(0)
        try:msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:raise BlockingIOError from None
    else:
        import fcntl
        fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)
