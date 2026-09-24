"""End-to-end smoke tests against a fake rtorrent - no real rtorrent needed.

Spawns the fake SCGI server from fake_rtorrent.py plus a real (daemonized)
server_wss.py in an isolated temp directory, then exercises the full protocol:
registration per view, detail requests, diff pushes, per-view filtering of new
torrents, set_global writes (allowlist, validation, untrusted-safe header,
immediate push), error handling, and updater resilience to an rtorrent outage.

The fake mimics rtorrent 0.16.x (current command names, api_version 26,
compact tinyxml2 XML), matching what this server version requires.
"""
import asyncio
import hashlib
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time

import pytest
import websockets

from rpc import RTorrentRpc

from .fake_rtorrent import FakeRtorrent, State

SECRET = 'smoketest'
HASH_A, HASH_B, HASH_C = 'A' * 40, 'B' * 40, 'C' * 40
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTERVAL = 1
# far above any test timeout: a push arriving promptly on the slow server can
# only come from the immediate post-write tick, never from a regular poll
SLOW_INTERVAL = 60
PUSH_TIMEOUT = INTERVAL + 4


class Server:
    def __init__(self, tmp):
        self.tmp = tmp
        self.sock_path = os.path.join(tmp, 'fake.sock')
        self.pid_path = os.path.join(tmp, 'server.pid')
        self.log_path = os.path.join(tmp, 'server.log')
        self.state = State()
        self.state.populate_default()
        self.fake = FakeRtorrent(self.sock_path, self.state)
        self.port = None
        self.pid = None

    @property
    def uri(self):
        return 'wss://127.0.0.1:%d' % self.port

    def rpc(self):
        return RTorrentRpc(self.sock_path)

    def restart_fake(self):
        self.fake = FakeRtorrent(self.sock_path, self.state)
        self.fake.start()


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _spawn_server(interval, extra_env=None):
    tmp = tempfile.mkdtemp(prefix='rtr_smoke_')
    server = Server(tmp)
    server.fake.start()
    server.port = _free_port()
    env = dict(os.environ)
    env.update({
        'RTR_CERT_PATH': os.path.join(REPO, 'cert', 'cert.pem'),
        'RTR_LISTEN_HOST': '127.0.0.1',
        'RTR_LISTEN_PORT': str(server.port),
        'RTR_SCGI_SOCKET_PATH': server.sock_path,
        'RTR_PID_PATH': server.pid_path,
        'RTR_LOG_PATH': server.log_path,
        'RTR_RETR_INTERVAL': str(interval),
        'RTR_SHORT_CACHE_TTL': str(INTERVAL),
        'RTR_SCGI_TIMEOUT': '5',
        'RTR_SECRET_KEY_SHA1': hashlib.sha1(SECRET.encode()).hexdigest(),
        # a nonexistent path keeps disk_usage constant (zeros): pushes stay deterministic
        'RTR_PLUGINS_DISK_USAGE_PATHS': os.path.join(tmp, 'nonexistent'),
        'PYTHONPATH': REPO,
    })
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen([sys.executable, os.path.join(REPO, 'server_wss.py')], env=env, cwd=REPO)
    proc.wait()  # the daemon detaches; the launcher exits immediately
    deadline = time.time() + 15
    while time.time() < deadline:
        if os.path.isfile(server.pid_path):
            with open(server.pid_path) as f:
                server.pid = int(f.read().strip())
            break
        time.sleep(0.1)
    assert server.pid is not None, 'server did not write a PID file; log:\n%s' % _read_log(server)
    while time.time() < deadline:
        try:
            s = socket.create_connection(('127.0.0.1', server.port), 0.2)
            s.close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        pytest.fail('server never opened its port; log:\n%s' % _read_log(server))
    return server


