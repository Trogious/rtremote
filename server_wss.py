import asyncio
import base64
import binascii
import errno
import hmac
import json
import logging
import os
import platform
import re
import signal
import ssl
import sys
import time
import weakref
from threading import RLock

from cachetools import TTLCache, cached
from websockets.asyncio.server import serve
from websockets.exceptions import WebSocketException

from diffs import map_diff, map_get_multi_diff
from model import Client
from plugins import DiskUsage
from remote import MAGNET_RE, SAFE_GROUP_RE, SAFE_PATH_RE, URI_RE, Remote
from rpc import RpcError
from scgi import RTR_SCGI_TIMEOUT, ScgiError
from utils import WHERE_RTREMOTE, Diag, Logger, get_sha1, getenv_path

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
diag = Diag(logger)
RTR_VERSION = '__RTR_VERSION_PLACEHOLDER__'
# wire-contract level for the Android app's feature gating; bump only when the
# protocol changes (new method, new field, changed shape) - never at release time.
# 1=reads 2=set_global(M0) 3=per-torrent actions(M1) 4=details/add/file-prio(M2)
# 5=tuning/peers(M3) 6=erase-with-data/add-tracker/add-torrent opts(M5)
# 7=scheduled caps + throttle groups(M6) 8=custom views + move data(M7)
RTR_PROTOCOL_VERSION = 8
# root that erase-with-data and move-data are confined to (realpath-checked);
# empty disables both features (they return a JSON-RPC error)
RTR_DATA_ROOT = os.getenv('RTR_DATA_ROOT', '')
# fixed schedule entry names rtremote owns for the day/night cap pair
SCHEDULE_DAY_UP = 'rtr_day_up'
SCHEDULE_DAY_DOWN = 'rtr_day_down'
SCHEDULE_NIGHT_UP = 'rtr_night_up'
SCHEDULE_NIGHT_DOWN = 'rtr_night_down'
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
    # rtorrent cannot enumerate named throttle groups or custom views, so rtremote
    # remembers the ones it created (in-memory; reset on restart, like rtorrent's own)
    throttle_groups = {}  # name -> (up_kb, down_kb)
    custom_views = {}     # name -> filter preset
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
            diag.app('registered %s' % client, level=logging.INFO)

    @staticmethod
    async def remove_client(websocket):
        async with Cached.clients_lock:
            remove = {client for client in Cached.clients if client.websocket == websocket}
            for client in remove:
                Cached.clients.discard(client)
                diag.app('disconnected %s' % client, level=logging.INFO)

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
                log_failure('view filtering for %s (sending unfiltered)' % view_name, e)
                payload = new_data
        per_view_cache[view_name] = payload
        return payload

    @staticmethod
    async def send_to_client(client, payload):
        try:
            response = prepare_response(get_json_response(client.req_id, payload))
            await asyncio.wait_for(client.websocket.send(response), RTR_SEND_TIMEOUT)
        except asyncio.TimeoutError:
            diag.app('%s did not accept data for %ds; dropping it' % (client, RTR_SEND_TIMEOUT),
                     'the phone\'s connection stalled (mobile network, app frozen or backgrounded); '
                     'it will reconnect on its own', level=logging.WARNING)
            transport = getattr(client.websocket, 'transport', None)
            if transport:
                transport.abort()
        except WebSocketException as e:
            diag.app('send to %s failed: %s' % (client, e), 'the connection closed while sending; normal when '
                     'the app goes away', level=logging.INFO)
        except Exception as e:
            log_failure('send to %s' % client, e)

    @staticmethod
    async def notify_clients(new_data):
        logger.debug('notify_clients', extra={'where': WHERE_RTREMOTE})
        logger.debug(new_data, extra={'where': WHERE_RTREMOTE})
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
            logger.debug('sending to %s' % client, extra={'where': WHERE_RTREMOTE})
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


def describe_failure(e):
    # -> (where, message, hint, is_bug): one place that decides which component
    # a failure belongs to, so log lines and JSON-RPC error texts agree
    if isinstance(e, (ScgiError, RpcError)):
        return e.where, str(e), e.hint, False
    if isinstance(e, PermissionError):
        return WHERE_RTREMOTE, 'permission denied: %s' % e, \
            'the user running rtremote lacks rights on that path; check RTR_DATA_ROOT ownership', False
    if isinstance(e, FileNotFoundError):
        return WHERE_RTREMOTE, 'path not found: %s' % e, \
            'rtorrent reports files that are not where rtremote looks; is RTR_DATA_ROOT the same ' \
            'filesystem view rtorrent has (containers, mounts)?', False
    if isinstance(e, OSError):
        return WHERE_RTREMOTE, 'filesystem error: %s' % e, 'check disk space and permissions under RTR_DATA_ROOT', False
    return WHERE_RTREMOTE, 'internal error: %r' % (e,), \
        'this is a bug in rtremote; please report it with the traceback below', True


