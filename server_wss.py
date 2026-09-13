import asyncio
import hmac
import json
import os
import re
import signal
import ssl
import sys
from threading import RLock

from cachetools import TTLCache, cached
from websockets.asyncio.server import serve
from websockets.exceptions import WebSocketException

from diffs import map_diff, map_get_multi_diff
from model import Client
from plugins import DiskUsage
from remote import Remote
from rpc import RpcError
from utils import Logger, get_sha1, getenv_path

RTR_CERT_PATH = getenv_path('RTR_CERT_PATH', './cert/cert.pem')
RTR_RETR_INTERVAL = int(os.getenv('RTR_RETR_INTERVAL', 5))
RTR_SHORT_CACHE_TTL = int(os.getenv('RTR_SHORT_CACHE_TTL', 5))
RTR_LISTEN_HOST = os.getenv('RTR_LISTEN_HOST', '127.0.0.1')
RTR_LISTEN_PORT = int(os.getenv('RTR_LISTEN_PORT', 8765))
DEFAULT_SECRET_KEY_SHA1 = get_sha1('abc123')
RTR_SECRET_KEY_SHA1 = os.getenv('RTR_SECRET_KEY_SHA1', DEFAULT_SECRET_KEY_SHA1).strip().lower()
SOCK_PATH = getenv_path('RTR_SCGI_SOCKET_PATH', './.rtorrent.sock')
RTR_PID_PATH = getenv_path('RTR_PID_PATH', './wss_server.pid')
RTR_PLUGINS_DISK_USAGE_PATHS = os.getenv('RTR_PLUGINS_DISK_USAGE_PATHS', '/')
RTR_SEND_TIMEOUT = 10  # seconds to wait for a single client send before dropping it
logger = Logger.get_logger()
RTR_VERSION = '__RTR_VERSION_PLACEHOLDER__'
# wire-contract level for the Android app's feature gating; bump only when the
# protocol changes (new method, new field, changed shape) - never at release time.
# 2 = set_global + throttle.max_uploads/downloads.global in the global data
RTR_PROTOCOL_VERSION = 2
INFO_HASH_RE = re.compile('[0-9A-Fa-f]{40}')

JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


