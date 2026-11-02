"""Stands in for app.entry.worker: sends its result straight away, then gets stuck 'closing the browser'."""
import os
import pickle
import sys
import time

from app.entry.browser import AttemptResult

pickle.load(sys.stdin.buffer)
sys.stdout.buffer.write(pickle.dumps(AttemptResult(result="failed", message="No entry form found on the page",
                                                   details={"timing": {"seconds": 1}})))
sys.stdout.buffer.flush()
devnull = os.open(os.devnull, os.O_WRONLY)
os.dup2(devnull, 1)
time.sleep(120)
