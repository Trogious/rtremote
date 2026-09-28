from xml.etree.ElementTree import ParseError, fromstring
from xml.sax.saxutils import escape

from xmljson import Parker, parker

from model import File, Global, Peer, Torrent, Tracker
from scgi import Scgi
from utils import WHERE_APP, WHERE_RTORRENT, WHERE_RTREMOTE

# the default parker coerces leaf text: a file named "2024" comes back as an
# int and "1e5" as a float. Anything that ends up as a filesystem path is
# parsed with this instance instead, which leaves every value a string.
raw_parker = Parker(xml_fromstring=False)


class RpcError(Exception):
    """rtorrent returned a fault, or the response could not be parsed.

    `where` names the component to blame and `hint` what to check; `code` is
    the XML-RPC fault code (None for a parse failure)."""

    def __init__(self, message, hint=None, where=WHERE_RTORRENT, code=None, command=None):
        super().__init__(message)
        self.hint = hint
        self.where = where
        self.code = code
        self.command = command


def classify_fault(code, string, command):
    # rtorrent fault codes seen in practice; the text is the reliable part.
    # A stale target is "invalid parameters: info-hash not found" (-500) in
    # rtorrent 0.16 (rpc/rpc_manager.cc) and was "Could not find info-hash."
    # (-501) in 0.9; a stale file/tracker index is "invalid parameters: index
    # not found" and a peer that left "invalid parameters: hash not found"
    # (the lookup slots in main.cc)
    text = str(string or '')
    label = ' (%s)' % command if command else ''
    if 'info-hash not found' in text or 'Could not find info-hash' in text:
        return RpcError('rtorrent has no torrent with that hash%s' % label,
                        'the app acted on a torrent rtorrent no longer has; its list refreshes on the next push',
                        WHERE_APP, code, command)
    if 'index not found' in text or 'hash not found' in text:
        return RpcError('rtorrent has no such file, tracker or peer on that torrent%s' % label,
                        'the app acted on a file, tracker or peer entry rtorrent no longer has; '
                        'the details screen refreshes on its next request', WHERE_APP, code, command)
    if 'not allowed for untrusted connections' in text:
        # the fault names the command rtorrent actually refused, which may be one
        # the requested command runs internally (system.file.allocate under
        # d.open, say), so keep it in the message
        return RpcError('rtorrent refused %s as untrusted: %s' % (command or 'the command', text),
                        'rtremote sent this command with UNTRUSTED_CONNECTION=1 but rtorrent does not allow it '
                        '(or a command it runs internally) on untrusted connections; this is an rtremote bug, '
                        'please report it', WHERE_RTREMOTE, code, command)
    if 'not defined' in text or 'Method' in text and 'unknown' in text.lower():
        return RpcError('rtorrent does not know the command %s' % (command or ''),
                        'this rtorrent is too old or built without the command; rtremote needs rtorrent >= 0.16',
                        WHERE_RTORRENT, code, command)
    return RpcError('rtorrent fault %s%s: %s' % (code, label, text),
                    'rtorrent rejected the request; the fault text above is its own explanation',
                    WHERE_RTORRENT, code, command)


def bad_response_error(detail, host_port):
    return RpcError('no XML-RPC response from %s (%s)' % (host_port, detail),
                    'something answered on the SCGI socket but not with XML-RPC: check that '
                    'RTR_SCGI_SOCKET_PATH is rtorrent\'s network.scgi.open_local socket and that rtorrent '
                    'was built with XML-RPC support')


