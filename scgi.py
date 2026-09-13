import errno
import os
import socket

from utils import WHERE_RTORRENT, WHERE_RTREMOTE

# per-socket-operation timeout; guards against a hung rtorrent blocking forever
RTR_SCGI_TIMEOUT = int(os.getenv('RTR_SCGI_TIMEOUT', 30))


class ScgiError(Exception):
    """The SCGI transport to rtorrent failed (rtorrent unreachable, hung, or
    inaccessible). Carries `where` (component to blame) and `hint` (what the
    user should check) for the diagnostic log."""

    def __init__(self, message, hint, where=WHERE_RTORRENT):
        super().__init__(message)
        self.hint = hint
        self.where = where


def classify_socket_error(e, host_port):
    # map a socket-level failure to a user-readable diagnosis
    if isinstance(e, socket.timeout):
        return ScgiError('rtorrent did not answer within RTR_SCGI_TIMEOUT=%ds' % RTR_SCGI_TIMEOUT,
                         'rtorrent is hung or overloaded (hash checking a large torrent, disk stalled); '
                         'check its own log and load')
    if isinstance(e, FileNotFoundError):
        return ScgiError('rtorrent SCGI socket not found at %s' % host_port,
                         'rtorrent is not running, or RTR_SCGI_SOCKET_PATH is not the path in its '
                         'network.scgi.open_local setting')
    if isinstance(e, PermissionError):
        return ScgiError('permission denied on rtorrent SCGI socket %s' % host_port,
                         'the user running rtremote cannot access the socket file; fix its permissions '
                         'or run both under the same user', WHERE_RTREMOTE)
    if isinstance(e, ConnectionRefusedError):
        return ScgiError('connection refused on rtorrent SCGI socket %s' % host_port,
                         'rtorrent is not listening there: it is down, still starting, or a stale socket '
                         'file is left over from a previous run (delete it and restart rtorrent)')
    if isinstance(e, (ConnectionResetError, BrokenPipeError)) or getattr(e, 'errno', None) in (errno.EPIPE,):
        return ScgiError('rtorrent closed the SCGI connection mid-request',
                         'rtorrent restarted or crashed while answering; check its own log')
    if isinstance(e, socket.gaierror):
        return ScgiError('cannot resolve rtorrent SCGI host in %s' % host_port,
                         'RTR_SCGI_SOCKET_PATH uses inet:host:port with an unresolvable host', WHERE_RTREMOTE)
    return ScgiError('rtorrent SCGI transport error on %s: %s' % (host_port, e),
                     'rtorrent is unreachable; check that it runs and that RTR_SCGI_SOCKET_PATH is correct')


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
        except OSError as e:
            sock.close()
            raise classify_socket_error(e, self.host_port) from e
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
        except OSError as e:
            raise classify_socket_error(e, self.host_port) from e
        finally:
            sock.close()
        return b''.join(chunks).decode(Scgi.ENCODING, errors='replace')
