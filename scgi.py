import os
import socket

# per-socket-operation timeout; guards against a hung rtorrent blocking forever
RTR_SCGI_TIMEOUT = int(os.getenv('RTR_SCGI_TIMEOUT', 30))


def netstring(data):
    return str(len(data)).encode('ascii') + b':' + data + b','


class Scgi:
    BUFSIZE = 4096
    ENCODING = 'utf8'

    def __init__(self, host_port):
        self.host_port = host_port

    @staticmethod
    def get_header(name, val):
        return name.encode(Scgi.ENCODING) + bytes(1) + str(val).encode(Scgi.ENCODING) + bytes(1)

    @staticmethod
    def get_headers(content_len, method='POST', extra_headers=None):
        # CONTENT_LENGTH must be the first header: rtorrent rejects the request otherwise
        h = Scgi.get_header('CONTENT_LENGTH', content_len)
        h += Scgi.get_header('SCGI', 1)
        h += Scgi.get_header('REQUEST_METHOD', method)
        h += Scgi.get_header('REQUEST_URI', '/RPC2')
        if extra_headers:
            for name, value in extra_headers:
                h += Scgi.get_header(name, value)
        return netstring(h)

    def get_connected_socket(self):
        if self.host_port.startswith('inet:'):
            host, _, port = self.host_port[len('inet:'):].rpartition(':')
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            addr = (host, int(port))
        else:
            path = self.host_port
            for prefix in ('unix:', 'local:'):
                if path.startswith(prefix):
                    path = path[len(prefix):]
                    break
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            addr = path
        sock.settimeout(RTR_SCGI_TIMEOUT)
        try:
            sock.connect(addr)
        except BaseException:
            sock.close()
            raise
        return sock

    def post(self, body, extra_headers=None):
        payload = body.encode(Scgi.ENCODING)
        req = Scgi.get_headers(len(payload), extra_headers=extra_headers) + payload
        sock = self.get_connected_socket()
        try:
            sock.sendall(req)
            chunks = []
            while True:
                r = sock.recv(Scgi.BUFSIZE)
                if not r:
                    break
                chunks.append(r)
        finally:
            sock.close()
        return b''.join(chunks).decode(Scgi.ENCODING)
