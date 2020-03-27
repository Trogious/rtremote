import os

from remote import Remote

SOCK_PATH = './.rtorrent.sock'


def test_api_version():
    sock_path = os.getenv('RTR_SCGI_SOCKET_PATH', SOCK_PATH)
    remote = Remote(sock_path)
    g = remote.get_global()
    assert g.system_client_version == os.getenv('RTR_SYSTEM_CLIENT_VERSION', '0.0.0')
    assert g.system_api_version == int(os.getenv('RTR_SYSTEM_API_VERSION', 0))
