"""A fake rtorrent SCGI/XML-RPC server for testing rtremote without rtorrent.

Mimics rtorrent 0.16.x: current command names (network.listen.port.range,
network.http.max_total_connections, d.tracker.has_active_not_scrape),
system.api_version 26 and the tinyxml2 compact XML output style (empty results
are a childless <data/>).

Speaks just enough of the XML-RPC dialect for server_wss.py:
- system.multicall (global getters; unknown commands answer with a fault
  struct in place of the value array, like the real thing)
- d.multicall / t.multicall / p.multicall / f.multicall
- plain getters and *.set / *.set_kb setters (for update-propagation tests);
  requests carrying UNTRUSTED_CONNECTION=1 are rejected for commands outside
  rtorrent's untrusted-safe allowlist, like the real thing
- per-torrent actions (d.start/stop/pause/resume/open/close/check_hash/erase),
  file priority (f.priority.set), tracker enable (t.is_enabled.set) and insert
  (d.tracker.insert), peer actions (p.banned/snubbed/disconnect via <hash>:p<id>),
  add torrent (load.start/load.raw_start), named throttle groups
  (throttle.up/down + throttle.up.max/down.max), custom views (view.*) and
  scheduling (schedule/schedule.remove) — all recorded so tests can observe them
- fake.add_torrent / fake.remove_torrent / fake.fail_next_set /
  fake.was_untrusted control methods
"""
import os
import socketserver
import threading
import urllib.parse
from xml.etree.ElementTree import fromstring
from xml.sax.saxutils import escape


# names rtorrent master keeps only as deprecated redirects: the fake refuses
# them so a regression back to the old names is caught by the smoke tests
DEPRECATED_COMMANDS = {'d.multicall2', 'network.open_sockets', 'network.max_open_sockets',
                       'network.max_open_sockets.set'}

# commands rtorrent marks rpc.mark_safe (usable on UNTRUSTED_CONNECTION=1
# requests), per v0.16.22 and master; like the real thing, the fake rejects
# any other command arriving on an untrusted request - this catches rtremote
# sending the untrusted header for a command rtorrent does not allow it on
UNTRUSTED_SAFE = {
    # global rate/slot/peer setters
    'throttle.global_up.max_rate.set_kb', 'throttle.global_down.max_rate.set_kb',
    'throttle.max_uploads.global.set', 'throttle.max_downloads.global.set',
    'throttle.max_uploads.set', 'throttle.max_downloads.set',
    'throttle.min_peers.normal.set', 'throttle.max_peers.normal.set',
    'throttle.min_peers.seed.set', 'throttle.max_peers.seed.set',
    # per-torrent actions rtorrent marks safe (start/stop/announce are NOT here)
    'd.pause', 'd.resume', 'd.open', 'd.close', 'd.check_hash', 'd.erase',
    # file / tracker / peer writes rtorrent marks safe
    'f.priority.set', 't.is_enabled.set',
    'p.banned.set', 'p.snubbed.set', 'p.disconnect',
}


def _fault(code, string):
    return ('<?xml version="1.0"?><methodResponse><fault><value><struct>'
            '<member><name>faultCode</name><value><i4>%d</i4></value></member>'
            '<member><name>faultString</name><value><string>%s</string></value></member>'
            '</struct></value></fault></methodResponse>' % (code, escape(string)))


def _fault_struct(code, string):
    # per-command fault inside a system.multicall response
    return ('<value><struct>'
            '<member><name>faultCode</name><value><i4>%d</i4></value></member>'
            '<member><name>faultString</name><value><string>%s</string></value></member>'
            '</struct></value>' % (code, escape(string)))