class RTorrentRpc:
    def __init__(self, host_port):
        self.host_port = host_port

    @staticmethod
    def extract_params(args):
        # generic helper for ad-hoc calls (tests, tooling): guesses the XML-RPC
        # type from the value; plain digit strings become i8
        params = []
        for p in args:
            param = p.split(':')
            if len(param) < 2:
                if param[0].isnumeric():
                    params.append(('i8', int(param[0])))
                else:
                    params.append(('string', param[0]))
            elif param[0] == 'i4':
                params.append(('i4', int(param[1])))
            elif param[0] == 'int':
                params.append(('int', int(param[1])))
            else:
                params.append(('string', param[0]))
        return params

    @staticmethod
    def string_params(args):
        # multicall arguments (target hash, view, commands) are always strings;
        # never guess types for them, or a numeric-looking info hash breaks the call
        return [('string', a) for a in args]

    @staticmethod
    def _param_xml(p):
        tag, value = p
        if tag == 'string':
            value = escape(str(value))
        elif tag == 'base64':
            # value is already base64 text (safe alphabet); strip whitespace so the
            # element body is clean. Used for load.raw_start torrent payloads.
            value = ''.join(str(value).split())
        return '<param><value><%s>%s</%s></value></param>' % (tag, value, tag)

    def _post(self, body, extra_headers=None, command=None, raw=False):
        scgi = Scgi(self.host_port)
        resp = scgi.post(body, extra_headers)
        start = resp.find('<')
        if start < 0:
            raise bad_response_error('no XML in %d bytes' % len(resp), self.host_port)
        try:
            # raw: leaf text stays a string (paths), see raw_parker
            data = (raw_parker if raw else parker).data(fromstring(resp[start:]), preserve_root=True)
        except ParseError as e:
            raise bad_response_error('unparseable XML: %s' % e, self.host_port) from e
        RTorrentRpc._check_fault(data, command)
        return data

    @staticmethod
    def _check_fault(data, command=None):
        method_response = data.get('methodResponse') if isinstance(data, dict) else None
        if isinstance(method_response, dict) and 'fault' in method_response:
            code, string = None, None
            try:
                members = method_response['fault']['value']['struct']['member']
                for m in members:
                    if m['name'] == 'faultCode':
                        code = next(iter(m['value'].values()))
                    elif m['name'] == 'faultString':
                        string = next(iter(m['value'].values()))
            except Exception:
                pass
            raise classify_fault(code, string, command)

    def call(self, method, params=None, extra_headers=None, raw=False):
        body = "<?xml version='1.0'?><methodCall><methodName>" + \
            escape(method) + "</methodName><params>"
        if params:
            for p in params:
                body += RTorrentRpc._param_xml(p)
        body += "</params></methodCall>"
        return self._post(body, extra_headers, method, raw)

    def set_value(self, command, value, untrusted=False):
        # untrusted=True sends rtorrent's UNTRUSTED_CONNECTION=1 SCGI header, so
        # rtorrent's own untrusted-safe allowlist applies as defence in depth;
        # only usable for commands rtorrent marks safe (rpc.mark_safe) AND whose
        # implementation runs no unsafe command internally - the flag is checked
        # on nested calls too (see Remote.TORRENT_ACTIONS)
        headers = [('UNTRUSTED_CONNECTION', 1)] if untrusted else None
        return self.call(command, [('string', ''), ('i8', int(value))], headers)

    def target_command(self, command, target, args=None, untrusted=False, raw=False):
        # generic command whose first XML-RPC param is the target: '' for a
        # global command, '<hash>' for a download, '<hash>:f<i>'/':t<i>'/':p<id>'
        # for a file/tracker/peer (see rtorrent object_to_target). args is a list
        # of (xml-type, value) tuples appended after the target.
        params = [('string', target)]
        if args:
            params.extend(args)
        headers = [('UNTRUSTED_CONNECTION', 1)] if untrusted else None
        return self.call(command, params, headers, raw)

    def get_struct(self, command):
        s = '<struct><member><name>methodName</name><value><string>' + escape(command) + \
            '</string></value></member><member><name>params</name><value><array><data></data></array></value></member></struct>'
        return s

    def system_multicall(self, commands):
        body = "<?xml version='1.0'?><methodCall><methodName>system.multicall</methodName><params><param><value><array><data>"
        if commands:
            for c in commands:
                body += '<value>' + self.get_struct(c) + '</value>'
        body += "</data></array></value></param></params></methodCall>"
        return self._post(body, command='system.multicall')

    def list_methods(self, params=None):
        data = self.call('system.listMethods', params)
        methods = []
        for d in data['methodResponse']['params']['param']['value']['array']['data']['value']:
            methods.append(d['string'])
        return methods

    def multicall(self, method, args, raw=False):
        data = self.call(method, RTorrentRpc.string_params(args), raw=raw)
        data = data['methodResponse']['params']['param']['value']['array']['data']
        # an empty result is a childless <data> element: parker turns it into
        # None (compact XML, rtorrent >= 0.10) or a whitespace string (pretty-
        # printed XML, rtorrent 0.9.x)
        if not isinstance(data, dict) or 'value' not in data:
            return []
        data = data['value']
        if isinstance(data, dict):  # single row is not wrapped in a list
            data = [data]
        return data

    def d_multicall(self, commands, view):
        # d.multicall2 is a deprecated redirect in rtorrent 0.16; the wire shape
        # (target '', view, commands...) is identical for d.multicall
        data = self.multicall('d.multicall', ['', view] + commands)
        return Torrent.get_torrents(data, commands)

    def t_multicall(self, hash, commands):
        data = self.multicall('t.multicall', [hash, ''] + commands)
        return Tracker.get_trackers(data, commands)

    def p_multicall(self, hash, commands):
        data = self.multicall('p.multicall', [hash, ''] + commands)
        return Peer.get_peers(data, commands)

    def f_multicall(self, hash, commands, raw=False):
        data = self.multicall('f.multicall', [hash, ''] + commands, raw)
        return File.get_files(data, commands)

    def global_data(self, commands):
        g = Global()
        data = self.system_multicall(commands)
        try:
            g.add_attributes(data, commands)
        except ValueError as e:
            # model.add_attribute: a per-command fault struct inside system.multicall
            raise RpcError(str(e), 'this rtorrent lacks a command rtremote needs; rtremote requires '
                           'rtorrent >= 0.16 (system.api_version >= 26)', WHERE_RTORRENT,
                           command='system.multicall') from e
        return g
