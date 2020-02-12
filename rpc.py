from xml.etree.ElementTree import fromstring

from xmljson import parker

from model import File, Global, Peer, Torrent, Tracker
from scgi import Scgi


class RTorrentRpc:
    def __init__(self, host_port):
        self.host_port = host_port

    @staticmethod
    def extract_params(args):
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

    def call(self, method, params=None):
        body = "<?xml version='1.0'?><methodCall><methodName>" + \
            method + "</methodName><params>"
        if params:
            for p in params:
                body += '<param><value><' + p[0] + '>' + str(p[1]) + '</' + p[0] + '></value></param>'
        body += "</params></methodCall>"
        scgi = Scgi(self.host_port)
        resp = scgi.post(body)
        resp = resp[resp.find('<'):]  # strip headers
        # print(resp)
        data = parker.data(fromstring(resp), preserve_root=True)  # convert to json
        return data

    def get_struct(self, command):
        s = '<struct><member><name>methodName</name><value><string>' + command + \
            '</string></value></member><member><name>params</name><value><array><data></data></array></value></member></struct>'
        return s

    def system_multicall(self, commands):
        body = "<?xml version='1.0'?><methodCall><methodName>system.multicall</methodName><params><param><value><array><data>"
        if commands:
            for c in commands:
                body += '<value>' + self.get_struct(c) + '</value>'
        body += "</data></array></value></param></params></methodCall>"
        scgi = Scgi(self.host_port)
        resp = scgi.post(body)
        resp = resp[resp.find('<'):]  # strip headers
        # print(resp)
        data = parker.data(fromstring(resp), preserve_root=True)  # convert to json
        return data

    def list_methods(self, params=None):
        data = self.call('system.listMethods', params)
        methods = []
        for d in data['methodResponse']['params']['param']['value']['array']['data']['value']:
            methods.append(d['string'])
        return methods

    def multicall(self, method, args):
        data = self.call(method, RTorrentRpc.extract_params(args))
        data = data['methodResponse']['params']['param']['value']['array']['data']  # TODO: add 'fault' handling
        if 'value' in data:
            data = data['value']
            if 'array' in data:
                data = [data]
        else:
            data = []
        return data

    def d_multicall(self, commands, view):
        data = self.multicall('d.multicall2', ['', view] + commands)
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
        g.add_attributes(data, commands)
        return g