def _response(inner):
    return '<?xml version="1.0"?><methodResponse><params><param>%s</param></params></methodResponse>' % inner


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.globals = {
            'system.api_version': 26,
            'system.client_version': '0.16.22',
            'system.library_version': '0.16.22',
            'system.hostname': 'fakehost',
            'system.pid': 4242,
            'system.cwd': '/fake/cwd',
            'session.path': '/fake/session',
            'throttle.global_down.rate': 0,
            'throttle.global_up.rate': 0,
            'throttle.global_down.max_rate': 1024,
            'throttle.global_up.max_rate': 1024,
            'network.max_open_files': 4096,
            'throttle.max_downloads': 50,
            'throttle.max_uploads': 50,
            'network.http.max_total_connections': 32,
            'system.sockets.size': 3,
            'system.sockets.max_size': 1048576,
            'throttle.unchoked_uploads': 0,
            'throttle.unchoked_downloads': 0,
            'network.listen.port': 22400,
            'network.listen.port.range': '22400-22400',
            'network.http.current_open': 0,
            'network.total_handshakes': 0,
            'network.open_files': 0,
            'throttle.max_unchoked_uploads': 2,
            'throttle.max_unchoked_downloads': 2,
            'throttle.max_uploads.global': 0,
            'throttle.max_downloads.global': 0,
            'throttle.min_peers.normal': 100,
            'throttle.max_peers.normal': 200,
            'throttle.min_peers.seed': -1,
            'throttle.max_peers.seed': -1,
        }
        # keyed by info hash; field names are rtorrent command names
        self.torrents = {}
        self.trackers = {}
        self.peers = {}
        self.files = {}
        # methods seen with the UNTRUSTED_CONNECTION=1 header (fake.was_untrusted)
        self.untrusted_methods = set()
        # when set (fake.fail_next_set), the next setter call returns a fault
        self.fail_next_set = False
        # M6/M7 registries the fake records so behaviour is observable in tests
        self.throttle_groups = {}  # name -> {'up': bytes, 'down': bytes}
        self.views = set()
        self.schedules = {}        # name -> command string
        self._added_counter = 0

    def add_torrent(self, hash, name, size, complete=0, is_open=1, is_active=1,
                    down_rate=0, up_rate=0, trackers=None, has_active_not_scrape=0,
                    directory='/home/seed/downloads', files=None):
        with self.lock:
            self.torrents[hash] = {
                'd.hash': hash, 'd.name': name, 'd.size_bytes': size,
                'd.bytes_done': size if complete else size // 2,
                'd.complete': complete, 'd.up.rate': up_rate, 'd.down.rate': down_rate,
                'd.up.total': 0, 'd.down.total': 0, 'd.ratio': 0,
                'd.size_files': 1, 'd.tracker_size': len(trackers or []),
                'd.peers_connected': 0, 'd.tied_to_file': '/fake/%s.torrent' % name,
                'd.ignore_commands': 0, 'd.is_open': is_open, 'd.is_active': is_active,
                'd.hashing': 0, 'd.is_hash_checking': 0, 'd.chunks_hashed': 0,
                'd.message': '', 'd.size_chunks': 100,
                'd.completed_chunks': 100 if complete else 50,
                'd.tracker.has_active_not_scrape': has_active_not_scrape,
                # per-torrent tuning + label (M3 reads) and directory (M7 move data)
                'd.custom1': '', 'd.priority': 2, 'd.uploads_max': 0,
                'd.downloads_max': 0, 'd.peers_max': 0, 'd.throttle_name': '',
                'd.directory': directory,
            }
            self.trackers[hash] = trackers or []
            self.peers[hash] = []
            self.files[hash] = files if files is not None else [{
                'f.size_chunks': 100, 'f.completed_chunks': 100 if complete else 50,
                'f.priority': 1, 'f.size_bytes': size, 'f.path': name,
            }]

    def add_next_torrent(self, payload, raw, start, trailing):
        # simulate load.start/load.raw_start: synthesize a torrent from the magnet's
        # dn= (or a placeholder) plus any trailing d.directory.set= / d.custom1.set=
        with self.lock:
            self._added_counter += 1
            n = self._added_counter
        name = None
        if not raw and 'magnet:' in str(payload).lower():
            for k, v in urllib.parse.parse_qsl(urllib.parse.urlparse(payload).query):
                if k == 'dn':
                    name = v
        elif raw:
            # extract the torrent name from the bencoded info dict (4:name<len>:<name>)
            try:
                import base64 as _b64
                blob = _b64.b64decode(payload)
                m = __import__('re').search(rb'4:name(\d+):', blob)
                if m:
                    start = m.end()
                    length = int(m.group(1))
                    name = blob[start:start + length].decode('utf-8', 'replace')
            except Exception:
                name = None
        if not name:
            name = 'added-%d.iso' % n
        directory, label = '/home/seed/downloads', ''
        for cmd in trailing:
            if isinstance(cmd, str) and cmd.startswith('d.directory.set='):
                directory = cmd[len('d.directory.set='):]
            elif isinstance(cmd, str) and cmd.startswith('d.custom1.set='):
                label = cmd[len('d.custom1.set='):]
        hash = ('%040X' % (n * 0x1111111111))[:40]
        self.add_torrent(hash, name, 700 * 1024 * 1024, complete=0,
                         is_open=1 if start else 0, is_active=1 if start else 0,
                         directory=directory)
        if label:
            with self.lock:
                self.torrents[hash]['d.custom1'] = label
        return hash

    def view_hashes(self, view):
        with self.lock:
            items = list(self.torrents.items())
        if view in ('main', 'default', ''):
            return [h for h, _ in items]
        if view == 'name':
            return [h for h, _ in sorted(items, key=lambda kv: kv[1]['d.name'].lower())]
        preds = {
            'started': lambda t: t['d.is_open'] == 1,
            'stopped': lambda t: t['d.is_open'] == 0,
            'complete': lambda t: t['d.complete'] == 1,
            'incomplete': lambda t: t['d.complete'] == 0,
            'hashing': lambda t: t['d.is_hash_checking'] == 1,
            'seeding': lambda t: t['d.complete'] == 1 and t['d.is_open'] == 1,
            'leeching': lambda t: t['d.complete'] == 0 and t['d.is_open'] == 1,
            'active': lambda t: (t['d.up.rate'] + t['d.down.rate']) > 0,
        }
        if view in preds:
            return [h for h, t in items if preds[view](t)]
        return []

    def populate_default(self):
        self.add_torrent('A' * 40, 'alpha.iso', 1000000, complete=1, is_open=1, is_active=1,
                         trackers=[{'t.group': 0, 't.url': 'udp://tr.example.org:6969/announce',
                                    't.is_busy': 0, 't.latest_event': 1, 't.id': 'tid', 't.failed_counter': 0,
                                    't.success_counter': 5, 't.scrape_counter': 2, 't.is_usable': 1,
                                    't.is_enabled': 1, 't.scrape_complete': 10, 't.scrape_incomplete': 2,
                                    't.scrape_downloaded': 100, 't.latest_new_peers': 3, 't.latest_sum_peers': 8}])
        self.add_torrent('B' * 40, 'bravo.iso', 2000000, complete=0, is_open=0, is_active=0,
                         has_active_not_scrape=1,
                         trackers=[{'t.group': 0, 't.url': 'http://tr2.example.org:8080/announce',
                                    't.is_busy': 1, 't.latest_event': 1, 't.id': 'xyz', 't.failed_counter': 1,
                                    't.success_counter': 3, 't.scrape_counter': 1, 't.is_usable': 1,
                                    't.is_enabled': 1, 't.scrape_complete': 4, 't.scrape_incomplete': 1,
                                    't.scrape_downloaded': 40, 't.latest_new_peers': 2, 't.latest_sum_peers': 5}])