def _stop_server(server):
    try:
        os.kill(server.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            os.kill(server.pid, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            break
    else:
        os.kill(server.pid, signal.SIGKILL)
    assert not os.path.isfile(server.pid_path), 'PID file was not removed on shutdown'
    server.fake.stop()


@pytest.fixture(scope='module')
def srv():
    server = _spawn_server(INTERVAL)
    yield server
    _stop_server(server)


@pytest.fixture(scope='module')
def srv_slow():
    server = _spawn_server(SLOW_INTERVAL)
    yield server
    _stop_server(server)


@pytest.fixture(scope='module')
def srv_data():
    # a server with RTR_DATA_ROOT set, so erase-with-data and move-data are enabled;
    # the fake and this test share the filesystem, so the fs ops are observable
    data_root = tempfile.mkdtemp(prefix='rtr_data_')
    server = _spawn_server(INTERVAL, {'RTR_DATA_ROOT': data_root})
    server.data_root = data_root
    yield server
    _stop_server(server)
    shutil.rmtree(data_root, ignore_errors=True)


def _read_log(server):
    try:
        with open(server.log_path) as f:
            return f.read()
    except OSError:
        return '<no log>'


def _ssl_ctx():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _req(method, params=None, id=1):
    obj = {'jsonrpc': '2.0', 'id': id, 'method': method}
    if params is not None:
        obj['params'] = params
    return json.dumps(obj)


async def _connect(srv, view=None, id=1):
    ws = await websockets.connect(srv.uri, ssl=_ssl_ctx())
    params = {'secret_key': SECRET}
    if view:
        params['view'] = view
    await ws.send(_req('register', params, id=id))
    response = json.loads(await asyncio.wait_for(ws.recv(), 5))
    return ws, response


async def _collect(ws, seconds):
    frames = []
    end = asyncio.get_running_loop().time() + seconds
    while True:
        left = end - asyncio.get_running_loop().time()
        if left <= 0:
            return frames
        try:
            frames.append(json.loads(await asyncio.wait_for(ws.recv(), left)))
        except asyncio.TimeoutError:
            return frames
        except websockets.exceptions.ConnectionClosed:
            return frames


async def _wait_for(ws, predicate, timeout=PUSH_TIMEOUT):
    end = asyncio.get_running_loop().time() + timeout
    while True:
        left = end - asyncio.get_running_loop().time()
        assert left > 0, 'expected push not received in time'
        frame = json.loads(await asyncio.wait_for(ws.recv(), left))
        if predicate(frame):
            return frame


def test_register_main(srv):
    async def run():
        ws, response = await _connect(srv, id=7)
        result = response['result']
        assert response['id'] == 7
        assert 'version' in result
        assert result['rtremote_protocol_version'] == 8
        assert result['global']['throttle_global_down_max_rate'] == 1024
        assert result['global']['network_http_max_open'] == 32
        assert result['global']['system_api_version'] == 26
        assert result['global']['network_port_range'] == '22400-22400'
        # wire names must stay stable although the underlying rtorrent commands
        # are now system.sockets.size / system.sockets.max_size
        assert result['global']['network_open_sockets'] == 3
        assert result['global']['network_max_open_sockets'] == 1048576
        # global unchoked caps, fetched since protocol level 2 (0 = auto)
        assert result['global']['throttle_max_uploads_global'] == 0
        assert result['global']['throttle_max_downloads_global'] == 0
        hashes = [t['hash'] for t in result['torrents']]
        assert hashes == [HASH_A, HASH_B]
        torrent_b = result['torrents'][1]
        # B has a busy (announcing) tracker: digest must be embedded
        assert torrent_b['has_active_not_scrape'] == 1
        assert torrent_b['trackers'] == [{'group': 0, 'url': 'http://tr2.example.org:8080/announce',
                                          'is_busy_not_scrape': 1}]
        assert result['plugins']['disk_usage'] == {'total': 0, 'used': 0, 'free': 0}
        await ws.close()
    asyncio.run(run())


def test_register_views(srv):
    async def run():
        ws, response = await _connect(srv, view='stopped')
        assert [t['hash'] for t in response['result']['torrents']] == [HASH_B]
        await ws.close()
        ws, response = await _connect(srv, view='name')
        assert [t['hash'] for t in response['result']['torrents']] == [HASH_A, HASH_B]
        await ws.close()
        # unknown view falls back to main
        ws, response = await _connect(srv, view='bogus')
        assert [t['hash'] for t in response['result']['torrents']] == [HASH_A, HASH_B]
        await ws.close()
    asyncio.run(run())


def test_detail_requests(srv):
    async def run():
        ws, _ = await _connect(srv)
        await ws.send(_req('get_files', {'hash': HASH_A}, id=2))
        files = json.loads(await asyncio.wait_for(ws.recv(), 5))['result']['files']
        assert files == [{'size_chunks': 100, 'completed_chunks': 100, 'priority': 1,
                          'size_bytes': 1000000, 'path': 'alpha.iso'}]
        await ws.send(_req('get_peers', {'hash': HASH_A}, id=3))
        peers = json.loads(await asyncio.wait_for(ws.recv(), 5))['result']['peers']
        assert peers == []
        await ws.send(_req('get_trackers', {'hash': HASH_B}, id=4))
        trackers = json.loads(await asyncio.wait_for(ws.recv(), 5))['result']['trackers']
        assert len(trackers) == 1
        assert trackers[0]['url'] == 'http://tr2.example.org:8080/announce'
        assert trackers[0]['is_busy_not_scrape'] == 1
        await ws.close()
    asyncio.run(run())


def test_no_pushes_when_idle(srv):
    async def run():
        ws, _ = await _connect(srv)
        frames = await _collect(ws, INTERVAL * 2.5)
        assert frames == []
        await ws.close()
    asyncio.run(run())


def test_global_change_push(srv):
    async def run():
        ws, _ = await _connect(srv)
        srv.rpc().call('network.http.max_total_connections.set', [('string', ''), ('i8', 33)])
        try:
            frame = await _wait_for(ws, lambda f: 'global' in f['result'])
            assert frame['result']['global']['network_http_max_open'] == 33
        finally:
            srv.rpc().call('network.http.max_total_connections.set', [('string', ''), ('i8', 32)])
        await ws.close()
    asyncio.run(run())


def test_torrent_change_push(srv):
    async def run():
        ws, _ = await _connect(srv)
        srv.rpc().call('d.ignore_commands.set', [('string', HASH_B), ('i8', 1)])
        try:
            frame = await _wait_for(ws, lambda f: 'torrents' in f['result'])
            changed = frame['result']['torrents']['changed']
            assert changed == [{'ignore_commands': 1, 'hash': HASH_B}]
        finally:
            srv.rpc().call('d.ignore_commands.set', [('string', HASH_B), ('i8', 0)])
        await ws.close()
    asyncio.run(run())


def test_new_torrent_filtered_per_view(srv):
    async def run():
        ws_main, _ = await _connect(srv)
        ws_stopped, _ = await _connect(srv, view='stopped')
        srv.rpc().call('fake.add_torrent',
                       [('string', HASH_C), ('string', 'charlie.iso'), ('i8', 3000000), ('i8', 1)])
        try:
            frame = await _wait_for(ws_main, lambda f: 'torrents' in f['result'])
            new = frame['result']['torrents']['new']
            assert [t['hash'] for t in new] == [HASH_C]
            # C is open, so the stopped view must NOT receive it as 'new'
            frames = await _collect(ws_stopped, INTERVAL * 1.5)
            for f in frames:
                assert 'new' not in f.get('result', {}).get('torrents', {})
        finally:
            srv.rpc().call('fake.remove_torrent', [('string', HASH_C)])
        # deletions are broadcast to every view
        frame = await _wait_for(ws_main, lambda f: 'torrents' in f['result'])
        assert frame['result']['torrents']['del'] == [HASH_C]
        frame = await _wait_for(ws_stopped, lambda f: 'torrents' in f['result'])
        assert frame['result']['torrents']['del'] == [HASH_C]
        await ws_main.close()
        await ws_stopped.close()
    asyncio.run(run())


def test_error_handling(srv):
    async def run():
        # valid JSON that is not a JSON-RPC request: connection is dropped
        ws = await websockets.connect(srv.uri, ssl=_ssl_ctx())
        await ws.send(json.dumps({'jsonrpc': '2.0', 'id': 9}))
        frames = await _collect(ws, 1.5)
        assert frames == []
        assert ws.state.name == 'CLOSED'

        # unknown method: JSON-RPC error, connection stays usable
        ws, _ = await _connect(srv)
        await ws.send(_req('get_bogus', {'hash': HASH_A}, id=41))
        frame = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert frame['error']['code'] == -32601
        await ws.send(_req('get_files', {'hash': HASH_A}, id=42))
        frame = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert 'files' in frame['result']

        # malformed hash: JSON-RPC error, connection stays usable
        await ws.send(_req('get_files', {'hash': 'not-a-hash'}, id=43))
        frame = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert frame['error']['code'] == -32602

        # unknown-but-wellformed hash: rtorrent fault becomes a JSON-RPC error
        await ws.send(_req('get_files', {'hash': 'D' * 40}, id=44))
        frame = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert frame['error']['code'] == -32603
        await ws.send(_req('get_files', {'hash': HASH_A}, id=45))
        frame = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert 'files' in frame['result']
        await ws.close()

        # wrong secret: dropped
        ws = await websockets.connect(srv.uri, ssl=_ssl_ctx())
        await ws.send(_req('register', {'secret_key': 'wrong'}))
        frames = await _collect(ws, 1.5)
        assert frames == []
        assert ws.state.name == 'CLOSED'

        # request before registering: dropped
        ws = await websockets.connect(srv.uri, ssl=_ssl_ctx())
        await ws.send(_req('get_files', {'hash': HASH_A}))
        frames = await _collect(ws, 1.5)
        assert frames == []
        assert ws.state.name == 'CLOSED'
    asyncio.run(run())


def test_re_register_switches_view(srv):
    async def run():
        ws, response = await _connect(srv, id=80)
        assert response['id'] == 80
        await ws.send(_req('register', {'secret_key': SECRET, 'view': 'stopped'}, id=81))
        response = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert response['id'] == 81
        assert [t['hash'] for t in response['result']['torrents']] == [HASH_B]
        await ws.close()
    asyncio.run(run())


def test_survives_rtorrent_outage(srv):
    async def run():
        ws, _ = await _connect(srv)
        srv.fake.stop()
        await asyncio.sleep(INTERVAL * 2.5)  # let a few polls fail
        srv.restart_fake()
        srv.rpc().call('network.http.max_total_connections.set', [('string', ''), ('i8', 40)])
        try:
            frame = await _wait_for(ws, lambda f: 'global' in f['result'], timeout=PUSH_TIMEOUT + INTERVAL * 2)
            assert frame['result']['global']['network_http_max_open'] == 40
        finally:
            srv.rpc().call('network.http.max_total_connections.set', [('string', ''), ('i8', 32)])
        await ws.close()
    asyncio.run(run())
    # the outage is blamed on rtorrent, logged once at its start and once at recovery
    # (not a traceback per failed poll), with a hint on what to check
    log = _read_log(srv)
    failed = [l for l in log.splitlines() if 'rtorrent poll failed:' in l]
    assert len(failed) == 1, log
    assert '|ERROR|rtorrent|' in failed[0] and ' -> ' in failed[0]
    assert 'Traceback' not in log
    assert sum('|INFO|rtorrent|' in l and 'rtorrent is back after' in l for l in log.splitlines()) == 1, log


def test_log_names_the_component(srv):
    # every diagnostic line carries a "where" column: rtorrent / app / rtremote
    log = _read_log(srv)
    assert any('|INFO|rtremote|' in l and 'starting rtremote' in l and 'protocol level' in l for l in log.splitlines())
    assert any('|INFO|rtremote|' in l and 'config: listen=' in l for l in log.splitlines())
    assert any('|INFO|rtorrent|' in l and 'connected: rtorrent' in l for l in log.splitlines())
    assert any('|INFO|rtremote|' in l and 'listening on wss://' in l for l in log.splitlines())

    async def run():
        # wrong secret: silently dropped on the wire, blamed on the app in the log
        ws = await websockets.connect(srv.uri, ssl=_ssl_ctx())
        await ws.send(_req('register', {'secret_key': 'wrong'}, id=7))
        assert await _collect(ws, 1.0) == []

        ws, _ = await _connect(srv)
        # unknown method: the app is newer than rtremote
        await ws.send(_req('no_such_method', {}, id=8))
        frame = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert frame['error']['code'] == -32601 and frame['error']['message'].startswith('rtremote: ')
        # invalid params: the app sent something rtremote does not accept
        await ws.send(_req('set_global', {'key': 'no_such_key', 'value': 1}, id=9))
        frame = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert frame['error']['code'] == -32602
        # rtorrent fault on a stale hash: error text says who is to blame
        await ws.send(_req('torrent_action', {'hash': 'D' * 40, 'action': 'start'}, id=10))
        frame = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert frame['error']['code'] == -32603
        assert frame['error']['message'].startswith('app: rtorrent has no torrent with that hash'), frame
        await ws.close()

        # a client that does not trust the certificate: asyncio would swallow this
        ctx = ssl.create_default_context()  # system CAs: our self-signed cert is rejected
        try:
            await websockets.connect(srv.uri, ssl=ctx, server_hostname='localhost')
        except Exception:
            pass
        else:
            pytest.fail('the test certificate must not be trusted by default')
        # plain text on the TLS port
        raw = socket.create_connection(('127.0.0.1', srv.port), 2)
        raw.sendall(b'GET / HTTP/1.0\r\n\r\n')
        try:
            raw.recv(64)
        except OSError:
            pass
        raw.close()
    asyncio.run(run())

    deadline = time.time() + 5
    while time.time() < deadline:
        log = _read_log(srv)
        lines = log.splitlines()
        checks = [
            any('|WARNING|app|' in l and 'secret key mismatch' in l and 'RTR_SECRET_KEY_SHA1' in l for l in lines),
            any('|WARNING|app|' in l and "unknown method 'no_such_method'" in l and 'update rtremote' in l
                for l in lines),
            any('|WARNING|app|' in l and 'rejected set_global' in l for l in lines),
            any('|WARNING|app|' in l and 'write torrent_action' in l and 'no torrent with that hash' in l
                for l in lines),
            any('|WARNING|app|' in l and 'closed the connection during the TLS handshake' in l
                and 'accept self-signed' in l for l in lines),
            # the harness's own TCP port probe at startup must not read as a failed app connection
            any('|INFO|app|' in l and 'closed before sending any TLS data' in l for l in lines),
            any('|WARNING|app|' in l and 'plain text to the TLS port' in l for l in lines),
        ]
        if all(checks):
            break
        time.sleep(0.2)
    assert all(checks), 'missing diagnostics %s in log:\n%s' % (checks, log)
    assert 'Traceback' not in log, log


async def _await_response_and_push(ws, req_id, push_pred, timeout=PUSH_TIMEOUT):
    # the write response and the resulting broadcast race on the same socket;
    # collect frames until both have arrived, ignoring unrelated pushes
    response, push = None, None
    end = asyncio.get_running_loop().time() + timeout
    while response is None or push is None:
        left = end - asyncio.get_running_loop().time()
        assert left > 0, 'missing %s' % ('response' if response is None else 'push')
        frame = json.loads(await asyncio.wait_for(ws.recv(), left))
        if response is None and frame.get('id') == req_id:
            assert 'error' not in frame, frame
            response = frame
        elif push is None and push_pred(frame):
            push = frame
    return response, push


def _was_untrusted(srv, command):
    data = srv.rpc().call('fake.was_untrusted', [('string', command)])
    return data['methodResponse']['params']['param']['value'] == {'i8': 1}


def test_set_global_success_push_and_untrusted_header(srv):
    async def run():
        ws, _ = await _connect(srv)
        await ws.send(_req('set_global', {'key': 'throttle_max_uploads', 'value': 7}, id=90))
        try:
            response, push = await _await_response_and_push(
                ws, 90, lambda f: f.get('result', {}).get('global', {}).get('throttle_max_uploads') == 7)
            assert response['result'] == {'key': 'throttle_max_uploads', 'value': 7}
            assert push['result']['global']['throttle_max_uploads'] == 7
            # throttle.max_uploads.set is on rtorrent's untrusted-safe list, so the
            # defence-in-depth header must have been sent (the fake enforces the
            # allowlist, so success alone already proves the call was acceptable)
            assert _was_untrusted(srv, 'throttle.max_uploads.set')
        finally:
            srv.rpc().call('throttle.max_uploads.set', [('string', ''), ('i8', 50)])
        await ws.close()
    asyncio.run(run())


def test_set_global_kb_scaling(srv):
    async def run():
        ws, _ = await _connect(srv)
        await ws.send(_req('set_global', {'key': 'throttle_global_up_max_rate', 'value': 2048}, id=91))
        try:
            # the wire value is KB (throttle.global_up.max_rate.set_kb); rtorrent
            # reports the rate back in bytes
            response, push = await _await_response_and_push(
                ws, 91,
                lambda f: f.get('result', {}).get('global', {}).get('throttle_global_up_max_rate') == 2048 * 1024)
            assert 'error' not in response
            assert _was_untrusted(srv, 'throttle.global_up.max_rate.set_kb')
        finally:
            srv.rpc().call('throttle.global_up.max_rate.set_kb', [('string', ''), ('i8', 1)])
        await ws.close()
    asyncio.run(run())


def test_set_global_not_untrusted_safe_commands(srv):
    # system.sockets.max_size.set and network.listen.port.set are NOT on
    # rtorrent's untrusted-safe list: they must succeed as trusted calls,
    # i.e. without the UNTRUSTED_CONNECTION header (the fake faults otherwise)
    async def run():
        ws, _ = await _connect(srv)
        await ws.send(_req('set_global', {'key': 'network_max_open_sockets', 'value': 2048}, id=92))
        try:
            response, push = await _await_response_and_push(
                ws, 92, lambda f: f.get('result', {}).get('global', {}).get('network_max_open_sockets') == 2048)
            assert response['result']['value'] == 2048
            assert not _was_untrusted(srv, 'system.sockets.max_size.set')
        finally:
            srv.rpc().call('system.sockets.max_size.set', [('string', ''), ('i8', 1048576)])

        await ws.send(_req('set_global', {'key': 'network_listen_port', 'value': 22401}, id=93))
        try:
            response, push = await _await_response_and_push(
                ws, 93, lambda f: f.get('result', {}).get('global', {}).get('network_listen_port') == 22401)
            assert response['result']['value'] == 22401
            assert not _was_untrusted(srv, 'network.listen.port.set')
        finally:
            srv.rpc().call('network.listen.port.set', [('string', ''), ('i8', 22400)])
        await ws.close()
    asyncio.run(run())


def test_set_global_rejections(srv):
    async def run():
        ws, _ = await _connect(srv)

        async def expect_error(params, code, id):
            await ws.send(_req('set_global', params, id=id))
            frame = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert frame['id'] == id
            assert frame['error']['code'] == code, params

        # key outside the allowlist (a real read-only field, and garbage)
        await expect_error({'key': 'network_max_open_files', 'value': 1}, -32602, 50)
        await expect_error({'key': 'session_path', 'value': 1}, -32602, 51)
        await expect_error({'key': 42, 'value': 1}, -32602, 52)
        # non-integer values (bool is an int subclass in Python: must not pass)
        await expect_error({'key': 'throttle_max_uploads', 'value': '7'}, -32602, 53)
        await expect_error({'key': 'throttle_max_uploads', 'value': 7.5}, -32602, 54)
        await expect_error({'key': 'throttle_max_uploads', 'value': True}, -32602, 55)
        await expect_error({'key': 'throttle_max_uploads'}, -32602, 56)
        # out of range
        await expect_error({'key': 'throttle_max_uploads', 'value': -1}, -32602, 57)
        await expect_error({'key': 'network_listen_port', 'value': 0}, -32602, 58)
        await expect_error({'key': 'network_listen_port', 'value': 65536}, -32602, 59)
        await expect_error({'key': 'throttle_max_uploads', 'value': 2 ** 63}, -32602, 60)

        # unknown method with a 'key' param
        await ws.send(_req('set_bogus', {'key': 'throttle_max_uploads', 'value': 1}, id=61))
        frame = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert frame['error']['code'] == -32601

        # rtorrent fault surfaces as -32603 and the connection stays usable
        srv.rpc().call('fake.fail_next_set', [])
        await ws.send(_req('set_global', {'key': 'throttle_max_uploads', 'value': 3}, id=62))
        frame = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert frame['error']['code'] == -32603
        await ws.send(_req('get_files', {'hash': HASH_A}, id=63))
        frame = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert 'files' in frame['result']
        await ws.close()
    asyncio.run(run())


def test_set_global_before_register_dropped(srv):
    async def run():
        ws = await websockets.connect(srv.uri, ssl=_ssl_ctx())
        await ws.send(_req('set_global', {'key': 'throttle_max_uploads', 'value': 1}))
        frames = await _collect(ws, 1.5)
        assert frames == []
        assert ws.state.name == 'CLOSED'
        # the value must not have been applied
        data = srv.rpc().call('throttle.max_uploads', [('string', '')])
        assert data['methodResponse']['params']['param']['value'] == {'i8': 50}
    asyncio.run(run())


def test_set_global_pushes_immediately(srv_slow):
    # on a server polling every SLOW_INTERVAL seconds, a broadcast within a few
    # seconds of the write can only come from the immediate post-write tick
    async def run():
        ws, response = await _connect(srv_slow)
        assert response['result']['global']['throttle_max_downloads'] == 50
        await ws.send(_req('set_global', {'key': 'throttle_max_downloads', 'value': 9}, id=95))
        response, push = await _await_response_and_push(
            ws, 95, lambda f: f.get('result', {}).get('global', {}).get('throttle_max_downloads') == 9,
            timeout=PUSH_TIMEOUT)
        assert response['result'] == {'key': 'throttle_max_downloads', 'value': 9}
        await ws.close()
    asyncio.run(run())


# ---- M1-M7 write methods -------------------------------------------------

async def _await_id(ws, req_id, timeout=5):
    # a write response and unrelated pushes race on the socket; return the frame
    # whose id matches the request
    end = asyncio.get_running_loop().time() + timeout
    while True:
        left = end - asyncio.get_running_loop().time()
        assert left > 0, 'no response for id %s' % req_id
        frame = json.loads(await asyncio.wait_for(ws.recv(), left))
        if frame.get('id') == req_id:
            return frame


async def _call(ws, method, params, id):
    await ws.send(_req(method, params, id=id))
    return await _await_id(ws, id)


def _add(srv, hash, **kw):
    kw.setdefault('is_open', 1)
    kw.setdefault('is_active', 1)
    srv.state.add_torrent(hash, kw.pop('name', 'demo.iso'), kw.pop('size', 1000000), **kw)


def test_m1_torrent_actions(srv):
    h = '1' * 40
    _add(srv, h, name='m1.iso', is_open=1, is_active=1)
    async def run():
        ws, _ = await _connect(srv)
        r = await _call(ws, 'torrent_action', {'hash': h, 'action': 'stop'}, 201)
        assert r['result'] == {'hash': h, 'action': 'stop'}
        assert srv.state.torrents[h]['d.is_open'] == 0
        assert await _call(ws, 'torrent_action', {'hash': h, 'action': 'start'}, 202)
        assert srv.state.torrents[h]['d.is_open'] == 1
        await _call(ws, 'torrent_action', {'hash': h, 'action': 'pause'}, 203)
        assert srv.state.torrents[h]['d.is_active'] == 0
        for i, action in enumerate(('resume', 'close', 'open', 'check_hash', 'announce')):
            r = await _call(ws, 'torrent_action', {'hash': h, 'action': action}, 210 + i)
            assert r['result'] == {'hash': h, 'action': action}, r
        assert srv.state.torrents[h]['d.is_hash_checking'] == 1
        r = await _call(ws, 'set_priority', {'hash': h, 'priority': 3}, 204)
        assert srv.state.torrents[h]['d.priority'] == 3
        # no per-torrent action may carry the untrusted header: start/stop/
        # announce are not marked safe, and the ones rtorrent does mark safe
        # (pause/resume/open/close/check_hash) break midway under it because
        # their implementations run unsafe commands - the fake faults on them
        for command in ('d.start', 'd.stop', 'd.pause', 'd.resume', 'd.open',
                        'd.close', 'd.check_hash', 'd.tracker_announce'):
            assert not _was_untrusted(srv, command), command
        # rejections
        assert (await _call(ws, 'torrent_action', {'hash': 'zz', 'action': 'stop'}, 205))['error']['code'] == -32602
        assert (await _call(ws, 'torrent_action', {'hash': h, 'action': 'bogus'}, 206))['error']['code'] == -32602
        assert (await _call(ws, 'set_priority', {'hash': h, 'priority': 9}, 207))['error']['code'] == -32602
        await ws.close()
    asyncio.run(run())
    srv.state.torrents.pop(h, None)


def test_m2_file_priority_and_tracker(srv):
    async def run():
        ws, _ = await _connect(srv)
        r = await _call(ws, 'set_file_priority', {'hash': HASH_A, 'file_index': 0, 'priority': 0}, 210)
        assert r['result']['priority'] == 0
        assert srv.state.files[HASH_A][0]['f.priority'] == 0
        assert _was_untrusted(srv, 'f.priority.set')
        await _call(ws, 'set_file_priority', {'hash': HASH_A, 'file_index': 0, 'priority': 1}, 211)
        r = await _call(ws, 'set_tracker_enabled', {'hash': HASH_A, 'tracker_index': 0, 'enabled': 0}, 212)
        assert r['result']['enabled'] == 0
        assert srv.state.trackers[HASH_A][0]['t.is_enabled'] == 0
        assert _was_untrusted(srv, 't.is_enabled.set')
        await _call(ws, 'set_tracker_enabled', {'hash': HASH_A, 'tracker_index': 0, 'enabled': 1}, 213)
        # rejections
        assert (await _call(ws, 'set_file_priority', {'hash': HASH_A, 'file_index': 0, 'priority': 3}, 214))['error']['code'] == -32602
        assert (await _call(ws, 'set_file_priority', {'hash': HASH_A, 'file_index': -1, 'priority': 1}, 215))['error']['code'] == -32602
        await ws.close()
    asyncio.run(run())


def test_m2_add_torrent(srv):
    async def run():
        ws, _ = await _connect(srv)
        magnet = 'magnet:?xt=urn:btih:%s&dn=AddedDistro.iso' % ('C' * 40)
        r = await _call(ws, 'add_torrent', {'magnet': magnet, 'start': True, 'label': 'iso'}, 220)
        assert r['result'] == {'added': True}
        added = [h for h, t in srv.state.torrents.items() if t['d.name'] == 'AddedDistro.iso']
        assert len(added) == 1
        assert srv.state.torrents[added[0]]['d.custom1'] == 'iso'
        # rejections: neither magnet nor content, and a bad magnet
        assert (await _call(ws, 'add_torrent', {'start': True}, 221))['error']['code'] == -32602
        assert (await _call(ws, 'add_torrent', {'magnet': 'http://x', 'start': True}, 222))['error']['code'] == -32602
        srv.state.torrents.pop(added[0], None)
        await ws.close()
    asyncio.run(run())


def test_m3_tuning_and_label(srv):
    async def run():
        ws, _ = await _connect(srv)
        assert (await _call(ws, 'set_torrent_limit', {'hash': HASH_A, 'key': 'uploads_max', 'value': 5}, 230))['result']['value'] == 5
        assert srv.state.torrents[HASH_A]['d.uploads_max'] == 5
        assert (await _call(ws, 'set_label', {'hash': HASH_A, 'label': 'linux-isos'}, 231))['result']['label'] == 'linux-isos'
        assert srv.state.torrents[HASH_A]['d.custom1'] == 'linux-isos'
        # throttle name requires an existing group
        assert (await _call(ws, 'set_throttle_name', {'hash': HASH_A, 'name': 'nope'}, 232))['error']['code'] == -32602
        await _call(ws, 'throttle_group', {'name': 'night', 'up_kb': 500, 'down_kb': 0}, 233)
        assert (await _call(ws, 'set_throttle_name', {'hash': HASH_A, 'name': 'night'}, 234))['result']['name'] == 'night'
        assert srv.state.torrents[HASH_A]['d.throttle_name'] == 'night'
        # detach
        await _call(ws, 'set_throttle_name', {'hash': HASH_A, 'name': ''}, 235)
        # restore
        await _call(ws, 'set_torrent_limit', {'hash': HASH_A, 'key': 'uploads_max', 'value': 0}, 236)
        await _call(ws, 'set_label', {'hash': HASH_A, 'label': ''}, 237)
        await ws.close()
    asyncio.run(run())


def test_m3_peer_actions(srv):
    peer = {'p.id': 'D' * 40, 'p.address': '192.0.2.5', 'p.up_rate': 0, 'p.down_rate': 0,
            'p.peer_rate': 0, 'p.is_preferred': 0, 'p.is_encrypted': 1, 'p.is_incoming': 0,
            'p.completed_percent': 42, 'p.client_version': 'rakshasa 0.16'}
    srv.state.peers[HASH_A].append(peer)
    async def run():
        ws, _ = await _connect(srv)
        assert (await _call(ws, 'peer_action', {'hash': HASH_A, 'peer': 'D' * 40, 'peer_action': 'ban'}, 240))['result']['peer_action'] == 'ban'
        assert srv.state.peers[HASH_A][0]['p.banned'] == 1
        assert _was_untrusted(srv, 'p.banned.set')
        assert (await _call(ws, 'peer_action', {'hash': HASH_A, 'peer': 'D' * 40, 'peer_action': 'disconnect'}, 241))
        assert srv.state.peers[HASH_A] == []
        # rejections
        assert (await _call(ws, 'peer_action', {'hash': HASH_A, 'peer': 'short', 'peer_action': 'ban'}, 242))['error']['code'] == -32602
        await ws.close()
    asyncio.run(run())


def test_m3_global_peer_limits(srv):
    async def run():
        ws, _ = await _connect(srv)
        r = await _call(ws, 'set_global', {'key': 'throttle_max_peers_normal', 'value': 300}, 250)
        assert r['result']['value'] == 300
        assert srv.state.globals['throttle.max_peers.normal'] == 300
        assert _was_untrusted(srv, 'throttle.max_peers.normal.set')
        srv.rpc().call('throttle.max_peers.normal.set', [('string', ''), ('i8', 200)])
        await ws.close()
    asyncio.run(run())


def test_m6_throttle_groups(srv):
    async def run():
        ws, _ = await _connect(srv)
        r = await _call(ws, 'throttle_group', {'name': 'day', 'up_kb': 1000, 'down_kb': 2000}, 260)
        assert r['result'] == {'name': 'day', 'up_kb': 1000, 'down_kb': 2000}
        assert srv.state.throttle_groups['day'] == {'up': 1000 * 1024, 'down': 2000 * 1024}
        # register surfaces the group
        ws2, resp = await _connect(srv, id=261)
        names = [g['name'] for g in resp['result'].get('throttle_groups', [])]
        assert 'day' in names
        await ws2.close()
        assert (await _call(ws, 'throttle_group', {'name': 'bad name!', 'up_kb': 1}, 262))['error']['code'] == -32602
        await ws.close()
    asyncio.run(run())


def test_m6_schedule(srv):
    async def run():
        ws, _ = await _connect(srv)
        r = await _call(ws, 'set_schedule', {'up_day': 4096, 'down_day': 10240, 'up_night': 0,
                                             'down_night': 0, 'day_hhmm': '09:00', 'night_hhmm': '23:30'}, 270)
        assert r['result']['up_day'] == 4096
        assert 'rtr_day_up' in srv.state.schedules
        assert 'rtr_night_down' in srv.state.schedules
        assert (await _call(ws, 'set_schedule', {'up_day': 1, 'down_day': 1, 'up_night': 1,
                                                 'down_night': 1, 'day_hhmm': '9am', 'night_hhmm': '23:30'}, 271))['error']['code'] == -32602
        await _call(ws, 'clear_schedule', {}, 272)
        assert srv.state.schedules == {}
        await ws.close()
    asyncio.run(run())


def test_m7_add_view(srv):
    async def run():
        ws, _ = await _connect(srv)
        r = await _call(ws, 'add_view', {'name': 'linuxonly', 'filter': 'seeding'}, 280)
        assert r['result'] == {'name': 'linuxonly', 'filter': 'seeding'}
        assert 'linuxonly' in srv.state.views
        # the new view is now registrable and reported in the register snapshot
        ws2, resp = await _connect(srv, view='linuxonly', id=281)
        assert 'linuxonly' in [v['name'] for v in resp['result'].get('views', [])]
        await ws2.close()
        assert (await _call(ws, 'add_view', {'name': 'main', 'filter': 'all'}, 282))['error']['code'] == -32602
        assert (await _call(ws, 'add_view', {'name': 'x', 'filter': 'evil'}, 283))['error']['code'] == -32602
        await ws.close()
    asyncio.run(run())


def test_m5_erase_with_data(srv_data):
    h = 'E' * 40
    tdir = os.path.join(srv_data.data_root, 'erase-me')
    os.makedirs(tdir, exist_ok=True)
    with open(os.path.join(tdir, 'payload.bin'), 'wb') as f:
        f.write(b'x' * 16)
    srv_data.state.add_torrent(h, 'erase-me', 16, directory=tdir,
                               files=[{'f.size_chunks': 1, 'f.completed_chunks': 1, 'f.priority': 1,
                                       'f.size_bytes': 16, 'f.path': 'payload.bin'}])
    async def run():
        ws, _ = await _connect(srv_data)
        r = await _call(ws, 'erase_torrent', {'hash': h, 'with_data': True}, 290)
        assert r['result'] == {'hash': h, 'with_data': True}
        await ws.close()
    asyncio.run(run())
    assert not os.path.exists(os.path.join(tdir, 'payload.bin'))
    assert h not in srv_data.state.torrents
    # d.erase is marked safe but runs close() and the event.download.erased
    # hooks (d.delete_tied etc.), which fail under the header
    assert not _was_untrusted(srv_data, 'd.erase')


def test_m7_move_data(srv_data):
    h = 'F' * 40
    src = os.path.join(srv_data.data_root, 'show')
    os.makedirs(src, exist_ok=True)
    with open(os.path.join(src, 'ep1.mkv'), 'wb') as f:
        f.write(b'y' * 16)
    dst_parent = os.path.join(srv_data.data_root, 'archive')
    srv_data.state.add_torrent(h, 'show', 16, directory=src,
                               files=[{'f.size_chunks': 1, 'f.completed_chunks': 1, 'f.priority': 1,
                                       'f.size_bytes': 16, 'f.path': 'ep1.mkv'}])
    async def run():
        ws, _ = await _connect(srv_data)
        r = await _call(ws, 'move_data', {'hash': h, 'directory': dst_parent}, 300)
        assert r['result']['directory'] == os.path.join(dst_parent, 'show')
        await ws.close()
    asyncio.run(run())
    assert os.path.exists(os.path.join(dst_parent, 'show', 'ep1.mkv'))
    assert not os.path.exists(os.path.join(src, 'ep1.mkv'))
    assert srv_data.state.torrents[h]['d.directory'] == os.path.join(dst_parent, 'show')
    # the close/open around the move go out trusted for the same reason
    for command in ('d.close', 'd.open'):
        assert not _was_untrusted(srv_data, command), command


def test_move_data_disabled_without_root(srv):
    # the default server has no RTR_DATA_ROOT, so data ops are refused (-32603)
    h = 'A' * 40
    async def run():
        ws, _ = await _connect(srv)
        r = await _call(ws, 'move_data', {'hash': h, 'directory': '/tmp/whatever'}, 310)
        assert r['error']['code'] == -32603
        await ws.close()
    asyncio.run(run())


def test_m2_add_torrent_raw(srv):
    # a .torrent file arrives as base64 content_b64 (load.raw_start); the fake
    # extracts the bencoded name so the added torrent is recognisable
    import base64
    blob = b'd4:infod4:name12:raw-demo.iso6:lengthi42eee'
    content = base64.b64encode(blob).decode()
    async def run():
        ws, _ = await _connect(srv)
        r = await _call(ws, 'add_torrent', {'content_b64': content, 'start': True}, 400)
        assert r['result'] == {'added': True}
        added = [t for t in srv.state.torrents.values() if t['d.name'] == 'raw-demo.iso']
        assert len(added) == 1
        for h, t in list(srv.state.torrents.items()):
            if t['d.name'] == 'raw-demo.iso':
                srv.state.torrents.pop(h, None)
        # invalid base64 is rejected before any rtorrent call
        assert (await _call(ws, 'add_torrent', {'content_b64': 'not!base64', 'start': True}, 401))['error']['code'] == -32602
        await ws.close()
    asyncio.run(run())


def test_m7_custom_view_filters(srv):
    # A registered custom view must return exactly the torrents its filter selects
    # -- the same set as the equivalent built-in view -- not the whole list. This
    # covers the fake's view.filter handling and, end to end, that rtremote installs
    # the right condition and filters the registration snapshot by it.
    async def run():
        ws, _ = await _connect(srv)
        wm, m = await _connect(srv, view='main', id=430)
        main_hashes = sorted(t['hash'] for t in m['result']['torrents'])
        await wm.close()
        assert len(main_hashes) >= 2  # fixture seeds alpha (seeding) + bravo (stopped)
        saw_proper_subset = False
        # (filter preset, the built-in view that selects the same set). 'downloading'
        # is a preset but not a built-in view name; its set is the 'incomplete' view.
        cases = [('seeding', 'seeding'), ('stopped', 'stopped'), ('complete', 'complete'),
                 ('downloading', 'incomplete'), ('all', 'main')]
        for i, (preset, builtin) in enumerate(cases):
            wref, ref = await _connect(srv, view=builtin, id=440 + i)
            expected = sorted(t['hash'] for t in ref['result']['torrents'])
            await wref.close()
            name = 'cv_' + preset
            assert (await _call(ws, 'add_view', {'name': name, 'filter': preset}, 450 + i))['result']['name'] == name
            wcv, resp = await _connect(srv, view=name, id=460 + i)
            got = sorted(t['hash'] for t in resp['result']['torrents'])
            await wcv.close()
            assert got == expected, (preset, got, expected)  # custom view filters like the built-in one
            if 0 < len(got) < len(main_hashes):
                saw_proper_subset = True
        # at least one filter returned a non-empty proper subset: proves the view is
        # actually filtered, not passed through as the whole list (the bug this guards)
        assert saw_proper_subset
        await ws.close()
    asyncio.run(run())


def test_compression_negotiated_and_logged(srv):
    # the app (OkHttp) offers the bare permessage-deflate extension, without the
    # client_max_window_bits parameter browsers add; serve()'s default configuration
    # must still accept it, and the connection line must say so, so a stripped extension
    # (a reverse proxy in between) or an app build from before OkHttp shows up in the
    # log as "compression: none"
    from websockets.extensions.permessage_deflate import ClientPerMessageDeflateFactory

    async def run():
        bare = ClientPerMessageDeflateFactory(client_max_window_bits=None)
        ws = await websockets.connect(srv.uri, ssl=_ssl_ctx(), compression=None, extensions=[bare])
        assert [e.name for e in ws.protocol.extensions] == ['permessage-deflate']
        assert ws.response.headers.get('Sec-WebSocket-Extensions', '').startswith('permessage-deflate')
        await ws.send(_req('register', {'secret_key': SECRET}))
        response = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert 'result' in response
        await ws.close()

    asyncio.run(run())
    log = _read_log(srv)
    assert any('|INFO|app|' in l and 'connection from' in l and 'compression: permessage-deflate' in l
               for l in log.splitlines()), log
