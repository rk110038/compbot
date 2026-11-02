"""Child-process entry point: run one Browser.enter() and hand back the result.

Started by browser.enter_isolated() as `python -m app.entry.worker`; the job and result are pickled over
stdin/stdout between compbot's own processes. Running each entry in its own process lets the parent kill a page
that hangs, without any dependence on how the parent itself was launched.
"""
from __future__ import annotations

import os
import pickle
import sys


def main() -> None:
    job = pickle.load(sys.stdin.buffer)
    result_channel = sys.stdout.buffer
    sys.stdout = sys.stderr  # keep any stray output out of the result channel
    sent = [False]

    def send(r) -> None:
        # Send the result the moment it's known, then let go of the channel: the parent stops waiting there, so
        # closing the browser afterwards can't hold up the run.
        if sent[0]:
            return
        sent[0] = True
        result_channel.write(pickle.dumps(r))
        result_channel.flush()
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.close(devnull)

    from .browser import AttemptResult, Browser

    b = None
    try:
        b = Browser()
        r = b.enter(job["url"], job["ctx"], job["settings"], job["shot_prefix"], job["comp_context"], on_result=send)
    except Exception as exc:  # noqa: BLE001
        r = AttemptResult(result="error", message=f"{type(exc).__name__}: {exc}"[:800])
    send(r)
    if b is not None:
        b.close()


if __name__ == "__main__":
    main()
