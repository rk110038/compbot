import pytest

from app.netguard import BlockedURL, guarded_client, is_public_url


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.delenv("ALLOW_PRIVATE_URLS", raising=False)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/", "http://localhost/", "http://192.168.1.10:8085/settings", "http://10.0.0.5/",
    "http://169.254.169.254/latest/meta-data", "http://nas.local/", "http://truenas/", "http://[::1]/",
    "file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)",
])
def test_private_and_odd_urls_blocked(strict, url):
    assert not is_public_url(url)


def test_public_ip_allowed(strict):
    assert is_public_url("https://8.8.8.8/")
    assert is_public_url("data:image/png;base64,AAAA", allow_inline=True)
    assert not is_public_url("data:image/png;base64,AAAA")


def test_guarded_client_refuses_private(strict):
    with guarded_client() as c, pytest.raises(BlockedURL):
        c.get("http://127.0.0.1:9/")
