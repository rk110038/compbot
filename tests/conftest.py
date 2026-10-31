import functools
import http.server
import os
import sys
import tempfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="compbot-test-")
os.environ.setdefault("DATA_DIR", _tmp)
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("WEB_PASSWORD", "test")
os.environ.setdefault("ALLOW_PRIVATE_URLS", "true")  # the fixture server runs on 127.0.0.1

PROFILE = {
    "title": "Mr", "first_name": "Test", "last_name": "Person", "email": "test.person@example.com",
    "phone": "07700900123", "dob": "1980-03-05", "gender": "", "address1": "1 Test Street", "address2": "",
    "town": "Testville", "county": "Berkshire", "postcode": "RG1 1AA", "country": "United Kingdom",
    "twitter": "", "instagram": "",
}


@pytest.fixture(scope="session")
def fixture_server():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(Quiet, directory=str(ROOT / "tests" / "fixtures"))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def profile():
    return dict(PROFILE)
