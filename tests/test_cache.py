import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cache import AssetCache, CHECKSUMS, DownloadError, HTTPSRedirect


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache = AssetCache(self.temp.name)
        self.payload = b'verified release bytes'
        self.checksum = hashlib.sha256(self.payload).hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def test_download_once_then_serve_verified_cache(self):
        opener = mock.Mock()
        opener.open.return_value = io.BytesIO(self.payload)
        with mock.patch.dict(CHECKSUMS, {'amd64': self.checksum}), mock.patch('cache.urllib.request.build_opener', return_value=opener):
            path = self.cache.get('amd64')
            self.assertEqual(path.read_bytes(), self.payload)
            self.assertEqual(self.cache.get('amd64'), path)
            self.assertEqual(opener.open.call_count, 1)
            self.assertEqual(list(Path(self.temp.name).glob('.download-*')), [])

    def test_bad_download_never_replaces_or_serves_old_corrupt_file(self):
        path = Path(self.temp.name) / self.cache.filename('amd64')
        path.write_bytes(b'corrupted cached data')
        opener = mock.Mock()
        opener.open.return_value = io.BytesIO(b'invalid release bytes')
        with mock.patch.dict(CHECKSUMS, {'amd64': self.checksum}), mock.patch('cache.urllib.request.build_opener', return_value=opener):
            with self.assertRaisesRegex(DownloadError, 'SHA256'):
                self.cache.get('amd64')
        self.assertEqual(path.read_bytes(), b'corrupted cached data')
        self.assertEqual(list(Path(self.temp.name).glob('.download-*')), [])

    def test_download_network_error_and_architecture_validation(self):
        with self.assertRaises(ValueError):
            self.cache.get('../../panel.db')
        opener = mock.Mock()
        opener.open.side_effect = TimeoutError()
        with mock.patch('cache.urllib.request.build_opener', return_value=opener):
            with self.assertRaises(DownloadError):
                self.cache.get('amd64')
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        with self.assertRaises(DownloadError):
            HTTPSRedirect().redirect_request(None, None, 302, '', {}, 'http://example.com/asset')
