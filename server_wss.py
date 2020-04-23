import asyncio
import json
# import logging
import os
import signal
import ssl
import sys
from threading import RLock

import websockets
from cachetools import TTLCache, cached
from websockets import WebSocketException

from diffs import map_diff, map_get_multi_diff
from model import Client
from plugins import DiskUsage
from remote import Remote
from utils import Logger, get_sha1, getenv_path

# logging.getLogger("asyncio").setLevel(logging.INFO)
RTR_CERT_PATH = getenv_path('RTR_CERT_PATH', './cert/cert.pem')
RTR_RETR_INTERVAL = int(os.getenv('RTR_RETR_INTERVAL', 5))
RTR_SHORT_CACHE_TTL = int(os.getenv('RTR_SHORT_CACHE_TTL', 5))
RTR_LISTEN_HOST = os.getenv('RTR_LISTEN_HOST', '127.0.0.1')
RTR_LISTEN_PORT = int(os.getenv('RTR_LISTEN_PORT', 8765))
RTR_SECRET_KEY_SHA1 = getenv_path('RTR_SECRET_KEY_SHA1', get_sha1('abc123'))
SOCK_PATH = getenv_path('RTR_SCGI_SOCKET_PATH', './.rtorrent.sock')
RTR_PID_PATH = getenv_path('RTR_PID_PATH', './wss_server.pid')
RTR_PLUGINS_DISK_USAGE_PATHS = os.getenv('RTR_PLUGINS_DISK_USAGE_PATHS', '/')
logger = Logger.get_logger()
RTR_VERSION = '__RTR_VERSION_PLACEHOLDER__'