def log_failure(context, e, level=None):
    where, message, hint, is_bug = describe_failure(e)
    if level is None:
        level = logging.ERROR if (is_bug or where == WHERE_RTREMOTE) else logging.WARNING
    diag.log(level, where, '%s failed: %s' % (context, message), hint, exc_info=e if is_bug else None)
    return where, message


def error_text(where, message):
    # the JSON-RPC error text the app shows; leads with the component so the
    # user sees "rtorrent: ..." or "rtremote: ..." on the phone as well
    return '%s: %s' % (where, message)


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
        diag.app('invalid JSON from %s (%s); dropping the connection' % (str(websocket.remote_address), e),
                 'whatever connected is not the rtorrent remote app, or a very old build of it')
        return None, False
    if not isinstance(req, dict) or req.get('jsonrpc') != '2.0' or 'method' not in req or 'id' not in req:
        diag.app('malformed JSON-RPC from %s; dropping the connection' % str(websocket.remote_address),
                 'the request lacks jsonrpc/method/id; not the rtorrent remote app, or a broken build')
        return None, False
    method = req['method']
    params = req.get('params') if isinstance(req.get('params'), dict) else {}
    if await Cached.is_registered(websocket):
        # re-register switches the client's view and returns a fresh snapshot
        if method == 'register':
            response_json = await handle_register(req, websocket)
            if response_json is None:
                return None, False
            return prepare_response(response_json), True
        handler = WRITE_HANDLERS.get(method)
        if handler is not None:
            response_json = await handler(req['id'], params)
        elif method in READ_HASH_METHODS:
            response_json = await handle_method_with_hash(req['id'], method, params.get('hash'))
        else:
            # registered client, unknown method: JSON-RPC error, connection stays usable
            diag.app('%s asked for unknown method %r' % (str(websocket.remote_address), method),
                     'the app is newer than this rtremote (protocol level %d): update rtremote'
                     % RTR_PROTOCOL_VERSION)
            response_json = get_json_error(req['id'], JSONRPC_METHOD_NOT_FOUND,
                                           error_text(WHERE_RTREMOTE, 'unknown method %s; update rtremote' % method))
        error = response_json.get('error')
        if error and error.get('code') == JSONRPC_INVALID_PARAMS:
            diag.app('rejected %s from %s: %s' % (method, str(websocket.remote_address), error.get('message')),
                     'the app sent parameters this rtremote does not accept; if the app is newer than rtremote, '
                     'update rtremote, otherwise report it as an app bug')
        return prepare_response(response_json), True
    # unregistered: only a successful register keeps the socket (no auth oracle)
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
        diag.app('registration refused for %s: secret key mismatch' % str(websocket.remote_address),
                 'the secret key entered in the app is not the one whose SHA1 is RTR_SECRET_KEY_SHA1; '
                 'the app gets no error on purpose (no auth oracle), so this line is the only trace')
        return None
    view = params.get('view')
    view_name = Cached.get_view_name(view) if isinstance(view, str) else Cached.VIEW_DEFAULT
    data = await Cached.get_global()
    torrents = await Cached.get_torrents()
    if data is None or torrents is None:
        diag.rtorrent('%s registered before rtorrent ever answered; dropping it' % str(websocket.remote_address),
                      'rtremote is still waiting for rtorrent (see the lines above); the app will retry')
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
    if Cached.throttle_groups:
        result['throttle_groups'] = [{'name': n, 'up_kb': u, 'down_kb': d}
                                     for n, (u, d) in Cached.throttle_groups.items()]
    if Cached.custom_views:
        result['views'] = [{'name': n, 'filter': f} for n, f in Cached.custom_views.items()]
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
    except Exception as e:
        where, message = log_failure('%s(%s)' % (method, hash), e)
        return get_json_error(req_id, JSONRPC_INTERNAL_ERROR, error_text(where, message))
    return get_json_response(req_id, {result_key: [x.__dict__ for x in data]})


def _err(req_id, code, message):
    return get_json_error(req_id, code, message)


def _invalid(req_id, message='invalid params'):
    return get_json_error(req_id, JSONRPC_INVALID_PARAMS, message)


def _valid_hash(hash):
    return isinstance(hash, str) and INFO_HASH_RE.fullmatch(hash)


def _valid_int(value, min_value=0, max_value=Remote.XMLRPC_I8_MAX):
    # bool is an int subclass in Python; a JSON true/false must never pass as 1/0
    return not isinstance(value, bool) and isinstance(value, int) and min_value <= value <= max_value


