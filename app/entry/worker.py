"""Child-process entry point: run one Browser.enter() and hand back the result.

Started by browser.enter_isolated() as `python -m app.entry.worker`; the job and result are pickled over
stdin/stdout between compbot's own processes. Running each entry in its own process lets the parent kill a page
that hangs, without any dependence on how the parent itself was launched.
"""
from __future__ import annotations

import pickle
import sys


def main() -> None:
    job = pickle.load(sys.stdin.buffer)
    result_channel = sys.stdout.buffer
    sys.stdout = sys.stderr  # keep any stray output out of the result channel

    from .browser import AttemptResult, Browser

    try:
        with Browser() as b:
            r = b.enter(job["url"], job["ctx"], job["settings"], job["shot_prefix"], job["comp_context"])
    except Exception as exc:  # noqa: BLE001
        r = AttemptResult(result="error", message=f"{type(exc).__name__}: {exc}"[:800])
    result_channel.write(pickle.dumps(r))
    result_channel.flush()


if __name__ == "__main__":
    main()
