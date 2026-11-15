import http.client
import io
import json
import os
import tarfile
import threading
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer

from tests.support import Base, fresh_server


class BackupBase(Base):
    def setUp(self):
        super().setUp()
        self.ui = fresh_server()
        self.bk = self.ui.backups
        self.data = self.tmp / 'data'
        self.patch(self.ui.core, 'DATA', self.data)
        (self.data / 'deployed' / 'web').mkdir(parents=True)
        (self.data / 'rollback' / 'web' / 'files').mkdir(parents=True)
        (self.data / 'settings.json').write_text('{"api_token_sha256": "ab"}', encoding='utf-8')
        (self.data / 'schedule.json').write_text('{"time": "04:00"}', encoding='utf-8')
        (self.data / 'deployed' / 'web' / 'compose.yaml').write_text('services: {}\n', encoding='utf-8')
        (self.data / 'rollback' / 'web' / 'rollback.json').write_text('{}', encoding='utf-8')
        (self.data / 'rollback' / 'web' / 'files' / 'compose.yaml').write_bytes(b'old\n')
        self.folder = self.tmp / 'backups'
        self.folder.mkdir()
        self.patch(self.bk, 'BACKUP_DIR', str(self.folder))

    @staticmethod
    def members(body: bytes) -> dict:
        with tarfile.open(fileobj=io.BytesIO(body), mode='r:gz') as tar:
            return {m.name: (tar.extractfile(m).read() if m.isfile() else None) for m in tar.getmembers()}


