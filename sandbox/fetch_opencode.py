"""Download the pinned OpenCode binary and verify its release digest."""
import argparse
import hashlib
from http.client import HTTPException
import os
from pathlib import Path
import platform
import tarfile
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

VERSION = '1.18.18'
ASSETS = {
    'x86_64': ('opencode-linux-x64-baseline.tar.gz', '05914655854c1dab48f0d52a8af8f74967e4e26daf87dfe6534f57e799a853d7'),
    'aarch64': ('opencode-linux-arm64.tar.gz', 'dcb1b5ec5687b43f87749560021f9203f3809e0ce5ae44ff9be8ae17083fe4ba'),
}
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_BINARY = 512 * 1024 * 1024


def download(url, archive, expected):
    """Retry transient release-host failures; verify before using any bytes."""
    for attempt in range(3):
        digest, total = hashlib.sha256(), 0
        deadline = time.monotonic() + 300
        try:
            with urlopen(url, timeout=60) as source, archive.open('wb') as target:
                while chunk := source.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_ARCHIVE or time.monotonic() >= deadline:
                        raise RuntimeError('The OpenCode download exceeded its size or time limit.')
                    digest.update(chunk)
                    target.write(chunk)
        except (HTTPError, URLError, HTTPException, TimeoutError) as error:
            if isinstance(error, HTTPError) and error.code not in {429, 500, 502, 503, 504}:
                raise RuntimeError(f'The OpenCode release download failed (HTTP {error.code}).') from None
            if attempt == 2:
                raise RuntimeError('The OpenCode release could not be downloaded after three attempts.') from None
            time.sleep(2 ** (attempt + 1))
            continue
        if digest.hexdigest() != expected:
            raise RuntimeError('The OpenCode release checksum does not match.')
        return


def install(archive, destination):
    """Extract one regular binary and replace the destination atomically."""
    with tarfile.open(archive) as source:
        members = [member for member in source.getmembers() if Path(member.name).name == 'opencode' and member.isfile()]
        if len(members) != 1 or not 0 < members[0].size <= MAX_BINARY:
            raise RuntimeError('The release does not contain the expected binary.')
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with source.extractfile(members[0]) as binary, tempfile.NamedTemporaryFile(
                    prefix='.opencode-', dir=destination.parent, delete=False) as target:
                temporary = Path(target.name)
                while chunk := binary.read(1024 * 1024):
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            temporary.chmod(0o755)
            os.replace(temporary, destination)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    if platform.machine() not in ASSETS:
        parser.error('Supported architectures are x86_64 and aarch64.')
    asset, expected = ASSETS[platform.machine()]
    url = f'https://github.com/anomalyco/opencode/releases/download/v{VERSION}/{asset}'
    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / asset
        download(url, archive, expected)
        install(archive, args.destination)
    print(f'Installed OpenCode {VERSION} at {args.destination}')


if __name__ == '__main__':
    main()