async def _run_write(req_id, label, func, *args):
    # run a validated rtorrent write off the event loop, map faults to JSON-RPC,
    # and trigger an immediate updater tick so the change is pushed at once
    try:
        result = await asyncio.to_thread(func, *args)
    except Exception as e:
        where, message = log_failure('write %s' % label, e)
        return get_json_error(req_id, JSONRPC_INTERNAL_ERROR, error_text(where, message)), False
    diag.app('write %s ok' % label, level=logging.INFO)
    Cached.update_now.set()
    return result, True


async def handle_set_global(req_id, params):
    # M0 (level 2), extended with the M3 global peer-limit keys. Everything is
    # validated against the GLOBAL_SETTERS allowlist before any command is built.
    key = params.get('key')
    setter = Remote.GLOBAL_SETTERS.get(key) if isinstance(key, str) else None
    if setter is None:
        return _invalid(req_id, 'key is not settable')
    value = params.get('value')
    _, min_value, max_value, _ = setter
    if not _valid_int(value, min_value, max_value):
        return _invalid(req_id, 'invalid value')
    response, ok = await _run_write(req_id, 'set_global: %s=%s' % (key, value),
                                    Remote(SOCK_PATH).set_global, key, value)
    return get_json_response(req_id, {'key': key, 'value': value}) if ok else response


async def handle_torrent_action(req_id, params):  # M1
    hash = params.get('hash')
    action = params.get('action')
    if not _valid_hash(hash):
        return _invalid(req_id, 'invalid hash')
    if action not in Remote.TORRENT_ACTIONS:
        return _invalid(req_id, 'unknown action')
    response, ok = await _run_write(req_id, 'torrent_action %s %s' % (action, hash),
                                    Remote(SOCK_PATH).torrent_action, hash, action)
    return get_json_response(req_id, {'hash': hash, 'action': action}) if ok else response


async def handle_set_priority(req_id, params):  # M1
    hash = params.get('hash')
    value = params.get('priority')
    if not _valid_hash(hash):
        return _invalid(req_id, 'invalid hash')
    if not _valid_int(value, 0, 3):
        return _invalid(req_id, 'priority must be 0..3')
    response, ok = await _run_write(req_id, 'set_priority %s=%s' % (hash, value),
                                    Remote(SOCK_PATH).set_priority, hash, value)
    return get_json_response(req_id, {'hash': hash, 'priority': value}) if ok else response


async def handle_set_file_priority(req_id, params):  # M2
    hash = params.get('hash')
    index = params.get('file_index')
    value = params.get('priority')
    if not _valid_hash(hash):
        return _invalid(req_id, 'invalid hash')
    if not _valid_int(index, 0):
        return _invalid(req_id, 'invalid file index')
    if not _valid_int(value, 0, 2):
        return _invalid(req_id, 'priority must be 0..2')
    response, ok = await _run_write(req_id, 'set_file_priority %s:f%s=%s' % (hash, index, value),
                                    Remote(SOCK_PATH).set_file_priority, hash, index, value)
    return get_json_response(req_id, {'hash': hash, 'file_index': index, 'priority': value}) if ok else response


async def handle_set_tracker_enabled(req_id, params):  # M2
    hash = params.get('hash')
    index = params.get('tracker_index')
    enabled = params.get('enabled')
    if not _valid_hash(hash):
        return _invalid(req_id, 'invalid hash')
    if not _valid_int(index, 0):
        return _invalid(req_id, 'invalid tracker index')
    if not _valid_int(enabled, 0, 1):
        return _invalid(req_id, 'enabled must be 0 or 1')
    response, ok = await _run_write(req_id, 'set_tracker_enabled %s:t%s=%s' % (hash, index, enabled),
                                    Remote(SOCK_PATH).set_tracker_enabled, hash, index, enabled)
    return get_json_response(req_id, {'hash': hash, 'tracker_index': index, 'enabled': enabled}) if ok else response


async def handle_set_torrent_limit(req_id, params):  # M3
    hash = params.get('hash')
    key = params.get('key')
    value = params.get('value')
    if not _valid_hash(hash):
        return _invalid(req_id, 'invalid hash')
    if key not in Remote.TORRENT_LIMITS:
        return _invalid(req_id, 'unknown limit')
    if not _valid_int(value, 0):
        return _invalid(req_id, 'invalid value')
    response, ok = await _run_write(req_id, 'set_torrent_limit %s %s=%s' % (hash, key, value),
                                    Remote(SOCK_PATH).set_torrent_limit, hash, key, value)
    return get_json_response(req_id, {'hash': hash, 'key': key, 'value': value}) if ok else response


