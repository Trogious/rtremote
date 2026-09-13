from xml.etree.ElementTree import fromstring
from xml.sax.saxutils import escape

from xmljson import parker

from model import File, Global, Peer, Torrent, Tracker
from scgi import Scgi


class RpcError(Exception):
    """rtorrent returned a fault, or the response could not be parsed."""


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

    def _post(self, body, extra_headers=None):
        scgi = Scgi(self.host_port)
        resp = scgi.post(body, extra_headers)
        start = resp.find('<')
        if start < 0:
            raise RpcError('no XML found in SCGI response')
        data = parker.data(fromstring(resp[start:]), preserve_root=True)  # convert to json
        RTorrentRpc._check_fault(data)
        return data

    @staticmethod
    def _check_fault(data):
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
            raise RpcError('rtorrent fault %s: %s' % (code, string))

    def call(self, method, params=None, extra_headers=None):
        body = "<?xml version='1.0'?><methodCall><methodName>" + \
            escape(method) + "</methodName><params>"
        if params:
            for p in params:
                body += RTorrentRpc._param_xml(p)
        body += "</params></methodCall>"
        return self._post(body, extra_headers)

    def set_value(self, command, value, untrusted=False):
        # untrusted=True sends rtorrent's UNTRUSTED_CONNECTION=1 SCGI header, so
        # rtorrent's own untrusted-safe allowlist applies as defence in depth;
        # only usable for commands rtorrent marks safe (rpc.mark_safe)
        headers = [('UNTRUSTED_CONNECTION', 1)] if untrusted else None
        return self.call(command, [('string', ''), ('i8', int(value))], headers)

    def target_command(self, command, target, args=None, untrusted=False):
        # generic command whose first XML-RPC param is the target: '' for a
        # global command, '<hash>' for a download, '<hash>:f<i>'/':t<i>'/':p<id>'
        # for a file/tracker/peer (see rtorrent object_to_target). args is a list
        # of (xml-type, value) tuples appended after the target.
        params = [('string', target)]
        if args:
            params.extend(args)
        headers = [('UNTRUSTED_CONNECTION', 1)] if untrusted else None
        return self.call(command, params, headers)

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
        return self._post(body)

    def list_methods(self, params=None):
        data = self.call('system.listMethods', params)
        methods = []
        for d in data['methodResponse']['params']['param']['value']['array']['data']['value']:
            methods.append(d['string'])
        return methods

    def multicall(self, method, args):
        data = self.call(method, RTorrentRpc.string_params(args))
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

    def f_multicall(self, hash, commands):
        data = self.multicall('f.multicall', [hash, ''] + commands)
        return File.get_files(data, commands)

    def global_data(self, commands):
        g = Global()
        data = self.system_multicall(commands)
        try:
            g.add_attributes(data, commands)
        except ValueError as e:
            raise RpcError(str(e))
        return g
