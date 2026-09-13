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


def _spawn_server(interval):
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
        assert result['rtremote_protocol_version'] == 2
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
