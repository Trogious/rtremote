import socket

import pynetstring


class Scgi:
    BUFSIZE = 4096
    ENCODING = 'utf8'

    def __init__(self, host_port):
        self.host_port = host_port

    @staticmethod
    def get_header(name, val):
        return name.encode(Scgi.ENCODING) + bytes(1) + str(val).encode(Scgi.ENCODING) + bytes(1)

    @staticmethod
    def get_headers(content_len, method='POST'):
        h = Scgi.get_header('CONTENT_LENGTH', content_len)
        h += Scgi.get_header('Scgi', 1)
        h += Scgi.get_header('REQUEST_METHOD', method)
        h += Scgi.get_header('REQUEST_URI', '/RPC2')
        h = pynetstring.encode(h)
        return h

    def get_connected_socket(self):
        if self.host_port.startswith('inet:'):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            host_port = self.host_port.replace('inet:', '').split(':')
            sock.connect(host_port)
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(self.host_port.replace('unix:', '').replace('local:', ''))
        return sock

    def post(self, body):
        req = Scgi.get_headers(len(body)) + body.encode(Scgi.ENCODING)
        sock = self.get_connected_socket()
        sock.sendall(req)
        r = sock.recv(Scgi.BUFSIZE)
        resp = b''
        while len(r) > 0:
            resp += r
            r = sock.recv(Scgi.BUFSIZE)
        return resp.decode(Scgi.ENCODING)