class Cached:
    VIEW_DEFAULT = 'main'
    VIEW_NAME = 'name'
    VIEWS = {VIEW_DEFAULT, VIEW_NAME, 'started', 'stopped', 'complete',
             'incomplete', 'hashing', 'seeding', 'leeching', 'active'}
    global_data = None
    global_data_lock = None
    torrents = None
    torrents_lock = None
    clients = set()
    clients_lock = None
    update_now = None  # set after a successful write to trigger an immediate updater tick
    SHORT_CACHES_NO = 4
    SHORT_CACHES = [TTLCache(maxsize=4096, ttl=RTR_SHORT_CACHE_TTL) for _ in range(SHORT_CACHES_NO)]
    SHORT_LOCKS = [RLock() for _ in range(SHORT_CACHES_NO)]
    plugins = [DiskUsage(RTR_PLUGINS_DISK_USAGE_PATHS)]

    @staticmethod
    def init_async():
        # asyncio primitives must be created inside the running loop: this module
        # is imported before daemonize() forks, and a loop created pre-fork does
        # not survive the fork on kqueue platforms (macOS/BSD)
        Cached.global_data_lock = asyncio.Lock()
        Cached.torrents_lock = asyncio.Lock()
        Cached.clients_lock = asyncio.Lock()
        Cached.update_now = asyncio.Event()

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
                diff = new_global.__dict__
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
                diff = {'new': [t.__dict__ for t in new_torrents]} if new_torrents else None
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
                logger.info('removed: %s' % client)

    @staticmethod
    async def filter_by_view(new_data, view_name):
        # register-time snapshot: for non-default views drop torrents outside the
        # view and apply the view's ordering
        if view_name != Cached.VIEW_DEFAULT and 'torrents' in new_data:
            hashes = await asyncio.to_thread(Cached.get_torrents_hashes, view_name)
            # 'name' view always has all torrents (i.e. same number as 'main'), just different order
            if view_name != Cached.VIEW_NAME:
                hashes_filter = {t.hash for t in hashes}
                new_data['torrents'] = [t for t in new_data['torrents'] if t['hash'] in hashes_filter]
            order = {t.hash: i for i, t in enumerate(hashes)}
            # a torrent may have appeared after the cached view lookup; sort it last
            new_data['torrents'].sort(key=lambda t: order.get(t['hash'], len(order)))
        return new_data

    @staticmethod
    async def get_view_payload(new_data, view_name, per_view_cache):
        # broadcast payload for one view; only 'new' entries are filtered: the
        # Android app adds every 'new' torrent to its current view but already
        # ignores 'changed'/'del' entries for hashes it does not display
        if view_name in per_view_cache:
            return per_view_cache[view_name]
        payload = new_data
        torrents = new_data.get('torrents')
        if view_name != Cached.VIEW_DEFAULT and isinstance(torrents, dict) and 'new' in torrents:
            try:
                hashes = await asyncio.to_thread(Cached.get_torrents_hashes, view_name)
                order = {t.hash: i for i, t in enumerate(hashes)}
                if view_name == Cached.VIEW_NAME:
                    filtered = sorted(torrents['new'], key=lambda t: order.get(t['hash'], len(order)))
                else:
                    filtered = [t for t in torrents['new'] if t['hash'] in order]
                torrents = dict(torrents)
                if filtered:
                    torrents['new'] = filtered
                else:
                    del torrents['new']
                payload = dict(new_data)
                if torrents:
                    payload['torrents'] = torrents
                else:
                    del payload['torrents']
                if not payload:
                    payload = None  # nothing left for this view
            except Exception as e:
                logger.error('view filtering failed for %s; sending unfiltered' % view_name, exc_info=e)
                payload = new_data
        per_view_cache[view_name] = payload
        return payload

    @staticmethod
    async def send_to_client(client, payload):
        try:
            response = prepare_response(get_json_response(client.req_id, payload))
            await asyncio.wait_for(client.websocket.send(response), RTR_SEND_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error('send to %s timed out; aborting its connection' % client)
            transport = getattr(client.websocket, 'transport', None)
            if transport:
                transport.abort()
        except WebSocketException as e:
            logger.info('send to %s failed: %s' % (client, e))
        except Exception as e:
            logger.error('send to %s failed' % client, exc_info=e)

    @staticmethod
    async def notify_clients(new_data):
        logger.info('notify_clients')
        logger.debug(new_data)
        async with Cached.clients_lock:
            clients = list(Cached.clients)
        if not clients:
            return
        per_view_cache = {}
        sends = []
        for client in clients:
            payload = await Cached.get_view_payload(new_data, client.view_name, per_view_cache)
            if payload is None:
                continue
            logger.info('sending to %s' % client)
            sends.append(Cached.send_to_client(client, payload))
        if sends:
            await asyncio.gather(*sends)

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


def get_json_error(request_id, code, message):
    return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}}


def prepare_response(response_json):
    response = json.dumps(response_json)
    return response


async def process_request(request, websocket):
    try:
        req = json.loads(request)
    except Exception as e:
        logger.debug(e)
        return None, False
    if not isinstance(req, dict) or req.get('jsonrpc') != '2.0' or 'method' not in req or 'id' not in req:
        logger.info('malformed request from %s' % str(websocket.remote_address))
        return None, False
    params = req.get('params')
    if await Cached.is_registered(websocket):
        if isinstance(params, dict) and 'hash' in params:
            response_json = await handle_method_with_hash(req['id'], req['method'], params['hash'])
            return prepare_response(response_json), True
        if isinstance(params, dict) and 'key' in params:
            response_json = await handle_set_global(req['id'], req['method'], params)
            return prepare_response(response_json), True
    response_json = await handle_register(req, websocket)
    if response_json is None:
        return None, False
    return prepare_response(response_json), True


