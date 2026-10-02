import io
import json
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import upgrade
from upgrade_control import UpgradeControl


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_request_atomic_deduplication_and_disabled_deployment(self):
        control = UpgradeControl(self.root, status_directory=self.root / 'status')
        with self.assertRaisesRegex(ValueError, '尚未启用'):
            control.start()
        control.enabled = True
        self.assertEqual(control.start()['state'], 'queued')
        self.assertEqual(control.status()['state'], 'queued')
        self.assertEqual(len(json.loads(control.request.read_text())['id']), 32)
        with self.assertRaisesRegex(ValueError, '正在执行'):
            control.start()

    def test_reject_archive_traversal_and_links(self):
        for filename, kind in [('repo/../../outside', tarfile.REGTYPE), ('repo/link', tarfile.SYMTYPE), ('repo/hard', tarfile.LNKTYPE)]:
            archive = self.root / 'bad.tar.gz'
            with tarfile.open(archive, 'w:gz') as bundle:
                member = tarfile.TarInfo(filename)
                member.type = kind
                member.linkname = '/etc/passwd'
                bundle.addfile(member)
            with self.assertRaisesRegex(ValueError, '不安全'):
                upgrade.extract(archive, self.root / 'source')

    def test_failed_health_restores_code_and_database(self):
        source, previous, data, state = [self.root / name for name in ('source', 'previous', 'data', 'state')]
        for directory in (source, data, state):
            directory.mkdir()
        (source / 'old.txt').write_text('old version')
        with sqlite3.connect(str(data / 'panel.db')) as db:
            db.execute('CREATE TABLE settings (value TEXT)')
            db.execute("INSERT INTO settings VALUES ('keep-me')")

        def download(url, destination, limit=None):
            if '/commits/' in url:
                destination.write_text(json.dumps({'sha': 'a' * 40}))
            else:
                with tarfile.open(destination, 'w:gz') as bundle:
                    for name in ('app.py', 'core.py', 'agent.py', 'installers.py', 'cache.py', 'upgrade.py', 'upgrade_control.py', 'diagnostics.py', 'static/index.html'):
                        member = tarfile.TarInfo('repo/' + name)
                        content = b'# new version\n'
                        member.size = len(content)
                        bundle.addfile(member, io.BytesIO(content))

        def unhealthy():
            with sqlite3.connect(str(data / 'panel.db')) as db:
                db.execute('ALTER TABLE settings ADD COLUMN migrated TEXT')
                db.execute("UPDATE settings SET value='changed'")
            raise RuntimeError('unhealthy')

        with mock.patch.multiple(upgrade, SOURCE=source, PREVIOUS=previous, DATA=data, STATE=state), mock.patch.object(upgrade, 'download', side_effect=download), mock.patch.object(upgrade, 'health', side_effect=unhealthy), mock.patch.object(upgrade, 'service') as service, mock.patch.object(upgrade.os, 'chown'):
            with self.assertRaisesRegex(RuntimeError, 'unhealthy'):
                upgrade.upgrade()
        self.assertEqual((source / 'old.txt').read_text(), 'old version')
        self.assertFalse((source / 'app.py').exists())
        self.assertEqual(service.call_args_list, [mock.call('stop'), mock.call('start'), mock.call('stop'), mock.call('start')])
        with sqlite3.connect(str(data / 'panel.db')) as db:
            self.assertEqual(db.execute('SELECT * FROM settings').fetchone(), ('keep-me',))


if __name__ == '__main__':
    unittest.main()