async def handle_set_throttle_name(req_id, params):  # M3
    hash = params.get('hash')
    name = params.get('name')
    if not _valid_hash(hash):
        return _invalid(req_id, 'invalid hash')
    if not isinstance(name, str):
        return _invalid(req_id, 'invalid name')
    # empty detaches; otherwise the group must be one rtremote created
    if name and name not in Cached.throttle_groups:
        return _invalid(req_id, 'unknown throttle group')
    response, ok = await _run_write(req_id, 'set_throttle_name %s=%s' % (hash, name),
                                    Remote(SOCK_PATH).set_throttle_name, hash, name)
    return get_json_response(req_id, {'hash': hash, 'name': name}) if ok else response


async def handle_set_label(req_id, params):  # M3
    hash = params.get('hash')
    label = params.get('label')
    if not _valid_hash(hash):
        return _invalid(req_id, 'invalid hash')
    if not isinstance(label, str) or len(label) > Remote.LABEL_MAX or any(ord(c) < 0x20 for c in label):
        return _invalid(req_id, 'invalid label')
    response, ok = await _run_write(req_id, 'set_label %s=%s' % (hash, label),
                                    Remote(SOCK_PATH).set_label, hash, label)
    return get_json_response(req_id, {'hash': hash, 'label': label}) if ok else response


async def handle_peer_action(req_id, params):  # M3
    hash = params.get('hash')
    peer = params.get('peer')
    action = params.get('peer_action')
    if not _valid_hash(hash):
        return _invalid(req_id, 'invalid hash')
    if not isinstance(peer, str) or not INFO_HASH_RE.fullmatch(peer):
        return _invalid(req_id, 'invalid peer id')
    if action not in Remote.PEER_ACTIONS:
        return _invalid(req_id, 'unknown peer action')
    response, ok = await _run_write(req_id, 'peer_action %s %s:p%s' % (action, hash, peer),
                                    Remote(SOCK_PATH).peer_action, hash, peer, action)
    return get_json_response(req_id, {'hash': hash, 'peer': peer, 'peer_action': action}) if ok else response


async def handle_add_torrent(req_id, params):  # M2/M5
    magnet = params.get('magnet')
    content = params.get('content_b64')
    start = params.get('start', True)
    directory = params.get('directory')
    label = params.get('label')
    if not isinstance(start, bool):
        return _invalid(req_id, 'start must be a boolean')
    if isinstance(magnet, str) and MAGNET_RE.match(magnet):
        magnet, content = magnet, None
    elif isinstance(content, str) and content:
        try:
            base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError):
            return _invalid(req_id, 'content_b64 is not valid base64')
        magnet = None
    else:
        return _invalid(req_id, 'provide a magnet link or content_b64')
    if directory is not None and (not isinstance(directory, str) or not SAFE_PATH_RE.match(directory)):
        return _invalid(req_id, 'invalid directory')
    if label is not None and (not isinstance(label, str) or len(label) > Remote.LABEL_MAX
                              or any(ord(c) < 0x20 for c in label)):
        return _invalid(req_id, 'invalid label')
    response, ok = await _run_write(req_id, 'add_torrent',
                                    Remote(SOCK_PATH).add_torrent, magnet, content, start, directory, label)
    return get_json_response(req_id, {'added': True}) if ok else response


async def handle_add_tracker(req_id, params):  # M5
    hash = params.get('hash')
    url = params.get('tracker_url')
    if not _valid_hash(hash):
        return _invalid(req_id, 'invalid hash')
    if not isinstance(url, str) or not URI_RE.match(url):
        return _invalid(req_id, 'invalid tracker url')
    response, ok = await _run_write(req_id, 'add_tracker %s %s' % (hash, url),
                                    Remote(SOCK_PATH).add_tracker, hash, url)
    return get_json_response(req_id, {'hash': hash, 'tracker_url': url}) if ok else response


async def handle_erase_torrent(req_id, params):  # M1 (bare) / M5 (with_data)
    hash = params.get('hash')
    with_data = params.get('with_data', False)
    if not _valid_hash(hash):
        return _invalid(req_id, 'invalid hash')
    if not isinstance(with_data, bool):
        return _invalid(req_id, 'with_data must be a boolean')
    response, ok = await _run_write(req_id, 'erase_torrent %s with_data=%s' % (hash, with_data),
                                    Remote(SOCK_PATH).erase_torrent, hash, with_data, RTR_DATA_ROOT)
    return get_json_response(req_id, {'hash': hash, 'with_data': with_data}) if ok else response


async def handle_move_data(req_id, params):  # M7
    hash = params.get('hash')
    directory = params.get('directory')
    if not _valid_hash(hash):
        return _invalid(req_id, 'invalid hash')
    if not isinstance(directory, str) or not SAFE_PATH_RE.match(directory):
        return _invalid(req_id, 'invalid directory')
    response, ok = await _run_write(req_id, 'move_data %s -> %s' % (hash, directory),
                                    Remote(SOCK_PATH).move_data, hash, directory, RTR_DATA_ROOT)
    return get_json_response(req_id, {'hash': hash, 'directory': response}) if ok else response


