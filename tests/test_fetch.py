"""Download robustness against a scripted local HTTP server (no internet needed).

Covers: retry after 503, Retry-After honoured, truncated body rejected and retried,
404 not retried, .part file never left as a final file, existing file not re-fetched.
"""

import http.server
import threading

import pytest

import ztf_lc_bulk.fetch as fetch

PAYLOAD = b"PAR1" + bytes(range(256)) * 400 + b"PAR1"


class Handler(http.server.BaseHTTPRequestHandler):
    script: dict = {}   # path -> list of actions, consumed in order
    log: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        actions = self.script.get(self.path, ["ok"])
        act = actions.pop(0) if len(actions) > 1 else actions[0]
        self.log.append((self.path, act))
        if act == "ok":
            self.send_response(200)
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)
        elif act == "short":  # promise full length, send half, close
            self.send_response(200)
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD[: len(PAYLOAD) // 2])
            self.close_connection = True
        elif act == "503-retry-after":
            self.send_response(503)
            self.send_header("Retry-After", "7")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:  # numeric status
            self.send_response(int(act))
            self.send_header("Content-Length", "0")
            self.end_headers()


@pytest.fixture
def server(monkeypatch):
    sleeps = []
    monkeypatch.setattr(fetch.time, "sleep", lambda s: sleeps.append(s))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    Handler.log = []
    yield f"http://127.0.0.1:{srv.server_address[1]}/", sleeps
    srv.shutdown()


def test_ok(server, tmp_path):
    base, _ = server
    Handler.script = {"/a/f.parquet": ["ok"]}
    p, n = fetch.download("a/f.parquet", tmp_path, base_url=base)
    assert p.read_bytes() == PAYLOAD and n == len(PAYLOAD)
    # second call: already present, no request
    p2, n2 = fetch.download("a/f.parquet", tmp_path, base_url=base)
    assert n2 == 0 and len(Handler.log) == 1


def test_retry_then_ok_and_retry_after(server, tmp_path):
    base, sleeps = server
    Handler.script = {"/b.parquet": ["503", "503-retry-after", "ok"]}
    p, n = fetch.download("b.parquet", tmp_path, base_url=base)
    assert p.read_bytes() == PAYLOAD
    assert [a for _, a in Handler.log] == ["503", "503-retry-after", "ok"]
    assert sleeps == [2.0, 7.0]  # exponential backoff, then Retry-After


def test_short_read_is_retried(server, tmp_path):
    base, _ = server
    Handler.script = {"/c.parquet": ["short", "ok"]}
    p, _ = fetch.download("c.parquet", tmp_path, base_url=base)
    assert p.read_bytes() == PAYLOAD
    assert not list(tmp_path.glob("*.part"))


def test_404_not_retried_and_no_file_left(server, tmp_path):
    base, sleeps = server
    Handler.script = {"/d.parquet": ["404"]}
    with pytest.raises(fetch.FetchError):
        fetch.download("d.parquet", tmp_path, base_url=base, retries=5)
    assert len(Handler.log) == 1 and sleeps == []
    assert not (tmp_path / "d.parquet").exists() and not list(tmp_path.glob("*.part"))


def test_gives_up_after_retries(server, tmp_path):
    base, sleeps = server
    Handler.script = {"/e.parquet": ["500"]}
    with pytest.raises(fetch.FetchError):
        fetch.download("e.parquet", tmp_path, base_url=base, retries=3)
    assert len(Handler.log) == 3 and not (tmp_path / "e.parquet").exists()
