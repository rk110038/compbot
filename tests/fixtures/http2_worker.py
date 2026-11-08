"""Stands in for app.entry.worker: a site that drops HTTP/2, then either loads over HTTP/1.1 or leaves it hanging."""
import os
import pickle
import sys

from app.entry.browser import AttemptResult

job = pickle.load(sys.stdin.buffer)
if not job.get("http1"):
    r = AttemptResult(result="error", message="Error: Page.goto: net::ERR_HTTP2_PROTOCOL_ERROR at " + job["url"],
                      details={"steps": []})
elif os.environ.get("HTTP1_LOADS"):
    r = AttemptResult(result="dry_run_ok", message="Dry run: filled 2 fields", details={"steps": ["filled"]})
else:
    r = AttemptResult(result="error", message="TimeoutError: Page.goto: Timeout 45000ms exceeded.",
                      details={"steps": []})
sys.stdout.buffer.write(pickle.dumps(r))
sys.stdout.buffer.flush()