class Cached:
    VIEW_DEFAULT = 'main'
    VIEW_NAME = 'name'
    VIEWS = {VIEW_DEFAULT, VIEW_NAME, 'started', 'stopped', 'complete',
             'incomplete', 'hashing', 'seeding', 'leeching', 'active'}
    global_data = None
    global_data_lock = asyncio.Lock()
    torrents = None
    torrents_lock = asyncio.Lock()
    clients = set()
    clients_lock = asyncio.Lock()
    SHORT_CACHES_NO = 4
    SHORT_CACHES = [TTLCache(maxsize=4096, ttl=RTR_SHORT_CACHE_TTL) for _ in range(SHORT_CACHES_NO)]
    SHORT_LOCKS = [RLock() for _ in range(SHORT_CACHES_NO)]
    plugins = [DiskUsage(RTR_PLUGINS_DISK_USAGE_PATHS)]

    @staticmethod
    async def update_global(new_global):
        diff = None
        async with Cached.global_data_lock:
            if Cached.global_data:
                d = Cached.get_global_diff(Cached.global_data, new_global)
                if len(d.keys()) > 0:
                    Cached.global_data = new_global
                    diff = d
            else:
                Cached.global_data = new_global
                diff = new_global
        return diff

    @staticmethod
    async def get_global():
        async with Cached.global_data_lock:
            return Cached.global_data

    @staticmethod
    def get_global_diff(old, new):
        return map_diff(old.__dict__, new.__dict__)

    @staticmethod
    async def update_torrents(new_torrents):
        diff = None
        async with Cached.torrents_lock:
            if Cached.torrents:
                d = Cached.get_torrents_diff(Cached.torrents, new_torrents)
                if d:
                    Cached.torrents = new_torrents
                    diff = d
            else:
                Cached.torrents = new_torrents
                diff = new_torrents
        return diff

    @staticmethod
    async def get_torrents():
        async with Cached.torrents_lock:
            return Cached.torrents

    @staticmethod
    def get_torrents_diff(old, new):
        diff = map_get_multi_diff(old, new)
        if (('changed' in diff and len(diff['changed']) > 0) or ('new' in diff and len(diff['new']) > 0)
                or ('del' in diff and len(diff['del']) > 0)):
            return diff
        return None

    @staticmethod
    def get_view_name(requested_view_name):
        requested_view_name = requested_view_name.lower()
        return requested_view_name if requested_view_name in Cached.VIEWS else Cached.VIEW_DEFAULT

    @staticmethod
    async def add_client(websocket, req_id, view_name):
        async with Cached.clients_lock:
            client = Client(websocket, req_id, view_name)
            Cached.clients.discard(client)
            Cached.clients.add(client)
            logger.info('added: %s' % client)

    @staticmethod
    async def remove_client(websocket):
        async with Cached.clients_lock:
            remove = {client for client in Cached.clients if client.websocket == websocket}
            for client in remove:
                Cached.clients.discard(client)
                logger.info('removed: %s:%d' % client)

    @staticmethod
    def filter_by_view(new_data, view_name):
        if view_name != Cached.VIEW_DEFAULT and 'torrents' in new_data:
            hashes = Cached.get_torrents_hashes(view_name)
            # 'name' view always has all torrents (i.e. same number as 'main'), just different order
            if view_name != Cached.VIEW_NAME:
                hashes_filter = {t.hash for t in hashes}
                new_data['torrents'] = [t for t in new_data['torrents'] if t['hash'] in hashes_filter]
            order = {t.hash: i for i, t in enumerate(hashes)}
            new_data['torrents'].sort(key=lambda t: order[t['hash']])
        return new_data

    @staticmethod
    async def notify_clients(new_data):
        logger.info('notify_clients')
        logger.debug(new_data)
        async with Cached.clients_lock:
            for client in Cached.clients:
                logger.info('sending to %s' % (client))
                try:
                    # TODO: add _prev_hash indicator for views with sorting, for 'new'
                    # OPTIMIZE: do not send 'changed' and 'del' for hashes not present client's view
                    await client.websocket.send(prepare_response(get_json_response(client.req_id, new_data)))
                except WebSocketException as e:
                    logger.error(e)

    @staticmethod
    async def is_registered(websocket):
        async with Cached.clients_lock:
            for client in Cached.clients:
                if client.websocket == websocket:
                    return True
        return False

    @staticmethod
    @cached(cache=SHORT_CACHES[0], lock=SHORT_LOCKS[0])
    def get_files(hash):
        return Remote(SOCK_PATH).get_files(hash)

    @staticmethod
    @cached(cache=SHORT_CACHES[1], lock=SHORT_LOCKS[1])
    def get_peers(hash):
        return Remote(SOCK_PATH).get_peers(hash)

    @staticmethod
    @cached(cache=SHORT_CACHES[2], lock=SHORT_LOCKS[2])
    def get_trackers(hash):
        return Remote(SOCK_PATH).get_trackers(hash)

    @staticmethod
    @cached(cache=SHORT_CACHES[3], lock=SHORT_LOCKS[3])
    def get_torrents_hashes(view):
        return Remote(SOCK_PATH).get_torrents_hashes(view)

    @staticmethod
    def clear_short_caches():
        for i in range(Cached.SHORT_CACHES_NO):
            with Cached.SHORT_LOCKS[i]:
                Cached.SHORT_CACHES[i].clear()


def get_json_response(request_id, result):
    json_obj = {}
    json_obj['jsonrpc'] = '2.0'
    json_obj['id'] = request_id
    json_obj['result'] = result
    return json_obj


def prepare_response(response_json):
    response = json.dumps(response_json)
    return response


async def process_request(request, websocket):
    try:
        req = json.loads(request)
    except Exception as e:
        logger.debug(e)
        return None, False
    if 'jsonrpc' in req and req['jsonrpc'] == '2.0' and 'method' in req and 'id' in req:
        if await Cached.is_registered(websocket):
            if 'params' in req and 'hash' in req['params']:
                response_json = handle_method_with_hash(req['id'], req['method'], req['params']['hash'])
            else:
                response_json = await handle_register(req, websocket)
                if response_json is None:
                    return None, False
        else:
            response_json = await handle_register(req, websocket)
            if response_json is None:
                return None, False
    return prepare_response(response_json), True


async def handle_register(req, websocket):
    if 'register' == req['method']:
        if 'params' in req and 'secret_key' in req['params'] and RTR_SECRET_KEY_SHA1 == get_sha1(req['params']['secret_key']):
            if 'view' in req['params']:
                view_name = Cached.get_view_name(req['params']['view'])
            else:
                view_name = Cached.VIEW_DEFAULT
            await Cached.add_client(websocket, req['id'], view_name)
            data = await Cached.get_global()
            torrents = await Cached.get_torrents()
            result = {'version': RTR_VERSION, 'global': data.__dict__, 'torrents': [t.__dict__ for t in torrents]}
            plugins_data = {}
            for plugin in Cached.plugins:
                plugin_output = await plugin.get(False)
                if plugin_output is not None:
                    plugins_data[plugin.name()] = plugin_output
            if plugins_data:
                result['plugins'] = plugins_data
            result = Cached.filter_by_view(result, view_name)
            response_json = get_json_response(req['id'], result)
            return response_json