def _parse_value(v):
    if v is None:
        return None
    for child in v:
        tag = child.tag
        text = child.text or ''
        if tag in ('i4', 'i8', 'int'):
            return int(text)
        if tag in ('string', 'base64'):
            return text
        if tag == 'array':
            data = child.find('data')
            return [_parse_value(x) for x in data.findall('value')] if data is not None else []
        if tag == 'struct':
            d = {}
            for m in child.findall('member'):
                d[m.find('name').text] = _parse_value(m.find('value'))
            return d
    return v.text or ''


def _parse_params(params_el):
    if params_el is None:
        return []
    return [_parse_value(p.find('value')) for p in params_el.findall('param')]


def _split_index(target, type_char):
    # "<hash>:f3" -> ("<hash>", 3); returns (hash, None) if malformed
    marker = ':' + type_char
    hash, sep, idx = target.partition(marker)
    if not sep:
        return target, None
    try:
        return hash, int(idx)
    except ValueError:
        return hash, None


def _split_peer(target):
    hash, sep, pid = target.partition(':p')
    return (hash, pid) if sep else (target, None)


def _new_tracker(group, url):
    return {'t.group': group, 't.url': url, 't.is_busy': 0, 't.latest_event': 0,
            't.id': 'ins', 't.failed_counter': 0, 't.success_counter': 0,
            't.scrape_counter': 0, 't.is_usable': 1, 't.is_enabled': 1,
            't.scrape_complete': 0, 't.scrape_incomplete': 0, 't.scrape_downloaded': 0,
            't.latest_new_peers': 0, 't.latest_sum_peers': 0}