async def handle_throttle_group(req_id, params):  # M6
    name = params.get('name')
    up_kb = params.get('up_kb', 0)
    down_kb = params.get('down_kb', 0)
    if not isinstance(name, str) or not SAFE_GROUP_RE.match(name):
        return _invalid(req_id, 'invalid group name')
    if not _valid_int(up_kb, 0) or not _valid_int(down_kb, 0):
        return _invalid(req_id, 'invalid rate')

    def apply():
        Remote(SOCK_PATH).throttle_group_set(name, up_kb, down_kb)
        Cached.throttle_groups[name] = (up_kb, down_kb)

    response, ok = await _run_write(req_id, 'throttle_group %s up=%s down=%s' % (name, up_kb, down_kb), apply)
    return get_json_response(req_id, {'name': name, 'up_kb': up_kb, 'down_kb': down_kb}) if ok else response


async def handle_set_schedule(req_id, params):  # M6
    fields = {}
    for key in ('up_day', 'down_day', 'up_night', 'down_night'):
        value = params.get(key, 0)
        if not _valid_int(value, 0):
            return _invalid(req_id, 'invalid ' + key)
        fields[key] = value
    day = params.get('day_hhmm')
    night = params.get('night_hhmm')
    if not _valid_hhmm(day) or not _valid_hhmm(night):
        return _invalid(req_id, 'times must be HH:MM')

    def apply():
        r = Remote(SOCK_PATH)
        r.set_schedule(SCHEDULE_DAY_UP, day + ':00', 'throttle.global_up.max_rate.set_kb=%d' % fields['up_day'])
        r.set_schedule(SCHEDULE_DAY_DOWN, day + ':00', 'throttle.global_down.max_rate.set_kb=%d' % fields['down_day'])
        r.set_schedule(SCHEDULE_NIGHT_UP, night + ':00', 'throttle.global_up.max_rate.set_kb=%d' % fields['up_night'])
        r.set_schedule(SCHEDULE_NIGHT_DOWN, night + ':00', 'throttle.global_down.max_rate.set_kb=%d' % fields['down_night'])

    response, ok = await _run_write(req_id, 'set_schedule', apply)
    return get_json_response(req_id, dict(fields, day_hhmm=day, night_hhmm=night)) if ok else response


async def handle_clear_schedule(req_id, params):  # M6
    def apply():
        r = Remote(SOCK_PATH)
        for name in (SCHEDULE_DAY_UP, SCHEDULE_DAY_DOWN, SCHEDULE_NIGHT_UP, SCHEDULE_NIGHT_DOWN):
            r.remove_schedule(name)

    response, ok = await _run_write(req_id, 'clear_schedule', apply)
    return get_json_response(req_id, {'cleared': True}) if ok else response


async def handle_add_view(req_id, params):  # M7
    name = params.get('name')
    preset = params.get('filter')
    if not isinstance(name, str) or not SAFE_GROUP_RE.match(name) or name in Cached.VIEWS:
        return _invalid(req_id, 'invalid or reserved view name')
    condition = VIEW_FILTER_PRESETS.get(preset)
    if condition is None:
        return _invalid(req_id, 'unknown filter preset')

    def apply():
        Remote(SOCK_PATH).add_view(name, condition)
        Cached.VIEWS.add(name)
        Cached.custom_views[name] = preset

    response, ok = await _run_write(req_id, 'add_view %s (%s)' % (name, preset), apply)
    return get_json_response(req_id, {'name': name, 'filter': preset}) if ok else response


def _valid_hhmm(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-2][0-9]:[0-5][0-9]', value):
        return False
    hh = int(value[:2])
    return hh < 24


# safe filter presets a custom view may use; rtremote maps each to an rtorrent
# filter expression, so no client-supplied condition text ever reaches rtorrent
VIEW_FILTER_PRESETS = {
    'all': '',
    'active': 'greater=value=$d.up.rate=,value=0',
    'downloading': 'd.complete=,false=',
    'complete': 'd.complete=',
    'seeding': 'and={d.complete=,d.is_open=}',
    'stopped': 'not=$d.is_open=',
}

READ_HASH_METHODS = {'get_files', 'get_peers', 'get_trackers'}
WRITE_HANDLERS = {
    'set_global': handle_set_global,
    'torrent_action': handle_torrent_action,
    'set_priority': handle_set_priority,
    'set_file_priority': handle_set_file_priority,
    'set_tracker_enabled': handle_set_tracker_enabled,
    'set_torrent_limit': handle_set_torrent_limit,
    'set_throttle_name': handle_set_throttle_name,
    'set_label': handle_set_label,
    'peer_action': handle_peer_action,
    'add_torrent': handle_add_torrent,
    'add_tracker': handle_add_tracker,
    'erase_torrent': handle_erase_torrent,
    'move_data': handle_move_data,
    'throttle_group': handle_throttle_group,
    'set_schedule': handle_set_schedule,
    'clear_schedule': handle_clear_schedule,
    'add_view': handle_add_view,
}


