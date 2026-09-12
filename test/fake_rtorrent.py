"""A fake rtorrent SCGI/XML-RPC server for testing rtremote without rtorrent.

Speaks just enough of rtorrent's XML-RPC dialect for server_wss.py:
- system.multicall (global getters)
- d.multicall2 / t.multicall / p.multicall / f.multicall
- plain getters and *.set setters (for update-propagation tests)
- fake.add_torrent / fake.remove_torrent control methods

Response XML mimics both real formatting styles, selected per instance:
- pretty=True: xmlrpc-c style (rtorrent 0.9.x) - whitespace inside empty <data>
- pretty=False: tinyxml2 compact style (rtorrent >= 0.10) - <data/>
"""
import os
import socketserver
import threading
from xml.etree.ElementTree import fromstring
from xml.sax.saxutils import escape


def _fault(code, string):
    return ('<?xml version="1.0"?><methodResponse><fault><value><struct>'
            '<member><name>faultCode</name><value><i4>%d</i4></value></member>'
            '<member><name>faultString</name><value><string>%s</string></value></member>'
            '</struct></value></fault></methodResponse>' % (code, escape(string)))


def _response(inner):
    return '<?xml version="1.0"?><methodResponse><params><param>%s</param></params></methodResponse>' % inner


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.globals = {
            'system.api_version': 10,
            'system.client_version': '0.9.8',
            'system.library_version': '0.13.8',
            'system.hostname': 'fakehost',
            'system.pid': 4242,
            'system.cwd': '/fake/cwd',
            'session.path': '/fake/session',
            'throttle.global_down.rate': 0,
            'throttle.global_up.rate': 0,
            'throttle.global_down.max_rate': 1024,
            'throttle.global_up.max_rate': 1024,
            'network.max_open_files': 8000,
            'throttle.max_downloads': 50,
            'throttle.max_uploads': 50,
            'network.http.max_open': 32,
            'network.open_sockets': 3,
            'network.max_open_sockets': 999,
            'throttle.unchoked_uploads': 0,
            'throttle.unchoked_downloads': 0,
            'network.listen.port': 22400,
            'network.port_range': '22400-22400',
            'network.http.current_open': 0,
        }
        # keyed by info hash; field names are rtorrent command names
        self.torrents = {}
        self.trackers = {}
        self.peers = {}
        self.files = {}

    def add_torrent(self, hash, name, size, complete=0, is_open=1, is_active=1,
                    down_rate=0, up_rate=0, trackers=None):
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
                'd.has_active_not_scrape': 0,
            }
            self.trackers[hash] = trackers or []
            self.peers[hash] = []
            self.files[hash] = [{
                'f.size_chunks': 100, 'f.completed_chunks': 100 if complete else 50,
                'f.priority': 1, 'f.size_bytes': size, 'f.path': name,
            }]

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
        if tag == 'string':
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


class Responder:
    def __init__(self, state, pretty=True):
        self.state = state
        self.pretty = pretty

    def value(self, v):
        if isinstance(v, str):
            return '<value><string>%s</string></value>' % escape(v)
        return '<value><i8>%d</i8></value>' % v

    def array(self, values_xml):
        if not values_xml:
            return ('<value><array><data>\n</data></array></value>' if self.pretty
                    else '<value><array><data/></array></value>')
        return '<value><array><data>%s</data></array></value>' % ''.join(values_xml)

    def row(self, fields, commands):
        vals = []
        for c in commands:
            base = c[:-1] if c.endswith('=') else c
            vals.append(self.value(fields[base]))
        return self.array(vals)

    def handle(self, body):
        state = self.state
        root = fromstring(body)
        method = root.find('methodName').text
        params = _parse_params(root.find('params'))

        if method == 'system.multicall':
            results = []
            for call in params[0]:
                name = call['methodName']
                with state.lock:
                    results.append(self.array([self.value(state.globals.get(name, 0))]))
            return _response(self.array(results))

        if method == 'd.multicall2':
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
        try:
            resp = self.server.responder.handle(body)
        except Exception as e:
            resp = _fault(-500, 'fake rtorrent error: %r' % e)
        payload = resp.encode('utf8')
        head = b'Content-Type: text/xml\r\nContent-Length: %d\r\n\r\n' % len(payload)
        self.request.sendall(head + payload)
        self.request.close()


class FakeRtorrent:
    def __init__(self, sock_path, state=None, pretty=True):
        self.sock_path = sock_path
        self.state = state if state is not None else State()
        if os.path.exists(sock_path):
            os.remove(sock_path)
        self.server = socketserver.ThreadingUnixStreamServer(sock_path, ScgiHandler)
        self.server.responder = Responder(self.state, pretty)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        if os.path.exists(self.sock_path):
            os.remove(self.sock_path)
