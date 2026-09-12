import asyncio
import json
import os
import ssl

import websockets

from rpc import RTorrentRpc

from .utils import (get_json_request, get_rpc_param0_value,
                    wait_for_server_spawn)

RTR_WSS_SERVER_URI = os.getenv('RTR_WSS_SERVER_URI', 'wss://127.0.0.1:8765')
RTR_RETR_INTERVAL = int(os.getenv('RTR_RETR_INTERVAL', 5))


def get_ssl_context():
    # tests talk to a local server with a self-signed certificate
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    return ssl_context


async def validate_response(validate_func, method, params=None):
    async with websockets.connect(RTR_WSS_SERVER_URI, ssl=get_ssl_context()) as websocket:
        await asyncio.wait_for(websocket.send(get_json_request('register', {'secret_key': 'abc123'})), 1)
        await asyncio.wait_for(websocket.recv(), 1)
        await asyncio.wait_for(websocket.send(get_json_request(method, params)), 1)
        response = await asyncio.wait_for(websocket.recv(), 1)
        data = json.loads(response)
        validate_func(data)


async def recv_until_valid(websocket, validate_func, timeout):
    # pushes for unrelated concurrent changes may arrive first; keep reading
    # until the expected frame shows up or the deadline passes
    deadline = asyncio.get_running_loop().time() + timeout
    last_error = None
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise last_error if last_error else AssertionError('expected update not received in time')
        try:
            response = await asyncio.wait_for(websocket.recv(), remaining)
        except asyncio.TimeoutError:
            raise last_error if last_error else AssertionError('expected update not received in time')
        data = json.loads(response)
        try:
            validate_func(data)
            return data
        except (AssertionError, KeyError, IndexError, TypeError) as e:
            last_error = e


async def validate_update(validate_func, variable, value, rpc_ret_value=None, rpc_param0=None):
    async with websockets.connect(RTR_WSS_SERVER_URI, ssl=get_ssl_context()) as websocket:
        await asyncio.wait_for(websocket.send(get_json_request('register', {'secret_key': 'abc123'})), 1)
        await asyncio.wait_for(websocket.recv(), 1)
        rpc = RTorrentRpc(os.getenv('RTR_SCGI_SOCKET_PATH', './.rtorrent.sock'))
        resp = rpc.call(variable, None if rpc_param0 is None else RTorrentRpc.extract_params([rpc_param0]))
        orig_value = get_rpc_param0_value(resp)
        changed_value = value + 1 if orig_value == value else value
        param0_eval = '' if rpc_param0 is None else rpc_param0
        resp = rpc.call(variable + '.set', RTorrentRpc.extract_params([param0_eval, str(changed_value)]))
        assert get_rpc_param0_value(resp) == (changed_value if rpc_ret_value is None else rpc_ret_value)
        try:
            await recv_until_valid(websocket, lambda d: validate_func(d, variable, changed_value),
                                   RTR_RETR_INTERVAL + 2)
        finally:
            rpc.call(variable + '.set', RTorrentRpc.extract_params([param0_eval, str(orig_value)]))


async def validate_update_plugins_disk_usage(validate_func):
    async with websockets.connect(RTR_WSS_SERVER_URI, ssl=get_ssl_context()) as websocket:
        await asyncio.wait_for(websocket.send(get_json_request('register', {'secret_key': 'abc123'})), 1)
        await asyncio.wait_for(websocket.recv(), 1)
        files = ['./rtr_test_disk_usage_file', '/tmp/rtr_test_disk_usage_file']
        for file in files:
            try:
                with open(file, 'w') as f:
                    f.write(''.join(['A' for _ in range(1025)]))
            except Exception:
                pass
        try:
            await recv_until_valid(websocket, validate_func, RTR_RETR_INTERVAL + 2)
        finally:
            for file in files:
                try:
                    os.remove(file)
                except Exception:
                    pass


def validate_global(data):
    assert data['result']['global']['network_max_open_files'] > 0
    assert data['result']['global']['throttle_global_down_max_rate'] == 1024
    assert data['result']['global']['throttle_global_up_max_rate'] == 1024


def validate_torrents(data):
    hashes = {'67DD1659106DCDDE0FEC4283D7B0C84B6C292675', 'A6B69431743F085D96692A81C6282617C50243C4'}
    new_hashes = {t['hash'] for t in data['result']['torrents']}
    assert len(hashes & new_hashes) == len(hashes)
    for t in data['result']['torrents']:
        if t['hash'] == 'A6B69431743F085D96692A81C6282617C50243C4':
            assert t['name'] == 'debian-10.2.0-amd64-DVD-1.iso'
            assert t['size_bytes'] == 3918200832