class Outage:
    # collapses a run of identical poll failures into one line at the start and
    # one at recovery, instead of a traceback every RTR_RETR_INTERVAL seconds
    def __init__(self):
        self.since = None
        self.count = 0
        self.last = None

    def failed(self, e):
        where, message, hint, is_bug = describe_failure(e)
        self.count += 1
        if self.since is None:
            self.since = time.monotonic()
            self.last = message
            diag.log(logging.ERROR, where, 'rtorrent poll failed: %s (retrying every %ds; identical failures '
                     'are not logged again)' % (message, RTR_RETR_INTERVAL), hint, exc_info=e if is_bug else None)
        elif message != self.last:
            self.last = message
            diag.log(logging.ERROR, where, 'rtorrent poll still failing, now: %s' % message, hint)
        else:
            logger.debug('rtorrent poll failed again (%d): %s' % (self.count, message), extra={'where': where})

    def recovered(self, g):
        if self.since is None:
            return
        diag.rtorrent('rtorrent is back after %ds (%d failed polls): rtorrent %s, api %s' % (
            time.monotonic() - self.since, self.count, getattr(g, 'system_client_version', '?'),
            getattr(g, 'system_api_version', '?')), level=logging.INFO)
        self.since, self.count, self.last = None, 0, None


async def global_data_updater(ready):
    remote = Remote(SOCK_PATH)
    outage = Outage()
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
                outage.recovered(g)
                diag.rtorrent('connected: rtorrent %s (libtorrent %s, api %s) at %s; %d torrents' % (
                    getattr(g, 'system_client_version', '?'), getattr(g, 'system_library_version', '?'),
                    getattr(g, 'system_api_version', '?'), SOCK_PATH, len(data)), level=logging.INFO)
            else:
                outage.recovered(await Cached.get_global())
                if len(new_data) > 0:
                    await Cached.notify_clients(new_data)
        except Exception as e:
            # a failed poll (e.g. rtorrent restarting) must not kill the server
            outage.failed(e)
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
        # compression is negotiated per connection: the app offers permessage-deflate and
        # serve() accepts it by default, so 'none' means an app build from before OkHttp or
        # something in between (a reverse proxy) that stripped the extension
        compression = ', '.join(e.name for e in websocket.protocol.extensions) or 'none'
        diag.app('connection from %s, compression: %s' % (str(websocket.remote_address), compression),
                 level=logging.INFO)
        async for message in websocket:
            response, keep = await process_request(message, websocket)
            if not keep:
                diag.app('dropping %s' % str(websocket.remote_address), level=logging.INFO)
                break
            if response:
                await websocket.send(response)
    except WebSocketException as e:
        diag.app('connection %s ended: %s' % (str(websocket.remote_address), e),
                 'normal when the app closes or loses network', level=logging.INFO)
    except Exception as e:
        log_failure('connection handler for %s' % str(websocket.remote_address), e)
    finally:
        await Cached.remove_client(websocket)


def handle_signal(stop_event, signum):
    diag.rtremote('signal %d received, shutting down' % signum, level=logging.INFO)
    stop_event.set()


def create_pid():
    try:
        with open(RTR_PID_PATH, 'w') as f:
            f.write(str(os.getpid()))
            f.flush()
    except Exception as e:
        diag.rtremote('cannot create PID file %s: %s' % (RTR_PID_PATH, e),
                      'RTR_PID_PATH must be writable by the user running rtremote')


def delete_pid():
    try:
        os.remove(RTR_PID_PATH)
    except Exception as e:
        diag.rtremote('removing PID file %s failed: %s' % (RTR_PID_PATH, e), level=logging.WARNING)


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


def unix_socket_path():
    # the filesystem path behind SOCK_PATH, or None for inet: sockets
    if SOCK_PATH.startswith('inet:'):
        return None
    for prefix in ('unix:', 'local:'):
        if SOCK_PATH.startswith(prefix):
            return SOCK_PATH[len(prefix):]
    return SOCK_PATH