class BuildTest(BackupBase):
    def test_the_archive_holds_the_data_folder_and_a_manifest(self):
        name, body = self.bk.build(datetime(2026, 11, 15, 18, 30, 5))
        self.assertEqual(name, 'stack-ui-backup-20261115-183005.tar.gz')
        files = self.members(body)
        self.assertEqual(sorted(files), ['deployed/web/compose.yaml', 'rollback/web/files/compose.yaml',
                                         'rollback/web/rollback.json', 'schedule.json', 'settings.json',
                                         'stack-ui-backup.json'])
        self.assertEqual(files['rollback/web/files/compose.yaml'], b'old\n')
        manifest = json.loads(files['stack-ui-backup.json'])
        self.assertEqual(manifest['stackUi'], self.ui.__version__)
        self.assertEqual(manifest['created'], '2026-11-15T18:30:05')
        self.assertEqual(len(manifest['files']), 5)

    def test_leftovers_temporary_files_and_other_files_stay_out(self):
        (self.data / 'deployed' / 'web.new').mkdir()
        (self.data / 'deployed' / 'web.new' / 'compose.yaml').write_text('half', encoding='utf-8')
        (self.data / '.schedule.json.tmp').write_text('half', encoding='utf-8')
        (self.data / 'unrelated.txt').write_text('x', encoding='utf-8')
        files = self.members(self.bk.build(datetime(2026, 11, 15))[1])
        self.assertNotIn('deployed/web.new/compose.yaml', files)
        self.assertNotIn('.schedule.json.tmp', files)
        self.assertNotIn('unrelated.txt', files)

    def test_a_link_is_never_followed(self):
        outside = self.tmp / 'outside.txt'
        outside.write_text('secret', encoding='utf-8')
        try:
            os.symlink(outside, self.data / 'deployed' / 'web' / 'link.yaml')
            os.symlink(self.tmp, self.data / 'rollback' / 'linkdir', target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('no symlinks here')
        files = self.members(self.bk.build(datetime(2026, 11, 15))[1])
        self.assertFalse([n for n in files if 'link' in n], files)
        self.assertNotIn(b'secret', b''.join(v for v in files.values() if v))

    def test_an_empty_data_folder_still_gives_an_archive(self):
        self.patch(self.ui.core, 'DATA', self.tmp / 'empty')
        self.assertEqual(list(self.members(self.bk.build(datetime(2026, 11, 15))[1])), ['stack-ui-backup.json'])

    def test_it_waits_for_no_change_it_runs_as_one(self):
        with self.ui.core.operation('deploy', 'web'):
            with self.assertRaises(self.ui.core.Conflict):
                self.bk.download()
            with self.assertRaises(self.ui.core.Conflict):
                self.bk.save()


class SaveTest(BackupBase):
    def test_a_saved_backup_is_private_and_listed(self):
        r = self.bk.save()
        path = self.folder / r['file']
        self.assertTrue(path.is_file())
        self.assertEqual(r['size'], path.stat().st_size)
        if os.name == 'posix':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual([b['name'] for b in self.bk.view()['backups']], [r['file']])
        self.assertIn('settings.json', self.members(path.read_bytes()))
        self.assertFalse([p for p in self.folder.iterdir() if p.name.endswith('.tmp')])

    def test_two_in_one_second_do_not_overwrite_each_other(self):
        self.patch(self.bk, 'now', lambda: datetime(2026, 11, 15, 18, 0, 0))
        first, second = self.bk.save()['file'], self.bk.save()['file']
        self.assertNotEqual(first, second)
        self.assertEqual(len(list(self.folder.iterdir())), 2)

    def test_only_the_newest_ones_are_kept_and_nothing_else_is_touched(self):
        self.patch(self.bk, 'KEEP', 3)
        (self.folder / 'notes.txt').write_text('mine', encoding='utf-8')
        for minute in range(5):
            self.patch(self.bk, 'now', lambda m=minute: datetime(2026, 11, 15, 18, m, 0))
            r = self.bk.save()
        names = sorted(p.name for p in self.folder.iterdir())
        self.assertEqual(names, ['notes.txt', 'stack-ui-backup-20261115-180200.tar.gz',
                                 'stack-ui-backup-20261115-180300.tar.gz', 'stack-ui-backup-20261115-180400.tar.gz'])
        self.assertEqual(r['removed'], ['stack-ui-backup-20261115-180100.tar.gz'])

    def test_without_a_folder_it_says_what_to_set(self):
        self.patch(self.bk, 'BACKUP_DIR', '')
        with self.assertRaisesRegex(ValueError, 'STACK_UI_BACKUP_DIR'):
            self.bk.save()
        self.assertEqual(self.bk.view()['ready'], False)
        self.patch(self.bk, 'BACKUP_DIR', str(self.tmp / 'missing'))
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            self.bk.save()


class RouteTest(BackupBase):
    def setUp(self):
        super().setUp()
        self.ui.Config.password_hash = ''
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.ui.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def call(self, method, path, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=15)
        self.addCleanup(conn.close)
        conn.request(method, path, body='{}' if method == 'POST' else None,
                     headers=headers or {'X-UI-Token': self.ui.Config.local_token, 'Content-Type': 'application/json'})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()

    def test_the_download_is_an_archive_with_a_file_name(self):
        code, headers, body = self.call('GET', '/api/backup/download')
        self.assertEqual(code, 200)
        self.assertEqual(headers['Content-Type'], 'application/gzip')
        self.assertRegex(headers['Content-Disposition'], r'^attachment; filename="stack-ui-backup-\d{8}-\d{6}\.tar\.gz"$')
        self.assertIn('settings.json', self.members(body))

    def test_save_and_view(self):
        code, _, body = self.call('POST', '/api/backup/save')
        self.assertEqual(code, 200)
        saved = json.loads(body)['file']
        code, _, body = self.call('GET', '/api/backup')
        view = json.loads(body)
        self.assertEqual((code, view['ready'], view['backups'][0]['name']), (200, True, saved))

    def test_a_change_running_answers_409(self):
        with self.ui.core.operation('deploy', 'web'):
            self.assertEqual(self.call('GET', '/api/backup/download')[0], 409)

    def test_the_api_token_cannot_take_a_backup(self):
        token = self.ui.api_token({})['token']
        bearer = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        for method, path in (('GET', '/api/backup'), ('GET', '/api/backup/download'), ('POST', '/api/backup/save'),
                             ('GET', '/api/backup/download/')):
            self.assertEqual(self.call(method, path, bearer)[0], 403, path)
        self.assertEqual(list(self.folder.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
