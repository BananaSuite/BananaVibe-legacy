"""Pinned release installation against real archives and scripted HTTP reads."""

import hashlib
from http.client import IncompleteRead
import io
import tarfile
from urllib.error import HTTPError

import pytest

from sandbox import fetch_opencode as fetch


def test_release_download_retries_transient_errors_and_restarts_partial_files(tmp_path, monkeypatch):
    data = b"verified archive"
    calls, delays = [], []

    class Interrupted(io.BytesIO):
        def read(self, size):
            if self.tell():
                raise IncompleteRead(b"partial")
            return super().read(3)

    results = [HTTPError("https://example.org", 503, "unavailable", {}, None), Interrupted(data), io.BytesIO(data)]

    def open_url(url, timeout):
        calls.append((url, timeout))
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(fetch, "urlopen", open_url)
    monkeypatch.setattr(fetch.time, "sleep", delays.append)
    archive = tmp_path / "download.tar.gz"
    fetch.download("https://example.org", archive, hashlib.sha256(data).hexdigest())
    assert archive.read_bytes() == data and len(calls) == 3 and delays == [2, 4]


@pytest.mark.parametrize("status,attempts", [(404, 1), (403, 1), (503, 3)])
def test_failed_downloads_are_bounded_and_do_not_retry_permanent_errors(tmp_path, monkeypatch, status, attempts):
    calls = []

    def open_url(*_, **__):
        calls.append(1)
        raise HTTPError("https://example.org", status, "failure", {}, None)
    monkeypatch.setattr(fetch, "urlopen", open_url)
    monkeypatch.setattr(fetch.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="download"):
        fetch.download("https://example.org", tmp_path / "archive", "not-a-digest")
    assert len(calls) == attempts


def test_checksum_mismatch_and_oversized_downloads_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "urlopen", lambda *_, **__: io.BytesIO(b"bad archive"))
    with pytest.raises(RuntimeError, match="checksum"):
        fetch.download("https://example.org", tmp_path / "archive", "not-a-digest")
    monkeypatch.setattr(fetch, "MAX_ARCHIVE", 2)
    with pytest.raises(RuntimeError, match="limit"):
        fetch.download("https://example.org", tmp_path / "archive", "not-a-digest")


def archive_with(tmp_path, names):
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as target:
        for name, content in names:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            target.addfile(member, io.BytesIO(content))
    return archive


def test_installation_replaces_only_the_requested_destination(tmp_path):
    archive = archive_with(tmp_path, [("../../outside/opencode", b"binary"), ("other", b"ignored")])
    destination = tmp_path / "bin" / "opencode"
    destination.parent.mkdir()
    destination.write_bytes(b"old binary")
    fetch.install(archive, destination)
    assert destination.read_bytes() == b"binary" and destination.stat().st_mode & 0o777 == 0o755
    assert list(destination.parent.iterdir()) == [destination]
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("names", [[], [("other", b"bad")], [("opencode", b"")],
                                   [("opencode", b"first"), ("dir/opencode", b"second")]])
def test_invalid_archives_preserve_the_previous_installation(tmp_path, names):
    archive = archive_with(tmp_path, names)
    destination = tmp_path / "opencode"
    destination.write_bytes(b"previous binary")
    with pytest.raises(RuntimeError, match="expected binary"):
        fetch.install(archive, destination)
    assert destination.read_bytes() == b"previous binary"