def log_startup_banner():
    # one block a user can paste into a bug report: what runs where, with what
    import websockets
    diag.rtremote('starting rtremote %s (protocol level %d) on python %s, websockets %s, %s' % (
        RTR_VERSION, RTR_PROTOCOL_VERSION, platform.python_version(), websockets.__version__,
        platform.platform()), level=logging.INFO)
    diag.rtremote('config: listen=%s:%d cert=%s scgi=%s interval=%ds scgi_timeout=%ds data_root=%s log_level=%s' % (
        RTR_LISTEN_HOST, RTR_LISTEN_PORT, RTR_CERT_PATH, SOCK_PATH, RTR_RETR_INTERVAL, RTR_SCGI_TIMEOUT,
        RTR_DATA_ROOT or '(unset: erase-with-data / move disabled)', logging.getLevelName(logger.level)),
        level=logging.INFO)
    if RTR_SECRET_KEY_SHA1 == DEFAULT_SECRET_KEY_SHA1:
        diag.rtremote('SECURITY WARNING: RTR_SECRET_KEY_SHA1 is not set; the server accepts the publicly '
                      'known default secret', 'set RTR_SECRET_KEY_SHA1 in start.sh (see README)')
    path = unix_socket_path()
    if path is not None and not os.path.exists(path):
        diag.rtorrent('rtorrent SCGI socket %s does not exist yet' % path,
                      'rtremote waits and retries; if this persists, rtorrent is not running or '
                      'RTR_SCGI_SOCKET_PATH is not its network.scgi.open_local path', level=logging.WARNING)


TLS_TRUST_REASONS = ('TLSV1_ALERT_UNKNOWN_CA', 'SSLV3_ALERT_CERTIFICATE_UNKNOWN', 'SSLV3_ALERT_BAD_CERTIFICATE',
                     'TLSV1_ALERT_DECRYPT_ERROR', 'SSLV3_ALERT_HANDSHAKE_FAILURE')
TLS_PLAINTEXT_REASONS = ('HTTP_REQUEST', 'WRONG_VERSION_NUMBER', 'UNKNOWN_PROTOCOL')
TLS_VERSION_REASONS = ('NO_SHARED_CIPHER', 'UNSUPPORTED_PROTOCOL', 'VERSION_TOO_LOW', 'NO_PROTOCOLS_AVAILABLE')


def log_tls_handshake_failure(e):
    # asyncio only logs a failed TLS handshake in debug mode (SSLError is an
    # OSError), so a phone that rejects our certificate would leave no trace;
    # this is called from DiagnosingSSLContext with the handshake exception
    reason = getattr(e, 'reason', None) or ''
    if reason in TLS_TRUST_REASONS:
        diag.app('a client rejected this server\'s TLS certificate (%s)' % reason,
                 'the app does not trust %s: either put the matching rtr_keystore.jks in the app\'s files '
                 'dir with the right keystore password, or enable "accept self-signed" in the app; also '
                 'check the host name in the app matches the certificate' % RTR_CERT_PATH)
    elif reason in TLS_PLAINTEXT_REASONS:
        diag.app('a client spoke plain text to the TLS port (%s)' % reason,
                 'something connected with ws:// or http:// instead of wss://; the app always uses wss, so '
                 'this is a browser, a proxy or a scanner')
    elif reason in TLS_VERSION_REASONS:
        diag.app('TLS version or cipher mismatch with a client (%s)' % reason,
                 'rtremote requires TLS 1.2+; the client offered nothing compatible')
    elif isinstance(e, ssl.SSLEOFError) or 'EOF' in reason:
        diag.app('a client closed the connection during the TLS handshake',
                 'usually the app rejecting the certificate without sending an alert (see the keystore / '
                 '"accept self-signed" settings), otherwise a port scanner')
    else:
        diag.app('TLS handshake with a client failed: %s' % e,
                 'if this is the phone, check the certificate trust settings in the app')