class Responder:
    def __init__(self, state):
        self.state = state

    def value(self, v):
        if isinstance(v, str):
            return '<value><string>%s</string></value>' % escape(v)
        return '<value><i8>%d</i8></value>' % v

    def array(self, values_xml):
        if not values_xml:
            return '<value><array><data/></array></value>'
        return '<value><array><data>%s</data></array></value>' % ''.join(values_xml)

    def row(self, fields, commands):
        vals = []
        for c in commands:
            base = c[:-1] if c.endswith('=') else c
            vals.append(self.value(fields[base]))
        return self.array(vals)

    def handle(self, body, untrusted=False):
        state = self.state
        root = fromstring(body)
        method = root.find('methodName').text
        params = _parse_params(root.find('params'))

        if method in DEPRECATED_COMMANDS:
            return _fault(-506, "Method '%s' not defined" % method)

        if untrusted:
            with state.lock:
                state.untrusted_methods.add(method)
            if method not in UNTRUSTED_SAFE:
                # message shape matches rtorrent's untrusted_error
                return _fault(-501, 'Command "%s" is not allowed for untrusted connections.' % method)

        if method == 'system.multicall':
            results = []
            for call in params[0]:
                name = call['methodName']
                with state.lock:
                    if name in state.globals and name not in DEPRECATED_COMMANDS:
                        results.append(self.array([self.value(state.globals[name])]))
                    else:
                        results.append(_fault_struct(-506, "Method '%s' not defined" % name))
            return _response(self.array(results))

        if method == 'd.multicall':
            view = params[1] if len(params) > 1 else 'main'
            commands = params[2:]
            with state.lock:
                rows = [self.row(state.torrents[h], commands) for h in state.view_hashes(view)]
            return _response(self.array(rows))

        if method in ('t.multicall', 'p.multicall', 'f.multicall'):
            hash = params[0]
            commands = params[2:]
            source = {'t.multicall': state.trackers, 'p.multicall': state.peers,
                      'f.multicall': state.files}[method]
            with state.lock:
                if hash not in source:
                    return _fault(-501, 'Could not find info-hash.')
                rows = [self.row(item, commands) for item in source[hash]]
            return _response(self.array(rows))

        if method == 'fake.add_torrent':
            state.add_torrent(params[0], params[1], params[2],
                              is_open=int(params[3]) if len(params) > 3 else 1)
            return _response(self.value(0))

        if method == 'fake.remove_torrent':
            with state.lock:
                state.torrents.pop(params[0], None)
            return _response(self.value(0))

        if method == 'fake.fail_next_set':
            with state.lock:
                state.fail_next_set = True
            return _response(self.value(0))

        if method == 'fake.was_untrusted':
            with state.lock:
                return _response(self.value(1 if params[0] in state.untrusted_methods else 0))

        # ---- per-torrent actions (M1): target = info hash ----
        if method in ('d.start', 'd.open', 'd.resume', 'd.stop', 'd.close',
                      'd.pause', 'd.check_hash', 'd.tracker_announce', 'd.erase'):
            hash = params[0] if params else ''
            with state.lock:
                if hash not in state.torrents:
                    return _fault(-501, 'Could not find info-hash.')
                t = state.torrents[hash]
                if method in ('d.start', 'd.open', 'd.resume'):
                    t['d.is_open'], t['d.is_active'] = 1, 1
                elif method in ('d.stop', 'd.close'):
                    t['d.is_open'], t['d.is_active'] = 0, 0
                elif method == 'd.pause':
                    t['d.is_active'] = 0
                elif method == 'd.check_hash':
                    t['d.is_hash_checking'] = 1
                elif method == 'd.erase':
                    state.torrents.pop(hash, None)
                    state.files.pop(hash, None)
                    state.trackers.pop(hash, None)
                    state.peers.pop(hash, None)
            return _response(self.value(0))

        # ---- file priority (M2): target = <hash>:f<index> ----
        if method == 'f.priority.set':
            hash, idx = _split_index(params[0], 'f')
            with state.lock:
                files = state.files.get(hash)
                if files is None or idx is None or idx >= len(files):
                    return _fault(-501, 'invalid file target.')
                files[idx]['f.priority'] = params[1]
            return _response(self.value(params[1]))

        # ---- tracker enable (M2): target = <hash>:t<index> ----
        if method == 't.is_enabled.set':
            hash, idx = _split_index(params[0], 't')
            with state.lock:
                trackers = state.trackers.get(hash)
                if trackers is None or idx is None or idx >= len(trackers):
                    return _fault(-501, 'invalid tracker target.')
                trackers[idx]['t.is_enabled'] = params[1]
            return _response(self.value(params[1]))

        # ---- peer actions (M3): target = <hash>:p<40-hex peer id> ----
        if method in ('p.banned.set', 'p.snubbed.set', 'p.disconnect'):
            hash, peer_id = _split_peer(params[0])
            with state.lock:
                peers = state.peers.get(hash)
                if peers is None or peer_id is None:
                    return _fault(-501, 'invalid peer target.')
                peer = next((p for p in peers if p.get('p.id') == peer_id), None)
                if peer is None:
                    return _fault(-501, 'peer not found.')
                if method == 'p.disconnect':
                    peers.remove(peer)
                elif method == 'p.banned.set':
                    peer['p.banned'] = params[1]
                else:
                    peer['p.snubbed'] = params[1]
            return _response(self.value(0))

        # ---- add torrent (M2/M5): load.* target '' then URI/raw + trailing cmds ----
        if method.startswith('load.'):
            raw = 'raw' in method
            start = 'start' in method
            payload = params[1] if len(params) > 1 else ''
            trailing = params[2:]
            state.add_next_torrent(payload, raw, start, trailing)
            return _response(self.value(0))

        # ---- add tracker (M5): d.tracker.insert(hash, group, url) ----
        if method == 'd.tracker.insert':
            hash = params[0]
            url = params[-1]
            with state.lock:
                if hash not in state.trackers:
                    return _fault(-501, 'Could not find info-hash.')
                state.trackers[hash].append(_new_tracker(int(params[1]), url))
                state.torrents[hash]['d.tracker_size'] = len(state.trackers[hash])
            return _response(self.value(0))

        # ---- named throttle groups (M6) ----
        if method in ('throttle.up', 'throttle.down'):
            name, rate_kb = params[1], params[2]
            with state.lock:
                grp = state.throttle_groups.setdefault(name, {'up': 0, 'down': 0})
                grp['up' if method == 'throttle.up' else 'down'] = rate_kb * 1024
            return _response(self.value(0))
        if method in ('throttle.up.max', 'throttle.down.max'):
            # CMD2_ANY_STRING: the group name is the target (first) param
            name = params[0] if params else ''
            with state.lock:
                grp = state.throttle_groups.get(name)
            key = 'up' if method == 'throttle.up.max' else 'down'
            return _response(self.value(grp[key] if grp else -1))

        # ---- custom views (M7) and scheduling (M6): accept and record ----
        if method in ('view.add', 'view.filter', 'view.filter_on', 'view.sort_new'):
            with state.lock:
                state.views.add(params[1] if method != 'view.add' else params[1])
            return _response(self.value(0))
        if method in ('schedule', 'schedule.remove', 'schedule.if_absent'):
            with state.lock:
                if method == 'schedule.remove':
                    state.schedules.pop(params[1] if len(params) > 1 else '', None)
                else:
                    state.schedules[params[1]] = params[4] if len(params) > 4 else ''
            return _response(self.value(0))

        if method.endswith('.set_kb'):
            # rate setters take KB and store bytes (CMD2_ANY_VALUE_KB)
            base = method[:-len('.set_kb')]
            with state.lock:
                if state.fail_next_set:
                    state.fail_next_set = False
                    return _fault(-501, 'injected fault')
                state.globals[base] = params[-1] * 1024
            return _response(self.value(0))

        if method.endswith('.set'):
            base = method[:-4]
            if base.startswith('d.'):
                hash, value = params[0], params[1]
                with state.lock:
                    if hash not in state.torrents:
                        return _fault(-501, 'Could not find info-hash.')
                    state.torrents[hash][base] = value
                return _response(self.value(value))
            with state.lock:
                if state.fail_next_set:
                    state.fail_next_set = False
                    return _fault(-501, 'injected fault')
                state.globals[base] = params[-1]
            return _response(self.value(0))

        if method.startswith('d.'):
            with state.lock:
                if params and params[0] in state.torrents:
                    return _response(self.value(state.torrents[params[0]][method]))
            return _fault(-501, 'Could not find info-hash.')

        with state.lock:
            if method in state.globals:
                return _response(self.value(state.globals[method]))
        return _fault(-506, "Method '%s' not defined" % method)


class ScgiHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data = b''
        while b':' not in data:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            data += chunk
        head_len_s, rest = data.split(b':', 1)
        head_len = int(head_len_s)
        while len(rest) < head_len + 1:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            rest += chunk
        headers_raw, rest = rest[:head_len], rest[head_len + 1:]
        parts = headers_raw.split(b'\x00')
        headers = dict(zip([p.decode() for p in parts[0::2]], [p.decode() for p in parts[1::2]]))
        content_length = int(headers.get('CONTENT_LENGTH', 0))
        while len(rest) < content_length:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            rest += chunk
        body = rest[:content_length].decode('utf8')
        untrusted = headers.get('UNTRUSTED_CONNECTION') == '1'
        try:
            resp = self.server.responder.handle(body, untrusted)
        except Exception as e:
            resp = _fault(-500, 'fake rtorrent error: %r' % e)
        payload = resp.encode('utf8')
        head = b'Content-Type: text/xml\r\nContent-Length: %d\r\n\r\n' % len(payload)
        self.request.sendall(head + payload)
        self.request.close()


class FakeRtorrent:
    def __init__(self, sock_path, state=None):
        self.sock_path = sock_path
        self.state = state if state is not None else State()
        if os.path.exists(sock_path):
            os.remove(sock_path)
        self.server = socketserver.ThreadingUnixStreamServer(sock_path, ScgiHandler)
        self.server.responder = Responder(self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        if os.path.exists(self.sock_path):
            os.remove(self.sock_path)
