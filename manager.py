#!/usr/bin/env python3
"""TurtleWoW control plane. Python standard library; no shell command API."""
import argparse
from auth import Auth, create_admin
import contextlib
import datetime as dt
import fcntl
import hashlib
import http.server
import json
import os
import pathlib
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
from urllib.parse import urlsplit

ROOT = pathlib.Path(__file__).resolve().parent
CONFIG = pathlib.Path(os.environ.get('TURTLEWOW_MANAGER_CONFIG', '~/.config/turtlewow-manager/config.json')).expanduser()
ACTIONS = {'start', 'stop', 'backup', 'build', 'activate'}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.' + secrets.token_hex(4) + '.tmp')
    with temp.open('w') as stream:
        os.chmod(temp, 0o600)
        json.dump(value, stream, indent=2)
    os.replace(temp, path)


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


@contextlib.contextmanager
def operation_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a+') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another operation is running. Wait for it to finish.') from None
        try:
            yield stream.fileno()
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def lock_busy(path):
    try:
        with operation_lock(path):
            return False
    except RuntimeError:
        return True


def parse_counts(text):
    sessions = re.findall(r'Sessions online:\s*(\d+)', text)
    bots = re.findall(r'Bots online:\s*(\d+)\s*\(active:\s*(\d+)\)', text)
    if not sessions or not bots:
        return None
    return {'users': int(sessions[-1]), 'bots': int(bots[-1][0]), 'active_bots': int(bots[-1][1])}


def env_values(path):
    result = {}
    for line in path.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            result[key.strip()] = value.strip().strip('\"\'')
    return result


def replace_image(path, image):
    if not re.fullmatch(r'tortoise-wow:playerbots-[0-9a-f]{8}(?:-[0-9]{14}-[0-9a-f]{4})?', image):
        raise RuntimeError('Unrecognized build image.')
    lines = path.read_text().splitlines()
    matches = [index for index, line in enumerate(lines) if line.startswith('TURTLE_IMAGE=')]
    if len(matches) > 1:
        raise RuntimeError('Duplicate TURTLE_IMAGE settings; review the deployment first.')
    if matches:
        lines[matches[0]] = 'TURTLE_IMAGE=' + image
    else:
        lines.append('TURTLE_IMAGE=' + image)
    temp = path.with_name('.env.manager.tmp')
    with temp.open('w') as stream:
        os.chmod(temp, 0o600)
        stream.write('\n'.join(lines) + '\n')
    os.replace(temp, path)


