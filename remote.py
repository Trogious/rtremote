import base64
import binascii
import os
import re
import shutil

from rpc import RpcError, RTorrentRpc
from utils import WHERE_APP, WHERE_RTREMOTE

SAFE_PATH_RE = re.compile(r'^/[\w /.\-+@]{0,1024}$')
SAFE_GROUP_RE = re.compile(r'^[A-Za-z0-9_.\-]{1,64}$')
HEX40_RE = re.compile(r'^[0-9A-Fa-f]{40}$')
MAGNET_RE = re.compile(r'^magnet:\?', re.IGNORECASE)
URI_RE = re.compile(r'^(https?|udp)://[^\s<>"]{1,2048}$', re.IGNORECASE)


class Remote:
    # Requires rtorrent >= 0.16 (system.api_version >= 26). rtorrent 0.9.x is NOT
    # supported by this version; rtremote v1.5.0 is the last release for 0.9.x.
    #
    # The JSON field names are part of the Android app protocol and must stay
    # stable, so rtorrent commands that were renamed in 0.16 are requested under
    # their new names and aliased back to the field names the app knows.
    GLOBAL_COMMANDS = [
        'throttle.global_down.rate', 'throttle.global_up.rate', 'throttle.global_down.max_rate', 'throttle.global_up.max_rate',
        'network.max_open_files', 'throttle.max_downloads', 'throttle.max_uploads', 'network.http.max_total_connections',
        'system.sockets.size', 'system.sockets.max_size', 'throttle.unchoked_uploads', 'throttle.unchoked_downloads',
        'network.listen.port', 'network.listen.port.range', 'system.client_version', 'system.library_version', 'system.hostname',
        'system.pid', 'system.cwd', 'session.path', 'system.api_version',
        'network.http.current_open', 'network.total_handshakes', 'network.open_files',
        'throttle.max_unchoked_uploads', 'throttle.max_unchoked_downloads',
        'throttle.max_uploads.global', 'throttle.max_downloads.global',
        'throttle.min_peers.normal', 'throttle.max_peers.normal',
        'throttle.min_peers.seed', 'throttle.max_peers.seed',
    ]
    # attribute produced by the rtorrent command -> wire field name the app expects
    GLOBAL_ALIASES = {
        'network_listen_port_range': 'network_port_range',
        'network_http_max_total_connections': 'network_http_max_open',
        'system_sockets_size': 'network_open_sockets',
        'system_sockets_max_size': 'network_max_open_sockets',
    }
    TORRENT_ALIASES = {
        'tracker_has_active_not_scrape': 'has_active_not_scrape',
    }
    # set_global allowlist: wire field -> (rtorrent setter, min, max, untrusted-safe).
    # Values out of [min, max] (and non-integers) are rejected before any RPC is
    # built; nothing outside this map is ever settable. XMLRPC_I8_MAX keeps the
    # value encodable as XML-RPC <i8>. untrusted-safe mirrors rtorrent's own
    # mark_safe allowlist (verified against v0.16.22 and master): those setters
    # also carry the UNTRUSTED_CONNECTION=1 header as defence in depth;
    # system.sockets.max_size.set and network.listen.port.set are NOT on
    # rtorrent's list, so they must go out as trusted calls.
    XMLRPC_I8_MAX = 2 ** 63 - 1
    GLOBAL_SETTERS = {
        'throttle_global_up_max_rate': ('throttle.global_up.max_rate.set_kb', 0, XMLRPC_I8_MAX, True),
        'throttle_global_down_max_rate': ('throttle.global_down.max_rate.set_kb', 0, XMLRPC_I8_MAX, True),
        'throttle_max_uploads_global': ('throttle.max_uploads.global.set', 0, XMLRPC_I8_MAX, True),
        'throttle_max_downloads_global': ('throttle.max_downloads.global.set', 0, XMLRPC_I8_MAX, True),
        'throttle_max_uploads': ('throttle.max_uploads.set', 0, XMLRPC_I8_MAX, True),
        'throttle_max_downloads': ('throttle.max_downloads.set', 0, XMLRPC_I8_MAX, True),
        'network_max_open_sockets': ('system.sockets.max_size.set', 0, XMLRPC_I8_MAX, False),
        'network_listen_port': ('network.listen.port.set', 1, 65535, False),
        # global peer limits (M3, level 5) - all rtorrent-untrusted-safe
        'throttle_min_peers_normal': ('throttle.min_peers.normal.set', 0, XMLRPC_I8_MAX, True),
        'throttle_max_peers_normal': ('throttle.max_peers.normal.set', 0, XMLRPC_I8_MAX, True),
        'throttle_min_peers_seed': ('throttle.min_peers.seed.set', -1, XMLRPC_I8_MAX, True),
        'throttle_max_peers_seed': ('throttle.max_peers.seed.set', -1, XMLRPC_I8_MAX, True),
    }

    # per-torrent action -> (rtorrent command, untrusted-safe). d.start/d.stop run
    # embedded visibility commands and d.tracker_announce hits the network, so
    # rtorrent does NOT mark them safe (verified v0.16.22 + master); the rest are.
    TORRENT_ACTIONS = {
        'start': ('d.start', False),
        'stop': ('d.stop', False),
        'pause': ('d.pause', True),
        'resume': ('d.resume', True),
        'open': ('d.open', True),
        'close': ('d.close', True),
        'check_hash': ('d.check_hash', True),
        'announce': ('d.tracker_announce', False),
    }
    # per-torrent integer limits (M3) -> rtorrent setter (none untrusted-safe)
    TORRENT_LIMITS = {
        'uploads_max': 'd.uploads_max.set',
        'downloads_max': 'd.downloads_max.set',
        'peers_max': 'd.peers_max.set',
    }
    PEER_ACTIONS = {
        'ban': ('p.banned.set', 1),
        'snub': ('p.snubbed.set', 1),
        'unsnub': ('p.snubbed.set', 0),
        'disconnect': ('p.disconnect', None),
    }
    INFO_HASH_LEN = 40
    LABEL_MAX = 200
    GROUP_NAME_MAX = 64

    def __init__(self, sock_path):
        self.sock_path = sock_path
        self.rpc = RTorrentRpc(sock_path)

    @staticmethod
    def apply_aliases(obj, aliases):
        for attr, wire_name in aliases.items():
            obj.__dict__[wire_name] = obj.__dict__.pop(attr)

    def get_global(self):
        g = self.rpc.global_data(Remote.GLOBAL_COMMANDS)
        Remote.apply_aliases(g, Remote.GLOBAL_ALIASES)
        return g

    def set_global(self, key, value):
        # key must have been validated against GLOBAL_SETTERS by the caller
        command, _, _, untrusted_safe = Remote.GLOBAL_SETTERS[key]
        self.rpc.set_value(command, value, untrusted=untrusted_safe)

    def get_torrents(self, view='main'):
        params = ['d.hash=', 'd.name=', 'd.size_bytes=', 'd.bytes_done=', 'd.complete=', 'd.up.rate=', 'd.down.rate=', 'd.up.total=',
                  'd.down.total=', 'd.ratio=', 'd.size_files=', 'd.tracker_size=', 'd.peers_connected=', 'd.tied_to_file=',
                  'd.ignore_commands=', 'd.is_open=', 'd.is_active=', 'd.hashing=', 'd.is_hash_checking=', 'd.chunks_hashed=', 'd.message=',
                  'd.size_chunks=', 'd.completed_chunks=', 'd.tracker.has_active_not_scrape=',
                  'd.custom1=', 'd.priority=', 'd.uploads_max=', 'd.downloads_max=', 'd.peers_max=', 'd.throttle_name=']
        torrents = self.rpc.d_multicall(params, view)
        for t in torrents:
            Remote.apply_aliases(t, Remote.TORRENT_ALIASES)
            if t.has_active_not_scrape == 1:
                t.trackers = [x.__dict__ for x in Remote.optimize_trackers_digest(self.get_trackers_digest(t.hash))]
        return torrents

    def get_torrents_hashes(self, view):
        return self.rpc.d_multicall(['d.hash='], view)

    def get_files(self, hash):
        params = ['f.size_chunks=', 'f.completed_chunks=', 'f.priority=', 'f.size_bytes=', 'f.path=']
        files = self.rpc.f_multicall(hash, params)
        return files

    def get_peers(self, hash):
        # missing in RPC:
        # is_down_choked_limited, is_down_queued, is_blocked, is_down_choked, is_down_interested, is_up_choked, is_up_interested,
        # outgoing_queue_size, incoming_queue_size, transfer->index, failed_counter
        params = ['p.id=', 'p.address=', 'p.up_rate=', 'p.down_rate=', 'p.peer_rate=',
                  'p.is_preferred=', 'p.is_encrypted=', 'p.is_incoming=', 'p.completed_percent=', 'p.client_version=']
        peers = self.rpc.p_multicall(hash, params)
        return peers

    def get_trackers(self, hash):
        # missing in RPC: key, is_requesting, is_promiscuous_mode, is_failure_mode
        # is_busy_not_scrape: latest_event != EVENT_SCRAPE && is_busy
        params = ['t.group=', 't.url=', 't.is_busy=', 't.latest_event=', 't.id=', 't.failed_counter=', 't.success_counter=',
                  't.scrape_counter=', 't.is_usable=', 't.is_enabled=', 't.scrape_complete=', 't.scrape_incomplete=',
                  't.scrape_downloaded=', 't.latest_new_peers=', 't.latest_sum_peers=']
        trackers = self.rpc.t_multicall(hash, params)
        return trackers

    def get_trackers_digest(self, hash):
        params = ['t.group=', 't.url=', 't.is_busy=', 't.latest_event=']
        trackers = self.rpc.t_multicall(hash, params)
        return trackers

    @staticmethod
    def optimize_trackers_digest(trackers):
        for t in trackers:
            del t.is_busy
            del t.latest_event
        return trackers

    # ---- writes (M1-M7); callers validate params, these build rtorrent calls ----

    def torrent_action(self, hash, action):
        command, safe = Remote.TORRENT_ACTIONS[action]
        self.rpc.target_command(command, hash, untrusted=safe)

    def set_priority(self, hash, value):
        # d.priority.set, 0 off / 1 low / 2 normal / 3 high (not untrusted-safe)
        self.rpc.target_command('d.priority.set', hash, [('i8', int(value))])

    def set_file_priority(self, hash, index, value):
        # f.priority.set, 0 off / 1 normal / 2 high; target <hash>:f<index> (safe)
        target = '%s:f%d' % (hash, int(index))
        self.rpc.target_command('f.priority.set', target, [('i8', int(value))], untrusted=True)

    def set_tracker_enabled(self, hash, index, enabled):
        target = '%s:t%d' % (hash, int(index))
        self.rpc.target_command('t.is_enabled.set', target, [('i8', 1 if enabled else 0)], untrusted=True)

    def set_torrent_limit(self, hash, key, value):
        self.rpc.target_command(Remote.TORRENT_LIMITS[key], hash, [('i8', int(value))])

    def set_throttle_name(self, hash, name):
        # empty name detaches from any group; a non-empty name must be a group
        # rtremote created (validated by the caller)
        self.rpc.target_command('d.throttle_name.set', hash, [('string', name)])

    def set_label(self, hash, label):
        self.rpc.target_command('d.custom1.set', hash, [('string', label)])

    def peer_action(self, hash, peer_id, action):
        command, value = Remote.PEER_ACTIONS[action]
        target = '%s:p%s' % (hash, peer_id)
        args = None if value is None else [('i8', value)]
        self.rpc.target_command(command, target, args, untrusted=True)

    def add_torrent(self, magnet=None, content_b64=None, start=True, directory=None, label=None):
        trailing = []
        if directory:
            trailing.append(('string', 'd.directory.set=' + directory))
        if label:
            trailing.append(('string', 'd.custom1.set=' + label))
        if magnet:
            command = 'load.start' if start else 'load.normal'
            self.rpc.target_command(command, '', [('string', magnet)] + trailing)
        else:
            command = 'load.raw_start' if start else 'load.raw'
            self.rpc.target_command(command, '', [('base64', content_b64)] + trailing)

    def add_tracker(self, hash, url):
        # d.tracker.insert(hash, group, url); group 0 is the primary group
        self.rpc.target_command('d.tracker.insert', hash, [('string', '0'), ('string', url)])

    def get_file_paths(self, hash):
        # absolute on-disk paths of a torrent's files (d.directory + each f.path)
        directory = self._get_string('d.directory', hash)
        files = self.rpc.f_multicall(hash, ['f.path='])
        return directory, [os.path.join(directory, f.path) for f in files]

    def _get_string(self, command, hash):
        data = self.rpc.target_command(command, hash)
        value = data['methodResponse']['params']['param']['value']
        return next(iter(value.values())) if isinstance(value, dict) else value

    def erase_torrent(self, hash, with_data=False, data_root=None):
        paths = []
        directory = None
        if with_data:
            directory, paths = self.get_file_paths(hash)
            for p in paths:
                Remote._require_under(p, data_root)
        self.rpc.target_command('d.erase', hash, untrusted=True)
        if with_data:
            for p in paths:
                try:
                    if os.path.isfile(p) or os.path.islink(p):
                        os.remove(p)
                except OSError:
                    pass
            # remove the torrent's own directory if it is now empty and under root
            try:
                if directory and Remote._is_under(directory, data_root) and not os.listdir(directory):
                    os.rmdir(directory)
            except OSError:
                pass

    def move_data(self, hash, directory, data_root):
        directory = os.path.normpath(directory)
        Remote._require_under(directory, data_root)
        src_dir, _ = self.get_file_paths(hash)
        src_dir = os.path.normpath(src_dir)
        Remote._require_under(src_dir, data_root)
        base = os.path.basename(src_dir.rstrip('/'))
        dst = os.path.join(directory, base)
        Remote._require_under(dst, data_root)
        self.rpc.target_command('d.close', hash, untrusted=True)
        os.makedirs(directory, exist_ok=True)
        if os.path.exists(src_dir):
            shutil.move(src_dir, dst)
        self.rpc.target_command('d.directory.set', hash, [('string', dst)])
        self.rpc.target_command('d.open', hash, untrusted=True)
        return dst

    def throttle_group_set(self, name, up_kb, down_kb):
        # throttle.up/throttle.down create-or-resize a named group; rate is KiB/s,
        # 0 = unlimited. rtorrent has no enumeration, so the caller tracks names.
        self.rpc.target_command('throttle.up', '', [('string', name), ('i8', int(up_kb))])
        self.rpc.target_command('throttle.down', '', [('string', name), ('i8', int(down_kb))])

    def throttle_group_rates(self, name):
        # returns (up_max_bytes, down_max_bytes); -1 unknown, 0 unlimited
        return (self._throttle_query('throttle.up.max', name),
                self._throttle_query('throttle.down.max', name))

    def _throttle_query(self, command, name):
        data = self.rpc.target_command(command, name)
        try:
            value = data['methodResponse']['params']['param']['value']
            return int(next(iter(value.values())) if isinstance(value, dict) else value)
        except (KeyError, TypeError, ValueError):
            return -1

    def add_view(self, name, condition):
        self.rpc.target_command('view.add', '', [('string', name)])
        self.rpc.target_command('view.filter', '', [('string', name), ('string', condition)])
        # populate immediately so the first register on the new view is not empty
        try:
            self.rpc.target_command('view.filter_on', '',
                                    [('string', name), ('string', 'event.download.inserted_new'),
                                     ('string', 'event.download.resumed')])
        except RpcError:
            pass

    def set_schedule(self, name, first_hhmmss, command):
        # rtremote owns fixed-name daily entries; command is a validated setter
        # string like 'throttle.global_up.max_rate.set_kb=1024'
        self.rpc.target_command('schedule', '',
                                [('string', name), ('string', first_hhmmss),
                                 ('string', '24:00:00'), ('string', command)])

    def remove_schedule(self, name):
        try:
            self.rpc.target_command('schedule.remove', '', [('string', name)])
        except RpcError:
            pass

    @staticmethod
    def _is_under(path, root):
        if not root:
            return False
        root = os.path.realpath(root)
        target = os.path.realpath(path)
        return target == root or target.startswith(root + os.sep)

    @staticmethod
    def _require_under(path, root):
        if not root:
            raise RpcError('data operations are not enabled on this server',
                           'set RTR_DATA_ROOT in start.sh to the directory rtremote may move or delete files under',
                           WHERE_RTREMOTE)
        if not Remote._is_under(path, root):
            raise RpcError('path is outside the configured data root',
                           'the app asked to touch %s, which is not under RTR_DATA_ROOT=%s' % (path, root),
                           WHERE_APP)