async def handle_register(req, websocket):
    if req['method'] != 'register':
        return None
    params = req.get('params')
    if not isinstance(params, dict):
        return None
    secret_key = params.get('secret_key')
    if not isinstance(secret_key, str) or not hmac.compare_digest(RTR_SECRET_KEY_SHA1, get_sha1(secret_key)):
        logger.info('failed registration from %s' % str(websocket.remote_address))
        return None
    view = params.get('view')
    view_name = Cached.get_view_name(view) if isinstance(view, str) else Cached.VIEW_DEFAULT
    data = await Cached.get_global()
    torrents = await Cached.get_torrents()
    if data is None or torrents is None:
        logger.error('register before the initial rtorrent snapshot; dropping client')
        return None
    await Cached.add_client(websocket, req['id'], view_name)
    result = {'version': RTR_VERSION, 'rtremote_protocol_version': RTR_PROTOCOL_VERSION,
              'global': data.__dict__, 'torrents': [t.__dict__ for t in torrents]}
    plugins_data = {}
    for plugin in Cached.plugins:
        plugin_output = await plugin.get(False)
        if plugin_output is not None:
            plugins_data[plugin.name()] = plugin_output
    if plugins_data:
        result['plugins'] = plugins_data
    result = await Cached.filter_by_view(result, view_name)
    return get_json_response(req['id'], result)


async def handle_method_with_hash(req_id, method, hash):
    methods = {
        'get_files': (Cached.get_files, 'files'),
        'get_peers': (Cached.get_peers, 'peers'),
        'get_trackers': (Cached.get_trackers, 'trackers'),
    }
    if method not in methods:
        return get_json_error(req_id, JSONRPC_METHOD_NOT_FOUND, 'unknown method: %s' % method)
    if not isinstance(hash, str) or not INFO_HASH_RE.fullmatch(hash):
        # the hash goes into an XML-RPC call; only accept real info hashes
        return get_json_error(req_id, JSONRPC_INVALID_PARAMS, 'invalid hash')
    func, result_key = methods[method]
    try:
        data = await asyncio.to_thread(func, hash)
    except RpcError as e:
        logger.info('%s(%s) failed: %s' % (method, hash, e))
        return get_json_error(req_id, JSONRPC_INTERNAL_ERROR, str(e))
    except Exception as e:
        logger.error('%s(%s) failed' % (method, hash), exc_info=e)
        return get_json_error(req_id, JSONRPC_INTERNAL_ERROR, 'internal error')
    return get_json_response(req_id, {result_key: [x.__dict__ for x in data]})


async def handle_set_global(req_id, method, params):
    # the only write method (protocol level 2). Everything is validated against
    # the server-side allowlist before any rtorrent command is built; clients
    # never supply command text
    if method != 'set_global':
        return get_json_error(req_id, JSONRPC_METHOD_NOT_FOUND, 'unknown method: %s' % method)
    key = params.get('key')
    setter = Remote.GLOBAL_SETTERS.get(key) if isinstance(key, str) else None
    if setter is None:
        return get_json_error(req_id, JSONRPC_INVALID_PARAMS, 'key is not settable')
    value = params.get('value')
    _, min_value, max_value, _ = setter
    # bool is an int subclass in Python; a JSON true/false must not pass as 1/0
    if isinstance(value, bool) or not isinstance(value, int) or not min_value <= value <= max_value:
        return get_json_error(req_id, JSONRPC_INVALID_PARAMS, 'invalid value')
    try:
        await asyncio.to_thread(Remote(SOCK_PATH).set_global, key, value)
    except RpcError as e:
        logger.info('set_global(%s=%s) failed: %s' % (key, value, e))
        return get_json_error(req_id, JSONRPC_INTERNAL_ERROR, str(e))
    except Exception as e:
        logger.error('set_global(%s=%s) failed' % (key, value), exc_info=e)
        return get_json_error(req_id, JSONRPC_INTERNAL_ERROR, 'internal error')
    logger.info('set_global: %s=%s' % (key, value))
    # push the change to every client as a normal global diff right away
    Cached.update_now.set()
    return get_json_response(req_id, {'key': key, 'value': value})