def handle_method_with_hash(req_id, method, hash):
    methods = {
        Cached.get_files: 'files',
        Cached.get_peers: 'peers',
        Cached.get_trackers: 'trackers'
    }
    for m, r in methods.items():
        if m.__name__ == method:
            data = m(hash)
            result = {r: [x.__dict__ for x in data]}
            response_json = get_json_response(req_id, result)
            break
    return response_json


async def global_data_updater():
    remote = Remote(SOCK_PATH)
    while True:
        try:
            new_data = {}
            data = remote.get_global()
            diff = await Cached.update_global(data)
            if diff:
                new_data['global'] = diff
            data = remote.get_torrents()
            diff = await Cached.update_torrents(data)
            if diff:
                new_data['torrents'] = diff
            plugins_data = {}
            for plugin in Cached.plugins:
                plugin_output = await plugin.get(False)
                if plugin_output is not None:
                    plugins_data[plugin.name()] = plugin_output
            if plugins_data:
                new_data['plugins'] = plugins_data
            if len(new_data) > 0:
                await Cached.notify_clients(new_data)
        except Exception as e:
            logger.error(e, exc_info=e)
            asyncio.get_running_loop().stop()
            return
        await asyncio.sleep(RTR_RETR_INTERVAL)


async def short_caches_cleaner():
    while True:
        Cached.clear_short_caches()
        await asyncio.sleep(RTR_SHORT_CACHE_TTL)


async def on_message(websocket, path):
    try:
        logger.info('on_message from: %s' % str(websocket.remote_address))
        async for message in websocket:
            response, keep = await process_request(message, websocket)
            if not keep:
                logger.info('dropping: %s' % str(websocket.remote_address))
                break
            if response:
                await websocket.send(response)
    except WebSocketException as e:
        logger.info(e)
    except Exception as e:
        logger.error(e, exc_info=e)
    finally:
        await Cached.remove_client(websocket)


async def handle_signal2(loop, signum):
    logger.info('signal received: %d' % signum)
    loop.stop()


def create_pid():
    try:
        with open(RTR_PID_PATH, 'w') as f:
            f.write(str(os.getpid()))
            f.flush()
    except Exception as e:
        logger.error('cannot create PID file: ' + RTR_PID_PATH)
        logger.debug(e)


def delete_pid():
    try:
        os.remove(RTR_PID_PATH)
    except Exception as e:
        logger.error('removing PID file failed: ' + RTR_PID_PATH)
        logger.debug(e)


def daemonize():
    pid = os.fork()
    if pid > 0:
        sys.exit(0)
    elif pid < 0:
        logger.error('fork failed: %d' % pid)
        sys.exit(1)
    os.chdir('/')
    os.setsid()
    os.umask(0)
    sys.stdin.close()
    sys.stdout.close()
    sys.stderr.close()


def main():
    daemonize()
    create_pid()
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
    loop = asyncio.get_event_loop()
    for s in [signal.SIGINT, signal.SIGUSR1, signal.SIGTERM]:
        loop.add_signal_handler(s, lambda s=s: loop.create_task(handle_signal2(loop, s)))
    try:
        ssl_context.load_cert_chain(RTR_CERT_PATH)
        app_server = websockets.serve(on_message, RTR_LISTEN_HOST, RTR_LISTEN_PORT, ssl=ssl_context)
        asyncio.ensure_future(app_server, loop=loop)
        logger.debug('WebSocket server scheduled')
        loop.create_task(global_data_updater())
        logger.debug('data updater scheduled')
        loop.create_task(short_caches_cleaner())
        logger.debug('short caches cleaner scheduled')
        loop.run_forever()
    except OSError as e:
        logger.error(e.filename, exc_info=e)
    except Exception as e:
        logger.error(e)
    finally:
        delete_pid()


if __name__ == '__main__':
    main()