class Manager:
    def __init__(self, config):
        self.config = config
        self.deployment = pathlib.Path(config['deployment']).resolve()
        self.core = pathlib.Path(config['core']).resolve()
        self.packaging = pathlib.Path(config['packaging']).resolve()
        self.state_dir = pathlib.Path(config['state']).expanduser().resolve()
        self.backups = pathlib.Path(config['backups']).expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_path = self.state_dir / 'operation.lock'
        self.state_path = self.state_dir / 'operations.json'
        self.mutex = threading.RLock()
        self.snapshot = {'sampled_at': None, 'services': {}, 'counts': None, 'error': 'Loading server state'}
        self.csrf = secrets.token_urlsafe(32)
        self.count_sample = None
        self.count_container = None
        self.last_count_request = 0
        self.fd = None
        self.operation_thread = None
        self.log_path = None
        self.operation = None
        self.stop_poll = threading.Event()
        self.secret_values = [v for k, v in env_values(self.deployment / '.env').items() if 'PASSWORD' in k and v]
        if not lock_busy(self.lock_path):
            state = self.read_state()
            if state.get('active'):
                stale = state['active']
                stale.update(status='interrupted', finished_at=now(), message='Manager stopped before the operation completed; inspect its log.')
                state.setdefault('history', []).insert(0, stale)
                state['active'] = None
                atomic_json(self.state_path, state)

    def redact(self, text):
        for secret in self.secret_values:
            text = text.replace(secret, '[redacted]')
        return text

    def read_state(self):
        return load_json(self.state_path, {'active': None, 'history': [], 'build': None})

    def record(self, message):
        if self.log_path:
            with self.log_path.open('a') as stream:
                stream.write(self.redact(message) + '\n')
        if self.operation:
            self.operation['message'] = message
            state = self.read_state()
            state['active'] = self.operation
            atomic_json(self.state_path, state)

    def build_progress(self, stage, percent=None):
        if not self.operation or self.operation['action'] != 'build':
            return
        progress = {'stage': stage, 'percent': percent}
        if self.operation.get('progress') == progress:
            return
        self.operation['progress'] = progress
        self.record(stage + (f' · {percent}% compiled' if percent is not None else ''))

    def build_output(self, line):
        if not self.operation or self.operation['action'] != 'build':
            return
        match = re.search(r'\[\s*(\d{1,3})%\]\s+(?:Building|Linking|Built target)', line)
        if match and int(match[1]) <= 100:
            previous = self.operation.get('progress', {})
            percent = int(match[1])
            if previous.get('stage') == 'Compiling':
                percent = max(percent, previous.get('percent') or 0)
            self.build_progress('Compiling', percent)
        elif '-- Installing:' in line or '-- Up-to-date:' in line:
            self.build_progress('Packaging image')
        elif 'exporting to image' in line:
            self.build_progress('Exporting image')

    def run(self, args, *, cwd=None, timeout=30, stream=False):
        # Argument arrays only. Inherited lock remains held if the manager dies
        # while its Docker client is still running.
        inherited_fd = self.fd if threading.get_ident() == self.operation_thread else None
        kwargs = {'cwd': cwd, 'text': True, 'stdout': subprocess.PIPE,
                  'stderr': subprocess.STDOUT, 'pass_fds': (inherited_fd,) if inherited_fd is not None else ()}
        if stream:
            with subprocess.Popen(args, **kwargs) as process:
                for line in process.stdout:
                    with self.log_path.open('a') as output:
                        output.write(self.redact(line))
                    self.build_output(line)
                if process.wait() != 0:
                    raise RuntimeError('Command failed; see the operation log.')
            return ''
        result = subprocess.run(args, timeout=timeout, **kwargs)
        if result.returncode:
            raise RuntimeError(self.redact(result.stdout[-2000:].strip()) or 'Command failed')
        return result.stdout.strip()

    def compose(self, *args, timeout=30):
        return self.run(['docker', 'compose', '--project-directory', str(self.deployment),
                         '-f', str(self.deployment / 'docker-compose.yml'), *args], timeout=timeout)

    def containers(self):
        ids = self.compose('ps', '-a', '-q').split()
        if not ids:
            return {}
        raw = json.loads(self.run(['docker', 'inspect', *ids]))
        return {item['Config']['Labels']['com.docker.compose.service']: item for item in raw}

    def assert_no_external_build(self):
        # A useful additional guard for ad-hoc Docker clients, not a security
        # boundary. All supported build/start entry points share operation.lock.
        for path in pathlib.Path('/proc').glob('[0-9]*/cmdline'):
            try:
                parts = path.read_bytes().split(b'\0')
                if parts and pathlib.Path(os.fsdecode(parts[0])).name == 'docker':
                    args = [os.fsdecode(p) for p in parts[1:] if p]
                    if args[:1] == ['build'] or args[:2] == ['buildx', 'build']:
                        raise RuntimeError('An external Docker build is running; wait before starting.')
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                pass

    def stopped(self):
        items = self.containers()
        if any(item['State']['Running'] for item in items.values()):
            raise RuntimeError('A deployment container is still running; build/backup cancelled.')
        return items

    def shutdown(self):
        before = self.containers()
        initializer = before.get('db-init')
        if initializer and initializer['State']['Running']:
            raise RuntimeError('Database initialization is still running. Wait before stopping, backing up or building.')
        self.record('Stopping world, login and database services cleanly…')
        self.compose('stop', '-t', '120', 'mangosd', 'realmd', 'db', timeout=180)
        after = self.stopped()
        for service in ('mangosd', 'realmd', 'db'):
            item = after.get(service)
            if item and before.get(service, {}).get('State', {}).get('Running') and item['State'].get('ExitCode', 0) != 0:
                raise RuntimeError(f'{service} stopped with exit code {item["State"]["ExitCode"]}. Inspect server logs before continuing.')
        return after

    def cold_backup(self):
        items = self.stopped()
        if items.get('db', {}).get('State', {}).get('ExitCode', 0) != 0:
            raise RuntimeError('Database did not stop normally. Inspect/recover it before taking a physical backup.')
        self.backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.backups / (dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S-') + secrets.token_hex(3))
        target.mkdir(mode=0o700)
        self.record('Creating a cold database, marker and configuration backup…')
        volumes = {}
        try:
            for service, mount_point, leaf in [('db', '/var/lib/mysql', 'db-data.tar.gz'),
                                              ('db-init', '/var/lib/turtle-init', 'init-marker.tar.gz')]:
                item = items.get(service)
                if not item:
                    raise RuntimeError(f'{service} container is missing; cannot identify its backup volume.')
                mounts = [m for m in item['Mounts'] if m['Destination'] == mount_point and m['Type'] == 'volume']
                if len(mounts) != 1:
                    raise RuntimeError(f'Expected exactly one named volume for {service}.')
                volume = mounts[0]['Name']
                volumes[service] = volume
                image = items['mangosd']['Config']['Image']
                self.run(['docker', 'run', '--rm', '--network', 'none', '--user', '0', '--entrypoint', 'sh',
                          '--mount', f'type=volume,source={volume},target=/source,readonly',
                          '--mount', f'type=bind,source={target},target=/backup', image, '-c',
                          'tar -C /source -czf "/backup/$1" . && chown "$2:$3" "/backup/$1"',
                          'backup', leaf, str(os.getuid()), str(os.getgid())], timeout=600)
                os.chmod(target / leaf, 0o600)
            with tarfile.open(target / 'deployment-config.tar.gz', 'w:gz') as archive:
                for leaf in ('.env', 'docker-compose.yml', 'config', 'docker'):
                    archive.add(self.deployment / leaf, arcname=leaf)
            atomic_json(target / 'manifest.json', {'created_at': now(), 'volumes': volumes,
                        'source': items['mangosd']['Config'].get('Labels', {}).get('org.opencontainers.image.revision'), 'image': items['mangosd']['Image'],
                        'mariadb_image': items['db']['Image'], 'kind': 'cold physical backup'})
            hashes = []
            for leaf in ('db-data.tar.gz', 'init-marker.tar.gz', 'deployment-config.tar.gz'):
                with tarfile.open(target / leaf) as archive:
                    if not archive.getmembers():
                        raise RuntimeError(f'Empty archive: {leaf}')
                with (target / leaf).open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                hashes.append(f'{digest}  {leaf}')
            (target / 'SHA256SUMS').write_text('\n'.join(hashes) + '\n')
            self.record('Backup archives and checksums verified: ' + target.name)
            return str(target)
        except Exception:
            (target / 'INCOMPLETE').write_text('Backup failed. Do not restore this directory.\n')
            raise

    def start(self):
        self.assert_no_external_build()
        self.record('Starting database, login and world services…')
        self.compose('up', '-d', 'realmd', 'mangosd', timeout=240)
        self.record('Containers started. World initialization can take several minutes; watch readiness below.')

    def build(self, revision=None):
        self.build_progress('Checking source')
        self.assert_no_external_build()
        for repo in (self.core, self.packaging):
            if self.run(['git', '-C', str(repo), 'status', '--porcelain']):
                raise RuntimeError('A development checkout has uncommitted changes; commit and push before building.')
        revision = revision or self.run(['git', '-C', str(self.core), 'rev-parse', 'HEAD'])
        if not re.fullmatch(r'[0-9a-f]{40}', revision):
            raise RuntimeError('Build revision must be a full 40-character commit SHA.')
        self.run(['git', '-C', str(self.core), 'fetch', 'origin', revision], timeout=90)
        self.build_progress('Stopping services')
        self.shutdown()
        self.build_progress('Creating backup')
        backup = self.cold_backup()
        # Unique tag per operation: rebuilding the same source must not move a
        # tag already selected by a production deployment or rollback image.
        suffix = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S') + '-' + secrets.token_hex(2)
        image = 'tortoise-wow:playerbots-' + revision[:8] + '-' + suffix
        self.record('Building ' + revision[:8] + ' with two jobs and persistent compiler/object caches. Servers remain stopped.')
        self.build_progress('Preparing compiler and cache')
        self.run(['docker', 'build', '--progress=plain', '--build-context', 'local-core=' + str(self.core),
                  '--build-arg', 'BUILD_SOURCE=local-builder', '--build-arg', 'SOURCE_COMMIT=' + revision,
                  '--build-arg', 'BUILD_PLAYERBOTS=ON', '--build-arg', 'BUILD_JOBS=2',
                  '--build-arg', 'CPU_TARGET=x86-64-v2', '--tag', image, '.'], cwd=self.packaging, stream=True)
        self.build_progress('Verifying image')
        actual = self.run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'cat', image, '/opt/turtle/SOURCE_COMMIT'])
        if actual != revision:
            raise RuntimeError('Built image source SHA does not match the requested revision.')
        state = self.read_state()
        state['build'] = {'image': image, 'revision': revision, 'finished_at': now(), 'backup': backup}
        atomic_json(self.state_path, state)
        self.build_progress('Complete')
        self.record('Build verified. Use “Activate build” to select it; startup is a separate action.')

    def activate(self):
        build = self.read_state().get('build')
        if not build:
            raise RuntimeError('No successful dashboard build is available.')
        actual = self.run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'cat', build['image'], '/opt/turtle/SOURCE_COMMIT'])
        if actual != build['revision']:
            raise RuntimeError('Build image changed; activation cancelled.')
        self.shutdown()
        self.cold_backup()
        replace_image(self.deployment / '.env', build['image'])
        self.record('Selected ' + build['image'] + '. Start the server when ready.')

    def execute(self, action, revision=None, reserved_fd=None):
        if action not in ACTIONS:
            raise RuntimeError('Unknown action.')
        lock = contextlib.nullcontext(reserved_fd) if reserved_fd is not None else operation_lock(self.lock_path)
        with lock as fd:
            self.fd = fd
            self.operation_thread = threading.get_ident()
            operation_id = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S-') + secrets.token_hex(3)
            self.log_path = self.state_dir / (operation_id + '.log')
            self.log_path.touch(mode=0o600)
            self.operation = {'id': operation_id, 'action': action, 'status': 'running', 'started_at': now(), 'message': 'Starting…'}
            self.record('Starting ' + action)
            error = None
            try:
                if action == 'start':
                    self.start()
                elif action == 'stop':
                    self.shutdown()
                elif action == 'build':
                    self.build(revision)
                elif action == 'activate':
                    self.activate()
                elif action == 'backup':
                    running = {service for service, item in self.containers().items() if item['State']['Running']}
                    self.shutdown()
                    self.operation['backup'] = self.cold_backup()
                    if running:
                        self.record('Restoring the services that were running before backup…')
                        self.compose('up', '-d', *sorted(running), timeout=240)
                self.operation['status'] = 'succeeded'
            except Exception as exc:
                error = self.redact(str(exc))
                self.record(error)
                self.operation['status'] = 'failed'
            finally:
                self.operation['finished_at'] = now()
                state = self.read_state()
                state.setdefault('history', []).insert(0, self.operation)
                state['history'] = state['history'][:50]
                state['active'] = None
                atomic_json(self.state_path, state)
                self.fd = self.log_path = self.operation = None
                self.operation_thread = None
            if error:
                raise RuntimeError(error)

    def submit(self, action):
        stream = self.lock_path.open('a+')
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            raise RuntimeError('Another operation is running. Wait for it to finish.')
        def work():
            try:
                self.execute(action, reserved_fd=stream.fileno())
            except RuntimeError:
                pass  # Failure is persisted by execute; no credential output.
            finally:
                stream.close()
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        while not self.stop_poll.is_set():
            try:
                items = self.containers()
                services = {}
                for service in ('db', 'realmd', 'mangosd'):
                    item = items.get(service)
                    services[service] = {'state': item['State']['Status'] if item else 'missing',
                                         'ready': False, 'restarts': item.get('RestartCount', 0) if item else 0,
                                         'exit_code': item['State'].get('ExitCode') if item else None,
                                         'oom_killed': item['State'].get('OOMKilled', False) if item else False}
                    if item and service == 'db':
                        services[service]['ready'] = item['State'].get('Health', {}).get('Status') == 'healthy'
                env = env_values(self.deployment / '.env')
                host = env.get('GAME_BIND_IP', '127.0.0.1')
                if host in ('0.0.0.0', '::'):
                    host = '127.0.0.1'
                for service, key, port in [('realmd', 'REALM_PORT', 3724), ('mangosd', 'WORLD_PORT', 8090)]:
                    if services[service]['state'] == 'running':
                        try:
                            with socket.create_connection((host, int(env.get(key, port))), timeout=1):
                                services[service]['ready'] = True
                        except OSError:
                            pass
                world = items.get('mangosd')
                counts = None
                if world and world['State']['Running']:
                    world_port_ready = services['mangosd']['ready']
                    logs = self.compose('logs', '--no-color', '--timestamps', '--since', world['State']['StartedAt'], '--tail', '350', 'mangosd')
                    ready = services['mangosd']['ready'] and 'World server is up and running' in logs
                    # Keep the readiness observation for this exact container start,
                    # even after the startup line scrolls out of the log tail.
                    generation = world['Id'] + world['State']['StartedAt']
                    if self.count_container != generation:
                        self.count_container, self.count_sample = generation, None
                        self.world_ready = False
                    self.world_ready = getattr(self, 'world_ready', False) or ready
                    services['mangosd']['ready'] = services['mangosd']['ready'] and self.world_ready
                    if services['mangosd']['state'] == 'running' and not lock_busy(self.lock_path):
                        if time.monotonic() - self.last_count_request > 15:
                            self.compose('exec', '-T', '-u', 'turtle', 'mangosd', 'sh', '-c',
                                         'printf "rndbot diff\\n" > /opt/turtle/run/mangosd.in', timeout=3)
                            self.last_count_request = time.monotonic()
                        native = parse_counts(logs)
                        if native:
                            matches = re.findall(r'(\d{4}-\d\d-\d\dT[^\s]+).*Bots online:', logs)
                            sampled_at = matches[-1] if matches else now()
                            self.count_sample = {'values': native, 'sampled_at': sampled_at}
                            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(sampled_at.replace('Z', '+00:00'))).total_seconds()
                            counts = native if age < 45 and world_port_ready else None
                            if counts is not None:
                                services['mangosd']['ready'] = True
                                self.world_ready = True
                elif services['mangosd']['state'] in ('exited', 'created', 'missing'):
                    counts = {'users': 0, 'bots': 0, 'active_bots': 0}
                    self.count_sample = None
                version, image = None, None
                if world:
                    image = world['Config']['Image']
                    version = world['Config'].get('Labels', {}).get('org.opencontainers.image.revision')
                memory = dict(line.split(':', 1) for line in pathlib.Path('/proc/meminfo').read_text().splitlines())
                total = int(memory['MemTotal'].split()[0]) * 1024
                available = int(memory['MemAvailable'].split()[0]) * 1024
                disk = shutil.disk_usage(self.deployment)
                head = self.run(['git', '-C', str(self.core), 'rev-parse', 'HEAD'])
                selected = env.get('TURTLE_IMAGE', 'ghcr.io/pjw345/tortoise-docker:' + env.get('TAG', 'playerbots'))
                self.snapshot = {'sampled_at': now(), 'services': services, 'counts': counts,
                                 'counts_sampled_at': (self.count_sample or {}).get('sampled_at'),
                                 'image': image, 'version': version, 'selected_image': selected, 'development_revision': head,
                                 'disk': {'total': disk.total, 'free': disk.free},
                                 'memory': {'total': total, 'used': total - available},
                                 'load': os.getloadavg()[0], 'error': None}
                failures = [f'{name} exited with code {service["exit_code"]}' for name, service in services.items()
                            if service['state'] == 'exited' and service['exit_code'] not in (None, 0)]
                self.snapshot['warning'] = '. '.join(failures) + '. Inspect server logs before restarting.' if failures else None
            except Exception as exc:
                self.snapshot = {'sampled_at': now(), 'services': {}, 'counts': None, 'error': self.redact(str(exc))}
            self.stop_poll.wait(5)

    def status(self):
        state = self.read_state()
        result = dict(self.snapshot)
        result.update(active=state.get('active'), history=state.get('history', []), build=state.get('build'), busy=lock_busy(self.lock_path))
        operation = result['active'] or next(iter(result['history']), None)
        result['log'] = ''
        if operation:
            path = self.state_dir / (operation['id'] + '.log')
            if path.exists():
                with path.open('rb') as stream:
                    stream.seek(max(0, path.stat().st_size - 12000))
                    result['log'] = self.redact(stream.read().decode('utf-8', errors='replace'))
        backups = []
        if self.backups.exists():
            for path in sorted(self.backups.iterdir(), reverse=True):
                if path.is_dir() and (path / 'SHA256SUMS').exists() and not (path / 'INCOMPLETE').exists():
                    backups.append({'name': path.name, 'size': sum(p.stat().st_size for p in path.iterdir() if p.is_file())})
        result['backups'] = backups[:10]
        return result


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'TurtleWoWManager'

    def log_message(self, fmt, *args):
        # Avoid logging paths or credentials.
        pass

    def reply(self, status, payload, mime='application/json'):
        raw = json.dumps(payload).encode() if mime == 'application/json' else payload
        self.send_response(status)
        self.send_header('Content-Type', mime + '; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        if getattr(self, 'cookie', None):
            self.send_header('Set-Cookie', self.cookie)
        self.end_headers()
        self.wfile.write(raw)

    def permitted(self):
        origin = self.server.manager.config.get('public_origin', 'https://turtlewow-server')
        if self.headers.get('Host', '') != urlsplit(origin).netloc:
            self.reply(403, {'error': 'Use the configured dashboard address.'})
            return False
        return True

    def session(self):
        return self.server.auth.session(self.headers.get('Cookie'))

    def do_GET(self):
        if not self.permitted():
            return
        path = urlsplit(self.path).path
        token, session = self.session()
        if path == '/admin/api/session':
            self.reply(200, {'authenticated': bool(session), 'csrf': session['csrf'] if session else None,
                             'setup_required': not self.server.auth.path.exists()})
        elif path == '/admin/api/status':
            self.reply(200, self.server.manager.status()) if session else self.reply(401, {'error': 'Sign in required.'})
        elif path in ('/admin', '/admin/', '/admin/app.js', '/admin/style.css'):
            leaf = 'index.html' if path in ('/admin', '/admin/') else path.rsplit('/', 1)[1]
            mime = {'index.html': 'text/html', 'app.js': 'text/javascript', 'style.css': 'text/css'}[leaf]
            self.reply(200, (ROOT / 'web' / leaf).read_bytes(), mime)
        else:
            self.reply(404, {'error': 'Not found'})

    def do_POST(self):
        if not self.permitted():
            return
        expected = self.server.manager.config.get('public_origin', 'https://turtlewow-server')
        if self.headers.get('Origin') != expected:
            self.reply(403, {'error': 'Invalid request origin.'})
            return
        if self.headers.get('Content-Type') != 'application/json':
            self.reply(415, {'error': 'JSON required'})
            return
        path = urlsplit(self.path).path
        token, session = self.session()
        if path != '/admin/api/login':
            if not session:
                self.reply(401, {'error': 'Sign in required.'})
                return
            if not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), session['csrf']):
                self.reply(403, {'error': 'Refresh the dashboard before submitting an action.'})
                return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2048:
                raise ValueError('Invalid request size')
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError('JSON object required')
            if path == '/admin/api/login':
                username, password = value.get('username'), value.get('password')
                if not isinstance(username, str) or not isinstance(password, str):
                    raise ValueError('Username and password required')
                token, error = self.server.auth.login(username, password)
                if error:
                    self.reply(401, {'error': error})
                    return
                self.cookie = 'turtlewow_session=' + token + '; Path=/admin; HttpOnly; Secure; SameSite=Strict; Max-Age=28800'
                self.reply(200, {'authenticated': True, 'csrf': self.server.auth.sessions[token]['csrf']})
            elif path == '/admin/api/logout':
                self.server.auth.logout(token)
                self.cookie = 'turtlewow_session=; Path=/admin; HttpOnly; Secure; SameSite=Strict; Max-Age=0'
                self.reply(200, {'authenticated': False})
            elif path == '/admin/api/action':
                action = value.get('action')
                if action not in ACTIONS:
                    raise ValueError('Unknown action')
                self.server.manager.submit(action)
                self.reply(202, {'accepted': True})
            else:
                self.reply(404, {'error': 'Not found'})
        except (ValueError, AttributeError) as exc:
            self.reply(400, {'error': str(exc)})
        except RuntimeError as exc:
            self.reply(409, {'error': str(exc)})

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['serve', 'start', 'stop', 'backup', 'build', 'activate', 'status', 'create-admin'])
    parser.add_argument('revision', nargs='?')
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == 'create-admin':
        create_admin(CONFIG.parent / 'account.json')
        return
    manager = Manager(load_json(CONFIG, {}))
    if args.command == 'serve':
        threading.Thread(target=manager.poll, daemon=True).start()
        server = http.server.ThreadingHTTPServer(('127.0.0.1', manager.config.get('port', 8787)), Handler)
        server.manager = manager
        server.auth = Auth(CONFIG.parent / 'account.json')
        print('TurtleWoW manager listening on loopback port ' + str(server.server_port), flush=True)
        try:
            server.serve_forever()
        finally:
            manager.stop_poll.set()
            server.server_close()
    elif args.command == 'status':
        print(json.dumps(manager.status(), indent=2))
    else:
        manager.execute(args.command, args.revision)
        print('Operation completed. See the dashboard activity log for details.')


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
