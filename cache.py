"""Verified, fixed-version GOST release cache for node installation downloads."""
import argparse
import hashlib
import os
import tempfile
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from core import CHECKSUMS, GOST_VERSION

MAX_ASSET_SIZE = 64 * 1024 * 1024


class DownloadError(RuntimeError):
    pass


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != 'https':
            raise DownloadError('安装包下载跳转必须使用 HTTPS')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class AssetCache:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.locks = {arch: threading.Lock() for arch in CHECKSUMS}

    def filename(self, arch):
        if arch not in CHECKSUMS:
            raise ValueError('仅支持 amd64 和 arm64 安装包')
        return 'gost_' + GOST_VERSION + '_linux_' + arch + '.tar.gz'

    def verified(self, path, checksum):
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_ASSET_SIZE:
            return False
        hasher = hashlib.sha256()
        with path.open('rb') as source:
            for chunk in iter(lambda: source.read(128 * 1024), b''):
                hasher.update(chunk)
        return hasher.hexdigest() == checksum

    def get(self, arch):
        filename = self.filename(arch)
        path = self.directory / filename
        with self.locks[arch]:
            if self.verified(path, CHECKSUMS[arch]):
                return path
            url = 'https://github.com/go-gost/gost/releases/download/v' + GOST_VERSION + '/' + filename
            temporary = None
            try:
                opener = urllib.request.build_opener(HTTPSRedirect())
                with opener.open(url, timeout=60) as response, tempfile.NamedTemporaryFile(dir=self.directory, prefix='.download-', delete=False) as output:
                    temporary = Path(output.name)
                    hasher, size = hashlib.sha256(), 0
                    for chunk in iter(lambda: response.read(128 * 1024), b''):
                        size += len(chunk)
                        if size > MAX_ASSET_SIZE:
                            raise DownloadError('安装包超过允许大小')
                        hasher.update(chunk)
                        output.write(chunk)
                    if size == 0 or hasher.hexdigest() != CHECKSUMS[arch]:
                        raise DownloadError('GOST 安装包 SHA256 校验失败')
                os.chmod(temporary, 0o600)
                os.replace(temporary, path)
                temporary = None
                return path
            except DownloadError:
                raise
            except Exception as exc:
                raise DownloadError('面板下载 GOST 安装包失败，请检查面板服务器到 GitHub 的连接或预置已校验的安装包') from exc
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', required=True)
    args = parser.parse_args()
    cache = AssetCache(args.directory)
    for arch in CHECKSUMS:
        print('缓存 GOST ' + GOST_VERSION + ' Linux ' + arch + '…', flush=True)
        print('已校验：' + str(cache.get(arch)), flush=True)


if __name__ == '__main__':
    main()