def validate_torrents_name(data):
    hashes = ['67DD1659106DCDDE0FEC4283D7B0C84B6C292675', 'A6B69431743F085D96692A81C6282617C50243C4']
    new_hashes = {t['hash'] for t in data['result']['torrents']}
    assert len(set(hashes) & new_hashes) == len(hashes)
    for i in range(len(hashes)):
        assert data['result']['torrents'][i]['hash'] == hashes[i]


def validate_files(data):
    assert data['result']['files'][0]['path'] == 'debian-10.2.0-amd64-DVD-1.iso'
    assert data['result']['files'][0]['size_bytes'] == 3918200832


def validate_peers(data):
    assert len(data['result']['peers']) == 0


def validate_trackers(data):
    trackers = {'udp://tracker.opentrackr.org', 'udp://tracker.coppersurfer.tk',
                'udp://tracker.leechers-paradise.org', 'udp://zer0day.ch', 'udp://explodie.org', 'http://linuxtracker.org'}
    # get just the protocol+host part of each tracker['url']
    new_trackers = {t['url'][:t['url'].rfind(':')] for t in data['result']['trackers']}
    assert len(trackers & new_trackers) == len(trackers)


def validate_update_global(data, variable, changed_value):
    assert data['result']['global'][variable.replace('.', '_')] == changed_value


def validate_update_torrents(data, variable, changed_value):
    assert data['result']['torrents']['changed'][0]['hash'] == 'A6B69431743F085D96692A81C6282617C50243C4'
    assert data['result']['torrents']['changed'][0]['ignore_commands'] == changed_value


def validate_disk_usage(data):
    assert data['result']['plugins']['disk_usage']['total'] > 0
    assert data['result']['plugins']['disk_usage']['total'] >= data['result']['plugins']['disk_usage']['used']


def test_global():
    wait_for_server_spawn()
    asyncio.run(validate_response(validate_global, 'register', {'secret_key': 'abc123'}))


def test_torrents():
    wait_for_server_spawn()
    asyncio.run(validate_response(validate_torrents, 'register', {'secret_key': 'abc123'}))


def test_files():
    wait_for_server_spawn()
    asyncio.run(validate_response(
        validate_files, 'get_files', {'hash': 'A6B69431743F085D96692A81C6282617C50243C4'}))


def test_peers():
    wait_for_server_spawn()
    asyncio.run(validate_response(
        validate_peers, 'get_peers', {'hash': 'A6B69431743F085D96692A81C6282617C50243C4'}))


def test_trackers():
    wait_for_server_spawn()
    asyncio.run(validate_response(
        validate_trackers, 'get_trackers', {'hash': '67DD1659106DCDDE0FEC4283D7B0C84B6C292675'}))


def test_register_global():
    wait_for_server_spawn()
    asyncio.run(validate_update(validate_update_global, 'throttle.global_down.max_rate', 2048, 0))


def test_register_torrents():
    wait_for_server_spawn()
    asyncio.run(validate_update(validate_update_torrents,
                                'd.ignore_commands', 1, None, 'A6B69431743F085D96692A81C6282617C50243C4'))


def test_register_torrents_name():
    wait_for_server_spawn()
    asyncio.run(validate_response(
        validate_torrents_name, 'register', {'secret_key': 'abc123', 'view': 'name'}))


def test_register_torrents_stopped():
    wait_for_server_spawn()
    asyncio.run(validate_response(
        validate_torrents, 'register', {'secret_key': 'abc123', 'view': 'stopped'}))


def test_register_torrents_incomplete():
    wait_for_server_spawn()
    asyncio.run(validate_response(
        validate_torrents, 'register', {'secret_key': 'abc123', 'view': 'incomplete'}))


def test_register_torrents_empty_view():
    wait_for_server_spawn()

    def no_torrents(d):
        assert not d['result']['torrents']
    for view in ['started', 'complete', 'hashing', 'seeding', 'leeching', 'active']:
        asyncio.run(validate_response(
            no_torrents, 'register', {'secret_key': 'abc123', 'view': view}))


def test_disk_usage():
    wait_for_server_spawn()
    asyncio.run(validate_response(validate_disk_usage, 'register', {'secret_key': 'abc123'}))


def test_register_disk_usage():
    wait_for_server_spawn()
    asyncio.run(validate_update_plugins_disk_usage(validate_disk_usage))