class DiagnosingSSLContext(ssl.SSLContext):
    # Diagnoses failed TLS handshakes, which asyncio otherwise swallows (an
    # SSLError is an OSError, logged only in loop debug mode). Three cases:
    # - the handshake itself fails (alert, plain text, version mismatch):
    #   caught around SSLObject.do_handshake;
    # - with TLS 1.3 the server-side handshake completes before the client has
    #   verified our certificate, so its "unknown CA" alert arrives on the
    #   first read: caught around SSLObject.read until application data flows;
    # - the client just closes the socket mid-handshake (an app that rejects
    #   the certificate does exactly this): asyncio then drops the connection
    #   without touching the SSLObject again, so a weakref finalizer reports a
    #   handshake that was still pending when its SSLObject was freed.
    # The wrappers hold only a weak reference to the SSLObject: a strong one
    # would form a cycle and delay that finalizer until the cyclic GC runs.
    pending = {}  # id(sslobj) -> [handshake start time, do_handshake calls]

    def wrap_bio(self, *args, **kwargs):
        sslobj = super().wrap_bio(*args, **kwargs)
        key = id(sslobj)
        entry = [time.monotonic(), 0]
        DiagnosingSSLContext.pending[key] = entry
        ref = weakref.ref(sslobj)

        def guarded(unbound, is_handshake):
            def wrapper(*a, **kw):
                obj = ref()
                if is_handshake:
                    # asyncio calls do_handshake once on connect and then once per
                    # received chunk: a second call means the client sent TLS data
                    entry[1] += 1
                try:
                    result = unbound(obj, *a, **kw)
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                    raise  # normal non-blocking progress, not a failure
                except ssl.SSLError as e:
                    DiagnosingSSLContext.pending.pop(key, None)
                    log_tls_handshake_failure(e)
                    raise
                if is_handshake:
                    DiagnosingSSLContext.pending.pop(key, None)
                elif obj is not None:
                    del obj.read  # application data flows: back to the plain method
                return result
            return wrapper
        sslobj.do_handshake = guarded(ssl.SSLObject.do_handshake, True)
        sslobj.read = guarded(ssl.SSLObject.read, False)
        weakref.finalize(sslobj, DiagnosingSSLContext._freed, key)
        return sslobj

    @staticmethod
    def _freed(key):
        entry = DiagnosingSSLContext.pending.pop(key, None)
        if entry is None:
            return  # handshake had completed or was already diagnosed
        started, calls = entry
        elapsed = time.monotonic() - started
        if calls <= 1:
            diag.app('a connection closed before sending any TLS data',
                     'a TCP port probe (monitoring, scanner, or the app checking reachability), not a failed app '
                     'connection', level=logging.INFO)
        elif elapsed > 10:
            diag.app('a TLS handshake never completed (dropped after %ds)' % elapsed,
                     'a client connected but did not finish TLS: a firewall, proxy or scanner rather than the app',
                     level=logging.INFO)
        else:
            log_tls_handshake_failure(ssl.SSLEOFError('client closed during handshake'))


def load_ssl_context():
    ssl_context = DiagnosingSSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ssl_context.load_cert_chain(RTR_CERT_PATH)
    except FileNotFoundError:
        diag.rtremote('TLS certificate %s not found' % RTR_CERT_PATH,
                      'RTR_CERT_PATH must point at a PEM file holding the certificate and its private key '
                      '(cert/howto.txt)')
        raise
    except (ssl.SSLError, PermissionError) as e:
        diag.rtremote('TLS certificate %s cannot be loaded: %s' % (RTR_CERT_PATH, e),
                      'the PEM must contain both the certificate and the unencrypted private key, readable '
                      'by the user running rtremote')
        raise
    return ssl_context


async def amain():
    Cached.init_async()
    log_startup_banner()
    ssl_context = load_ssl_context()
    stop = asyncio.Event()
    ready = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGUSR1, signal.SIGTERM):
        loop.add_signal_handler(s, handle_signal, stop, s)
    updater = loop.create_task(global_data_updater(ready))
    cleaner = loop.create_task(short_caches_cleaner())
    logger.debug('data updater and short caches cleaner scheduled', extra={'where': WHERE_RTREMOTE})
    # only accept clients once the first rtorrent snapshot exists (or we are stopped)
    ready_task = loop.create_task(ready.wait())
    stop_task = loop.create_task(stop.wait())
    try:
        await asyncio.wait({ready_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if not stop.is_set():
            try:
                async with serve(on_message, RTR_LISTEN_HOST, RTR_LISTEN_PORT, ssl=ssl_context):
                    diag.rtremote('listening on wss://%s:%d; the app can connect now'
                                  % (RTR_LISTEN_HOST, RTR_LISTEN_PORT), level=logging.INFO)
                    await stop.wait()
            except OSError as e:
                if e.errno == errno.EADDRINUSE:
                    diag.rtremote('cannot listen on %s:%d: address already in use' % (RTR_LISTEN_HOST, RTR_LISTEN_PORT),
                                  'another rtremote (or another service) already uses RTR_LISTEN_PORT; check the '
                                  'PID in %s' % RTR_PID_PATH)
                elif e.errno in (errno.EADDRNOTAVAIL, errno.EACCES):
                    diag.rtremote('cannot listen on %s:%d: %s' % (RTR_LISTEN_HOST, RTR_LISTEN_PORT, e.strerror),
                                  'RTR_LISTEN_HOST must be an address of this machine (or 0.0.0.0) and ports below '
                                  '1024 need privileges')
                else:
                    diag.rtremote('cannot listen on %s:%d: %s' % (RTR_LISTEN_HOST, RTR_LISTEN_PORT, e))
                raise
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
    except (OSError, ssl.SSLError):
        pass  # already diagnosed where it happened
    except Exception as e:
        log_failure('rtremote main loop', e)
    finally:
        diag.rtremote('stopped', level=logging.INFO)
        delete_pid()


if __name__ == '__main__':
    main()