async def global_data_updater(ready):
    remote = Remote(SOCK_PATH)
    while True:
        # cleared before polling: a write landing mid-poll re-triggers a fresh
        # tick instead of being lost until the next interval
        Cached.update_now.clear()
        try:
            new_data = {}
            data = await asyncio.to_thread(remote.get_global)
            diff = await Cached.update_global(data)
            if diff:
                new_data['global'] = diff
            data = await asyncio.to_thread(remote.get_torrents)
            diff = await Cached.update_torrents(data)
            if diff:
                new_data['torrents'] = diff
            plugins_data = {}
            for plugin in Cached.plugins:
                plugin_output = await plugin.get()
                if plugin_output is not None:
                    plugins_data[plugin.name()] = plugin_output
            if plugins_data:
                new_data['plugins'] = plugins_data
            if not ready.is_set():
                ready.set()
                g = await Cached.get_global()
                logger.info('initial rtorrent snapshot fetched (rtorrent %s, api %s)'
                            % (getattr(g, 'system_client_version', '?'), getattr(g, 'system_api_version', '?')))
            elif len(new_data) > 0:
                await Cached.notify_clients(new_data)
        except Exception as e:
            # a failed poll (e.g. rtorrent restarting) must not kill the server
            logger.error('updater tick failed, retrying in %ds: %s' % (RTR_RETR_INTERVAL, e), exc_info=e)
        try:
            # interval sleep that a successful write cuts short (see handle_set_global)
            await asyncio.wait_for(Cached.update_now.wait(), RTR_RETR_INTERVAL)
        except asyncio.TimeoutError:
            pass


async def short_caches_cleaner():
    while True:
        Cached.clear_short_caches()
        await asyncio.sleep(RTR_SHORT_CACHE_TTL)


async def on_message(websocket):
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


def handle_signal(stop_event, signum):
    logger.info('signal received: %d' % signum)
    stop_event.set()


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
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()
    if os.fork() > 0:  # second fork: never reacquire a controlling terminal
        os._exit(0)
    os.chdir('/')
    os.umask(0o022)
    # redirect - not close - stdio, so stray writes cannot hit an fd reused by a socket
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    if devnull > 2:
        os.close(devnull)


async def amain():
    Cached.init_async()
    if RTR_SECRET_KEY_SHA1 == DEFAULT_SECRET_KEY_SHA1:
        logger.error('SECURITY WARNING: RTR_SECRET_KEY_SHA1 is not set; '
                     'the server accepts the publicly known default secret')
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
    ssl_context.load_cert_chain(RTR_CERT_PATH)
    stop = asyncio.Event()
    ready = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGUSR1, signal.SIGTERM):
        loop.add_signal_handler(s, handle_signal, stop, s)
    updater = loop.create_task(global_data_updater(ready))
    cleaner = loop.create_task(short_caches_cleaner())
    logger.debug('data updater and short caches cleaner scheduled')
    # only accept clients once the first rtorrent snapshot exists (or we are stopped)
    ready_task = loop.create_task(ready.wait())
    stop_task = loop.create_task(stop.wait())
    try:
        await asyncio.wait({ready_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if not stop.is_set():
            async with serve(on_message, RTR_LISTEN_HOST, RTR_LISTEN_PORT, ssl=ssl_context):
                logger.info('listening on %s:%d' % (RTR_LISTEN_HOST, RTR_LISTEN_PORT))
                await stop.wait()
    finally:
        for task in (updater, cleaner, ready_task, stop_task):
            task.cancel()
        await asyncio.gather(updater, cleaner, ready_task, stop_task, return_exceptions=True)


def main():
    if '-f' not in sys.argv and '--foreground' not in sys.argv:
        daemonize()
    create_pid()
    try:
        asyncio.run(amain())
    except OSError as e:
        logger.error(e.filename, exc_info=e)
    except Exception as e:
        logger.error(e, exc_info=e)
    finally:
        delete_pid()


if __name__ == '__main__':
    main()
