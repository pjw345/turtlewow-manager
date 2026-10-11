import http.client
import json
import multiprocessing
import pathlib
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import manager


REVISION = 'a' * 40


def try_other_process(path, queue):
    try:
        with manager.operation_lock(pathlib.Path(path)):
            queue.put('acquired')
    except RuntimeError:
        queue.put('blocked')


class Sandbox(manager.Manager):
    def __init__(self, root):
        deployment = root / 'deployment'
        deployment.mkdir()
        (deployment / '.env').write_text('DB_PASSWORD=very-secret\nTURTLE_IMAGE=tortoise-wow:playerbots-12345678\n')
        super().__init__({'deployment': str(deployment), 'core': str(root / 'core'),
                          'packaging': str(root / 'packaging'), 'state': str(root / 'state'),
                          'backups': str(root / 'backups')})
        self.calls = []
        self.running = True
        self.fail_stop = False
        self.fail_build = False
        self.fail_backup = False
        self.wrong_revision = False
        self.stop_exit = 0
        self.remote_revision = REVISION
        self.remote_error = False
        self.source_requests = 0
        self.current_revision = 'c' * 40

    def assert_no_external_build(self):
        pass

    def run(self, args, **kwargs):
        if args[0] == 'git':
            if 'status' in args:
                return ''
            if 'ls-remote' in args:
                self.source_requests += 1
                if self.remote_error:
                    raise RuntimeError('GitHub unavailable')
                return self.remote_revision + '\t' + args[-1]
            return REVISION
        if args[:3] == ['docker', 'image', 'inspect']:
            return self.current_revision
        if args[:2] == ['docker', 'build']:
            self.calls.append('compile')
            self.compile_args = list(args)
            if self.fail_build:
                raise RuntimeError('Compilation failed')
            return ''
        if args[:2] == ['docker', 'run']:
            self.calls.append('verify')
            return 'b' * 40 if self.wrong_revision else next(
                value.split('=', 1)[1] for value in self.compile_args if value.startswith('SOURCE_COMMIT='))
        raise AssertionError(args)

    def compose(self, *args, **kwargs):
        if args[0] == 'stop':
            self.calls.append('stop')
            if not self.fail_stop:
                self.running = False
        if args[0] == 'up':
            self.calls.append('start')
            self.running = True
        return ''

    def containers(self):
        return {'mangosd': {'State': {'Running': self.running, 'ExitCode': self.stop_exit if not self.running else 0}}}

    def cold_backup(self):
        self.calls.append('backup')
        assert not self.running
        if self.fail_backup:
            raise RuntimeError('Archive failed')
        return '/safe/backup'


class ConfigurationPathsTest(unittest.TestCase):
    def test_home_relative_paths_use_the_operator_home(self):
        with tempfile.TemporaryDirectory() as home:
            config = json.loads((manager.ROOT / 'config.example.json').read_text())
            deployment = pathlib.Path(home) / 'turtlewow'
            deployment.mkdir()
            (deployment / '.env').write_text('')
            with patch.dict('os.environ', {'HOME': home}):
                control = manager.Manager(config)
            expected = {
                'deployment': 'turtlewow',
                'core': 'development/tortoise-wow',
                'packaging': 'development/tortoise-docker',
                'state_dir': '.local/state/turtlewow-manager',
                'backups': 'turtlewow/backups/manager',
            }
            for attribute, suffix in expected.items():
                with self.subTest(attribute=attribute):
                    self.assertEqual(getattr(control, attribute), (pathlib.Path(home) / suffix).resolve())
            self.assertTrue(control.state_dir.is_dir())


class OperationsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = Sandbox(pathlib.Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_default_build_uses_remote_tip_even_with_stale_checkout(self):
        self.manager.remote_revision = 'd' * 40
        self.manager.execute('build')
        self.assertIn('SOURCE_COMMIT=' + 'd' * 40, self.manager.compile_args)
        self.assertEqual(self.manager.read_state()['build']['revision'], 'd' * 40)

    def test_same_revision_refuses_build_before_shutdown(self):
        self.manager.current_revision = REVISION
        with self.assertRaisesRegex(RuntimeError, 'already uses the latest'):
            self.manager.execute('build')
        self.assertEqual(self.manager.calls, [])
        self.assertTrue(self.manager.running)

    def test_verified_latest_build_refuses_duplicate_before_activation(self):
        manager.atomic_json(self.manager.state_path, {'build': {'revision': REVISION}})
        with self.assertRaisesRegex(RuntimeError, 'already built'):
            self.manager.execute('build')
        self.assertEqual(self.manager.calls, [])

    def test_unreachable_remote_refuses_build_without_stopping(self):
        self.manager.remote_error = True
        with self.assertRaisesRegex(RuntimeError, 'Cannot check GitHub'):
            self.manager.execute('build')
        self.assertEqual(self.manager.calls, [])

    def test_remote_checks_are_cached_and_failure_disables_stale_availability(self):
        self.assertTrue(self.manager.build_availability('c' * 40)['build_available'])
        self.assertFalse(self.manager.build_availability(REVISION)['build_available'])
        self.assertEqual(self.manager.source_requests, 1)
        self.manager.remote_error = True
        self.manager.source_checked_at -= 61
        result = self.manager.build_availability('c' * 40)
        self.assertFalse(result['build_available'])
        self.assertIsNone(result['latest_revision'])

    def test_missing_branch_is_not_treated_as_new_source(self):
        self.manager.remote_revision = 'invalid'
        self.assertFalse(self.manager.build_availability(None)['build_available'])

    def test_build_stops_and_backs_up_before_compilation(self):
        self.manager.execute('build')
        self.assertEqual(self.manager.calls, ['stop', 'backup', 'compile', 'verify'])
        self.assertFalse(self.manager.running)
        self.assertEqual(self.manager.read_state()['build']['revision'], REVISION)
        self.assertIn('12345678', (self.manager.deployment / '.env').read_text())

    def test_build_uses_local_source_and_persistent_builder(self):
        self.manager.execute('build', REVISION)
        args = self.manager.compile_args
        self.assertIn('local-core=' + str(self.manager.core), args)
        self.assertIn('BUILD_SOURCE=local-builder', args)
        self.assertIn('SOURCE_COMMIT=' + REVISION, args)
        self.assertNotIn('--no-cache', args)

    def test_failed_shutdown_never_compiles(self):
        self.manager.fail_stop = True
        with self.assertRaisesRegex(RuntimeError, 'still running'):
            self.manager.execute('build')
        self.assertEqual(self.manager.calls, ['stop'])
        self.assertIsNone(self.manager.read_state()['build'])

    def test_failed_backup_never_compiles_or_restarts(self):
        self.manager.fail_backup = True
        with self.assertRaisesRegex(RuntimeError, 'Archive failed'):
            self.manager.execute('build')
        self.assertEqual(self.manager.calls, ['stop', 'backup'])
        self.assertFalse(self.manager.running)

    def test_stop_segfault_aborts_build_and_is_recorded_as_failure(self):
        self.manager.stop_exit = 139
        with self.assertRaisesRegex(RuntimeError, 'exit code 139'):
            self.manager.execute('build')
        self.assertEqual(self.manager.calls, ['stop'])
        self.assertEqual(self.manager.read_state()['history'][0]['status'], 'failed')

    def test_unclean_database_exit_refuses_physical_backup(self):
        self.manager.containers = lambda: {'db': {'State': {'Running': False, 'ExitCode': 137}}}
        with self.assertRaisesRegex(RuntimeError, 'Database did not stop normally'):
            manager.Manager.cold_backup(self.manager)

    def test_failed_build_leaves_stopped_and_does_not_select_image(self):
        self.manager.fail_build = True
        with self.assertRaisesRegex(RuntimeError, 'Compilation failed'):
            self.manager.execute('build')
        self.assertFalse(self.manager.running)
        state = self.manager.read_state()
        self.assertEqual(state['history'][0]['status'], 'failed')
        self.assertIsNone(state['build'])
        self.assertFalse(manager.lock_busy(self.manager.lock_path))

    def test_wrong_embedded_source_rejects_build(self):
        self.manager.wrong_revision = True
        with self.assertRaisesRegex(RuntimeError, 'source SHA'):
            self.manager.execute('build')
        self.assertIsNone(self.manager.read_state()['build'])

    def test_start_blocked_by_build_lock_in_another_process(self):
        context = multiprocessing.get_context('fork')
        queue = context.Queue()
        with manager.operation_lock(self.manager.lock_path):
            process = context.Process(target=try_other_process, args=(str(self.manager.lock_path), queue))
            process.start()
            self.assertEqual(queue.get(timeout=3), 'blocked')
            process.join(timeout=3)
            with self.assertRaises(RuntimeError):
                self.manager.execute('start')
        self.assertNotIn('start', self.manager.calls)

    def test_http_submission_reserves_lock_before_worker_starts(self):
        entered, release = threading.Event(), threading.Event()
        def held_start():
            entered.set()
            release.wait(3)
        self.manager.start = held_start
        self.manager.submit('start')
        self.assertTrue(entered.wait(2))
        with self.assertRaisesRegex(RuntimeError, 'Another operation'):
            self.manager.submit('build')
        release.set()
        deadline = time.monotonic() + 3
        while manager.lock_busy(self.manager.lock_path) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse(manager.lock_busy(self.manager.lock_path))

    def test_backup_restores_previously_running_services(self):
        self.manager.execute('backup')
        self.assertEqual(self.manager.calls, ['stop', 'backup', 'start'])
        self.assertTrue(self.manager.running)

    def test_failed_backup_does_not_restart(self):
        self.manager.fail_backup = True
        with self.assertRaises(RuntimeError):
            self.manager.execute('backup')
        self.assertFalse(self.manager.running)

    def test_native_counts_do_not_subtract_free_floating_bots(self):
        counts = manager.parse_counts('Sessions online: 2\nBots online: 12 (active: 9)')
        self.assertEqual(counts, {'users': 2, 'bots': 12, 'active_bots': 9})
        self.assertIsNone(manager.parse_counts('World server starting'))

    def test_image_selection_preserves_password_and_rejects_arbitrary_images(self):
        path = self.manager.deployment / '.env'
        manager.replace_image(path, 'tortoise-wow:playerbots-aaaaaaaa')
        self.assertIn('DB_PASSWORD=very-secret', path.read_text())
        with self.assertRaises(RuntimeError):
            manager.replace_image(path, 'evil:latest')

    def test_operation_output_redacts_password(self):
        self.manager.start = lambda: self.manager.record('failure: very-secret')
        self.manager.execute('start')
        self.assertNotIn('very-secret', self.manager.status()['log'])

    def test_active_initializer_is_not_interrupted(self):
        self.manager.containers = lambda: {'db-init': {'State': {'Running': True}}}
        with self.assertRaisesRegex(RuntimeError, 'initialization'):
            self.manager.execute('build')
        self.assertEqual(self.manager.calls, [])

    def test_wrong_build_source_cannot_change_deployment(self):
        state = self.manager.read_state()
        state['build'] = {'image': 'tortoise-wow:playerbots-aaaaaaaa', 'revision': REVISION}
        manager.atomic_json(self.manager.state_path, state)
        self.manager.wrong_revision = True
        with self.assertRaisesRegex(RuntimeError, 'Build image changed'):
            self.manager.execute('activate')
        self.assertIn('12345678', (self.manager.deployment / '.env').read_text())
        self.assertNotIn('stop', self.manager.calls)

    def test_unique_build_tag_is_accepted_but_arbitrary_suffix_is_not(self):
        path = self.manager.deployment / '.env'
        manager.replace_image(path, 'tortoise-wow:playerbots-aaaaaaaa-20261011103000-ab12')
        with self.assertRaises(RuntimeError):
            manager.replace_image(path, 'tortoise-wow:playerbots-aaaaaaaa-malicious')


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = Sandbox(pathlib.Path(self.temp.name))
        self.server = manager.http.server.ThreadingHTTPServer(('127.0.0.1', 0), manager.Handler)
        self.server.manager = self.manager
        self.port = self.server.server_port
        self.manager.config['public_origin'] = f'http://127.0.0.1:{self.port}'
        self.server.auth = manager.Auth(pathlib.Path(self.temp.name) / 'account.json')
        self.server.auth.sessions['test'] = {'csrf': self.manager.csrf, 'expires': time.monotonic()+100, 'last': time.monotonic(), 'hash': 'test', 'username': 'admin'}
        manager.atomic_json(self.server.auth.path, {'hash': 'test'})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port)
        connection.request(method, path, json.dumps({'action': 'start'}) if method == 'POST' else None, headers or {})
        response = connection.getresponse()
        body = response.read()
        status = response.status
        connection.close()
        return status, body

    def test_rebinding_host_rejected(self):
        self.assertEqual(self.request('GET', '/admin/api/status', {'Host': 'evil.example'})[0], 403)

    def test_unauthenticated_status_is_private(self):
        self.assertEqual(self.request('GET', '/admin/api/status')[0], 401)

    def test_login_cookie_csrf_and_logout(self):
        from auth import password_hash
        salt = 'ab' * 32
        manager.atomic_json(self.server.auth.path, {'username': 'admin', 'salt': salt,
                            'hash': password_hash('test-password-only', salt)})
        headers = {'Content-Type': 'application/json', 'Origin': self.manager.config['public_origin']}
        connection = http.client.HTTPConnection('127.0.0.1', self.port)
        connection.request('POST', '/admin/api/login', json.dumps({'username': 'admin', 'password': 'test-password-only'}), headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        cookie = response.getheader('Set-Cookie')
        for protection in ['HttpOnly', 'Secure', 'SameSite=Strict', 'Path=/admin']:
            self.assertIn(protection, cookie)
        csrf = json.loads(response.read())['csrf']
        connection.close()
        headers['Cookie'] = cookie.split(';')[0]
        self.assertEqual(self.request('GET', '/admin/api/status', headers)[0], 200)
        self.assertEqual(self.request('POST', '/admin/api/action', headers)[0], 403)
        headers['X-CSRF-Token'] = csrf
        self.assertEqual(self.request('POST', '/admin/api/logout', headers)[0], 200)
        self.assertEqual(self.request('GET', '/admin/api/status', headers)[0], 401)

    def test_cross_origin_and_missing_csrf_cannot_start(self):
        base = {'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{self.port}'}
        self.assertEqual(self.request('POST', '/admin/api/action', base)[0], 401)
        base.update({'Origin': 'http://evil.example', 'X-CSRF-Token': self.manager.csrf})
        self.assertEqual(self.request('POST', '/admin/api/action', base)[0], 403)
        self.assertEqual(self.manager.calls, [])

    def test_same_origin_csrf_action_rejected_when_locked(self):
        headers = {'Cookie': 'turtlewow_session=test', 'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{self.port}', 'X-CSRF-Token': self.manager.csrf}
        with manager.operation_lock(self.manager.lock_path):
            self.assertEqual(self.request('POST', '/admin/api/action', headers)[0], 409)
        self.assertEqual(self.manager.calls, [])

    def test_frontend_has_security_headers_and_no_placeholder_token(self):
        status, body = self.request('GET', '/admin')
        self.assertEqual(status, 200)
        self.assertNotIn(b'__CSRF__', body)
        self.assertNotIn(self.manager.csrf.encode(), body)
        self.assertIn(b'login-form', body)


if __name__ == '__main__':
    unittest.main()
