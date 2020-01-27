import os

from remote import Remote

SOCK_PATH = os.getenv('RTR_SCGI_SOCKET_PATH', './.rtorrent.sock')


def test_global_data():
    g = Remote(SOCK_PATH).get_global()
    assert g.throttle_global_down_max_rate == 1024
    assert g.throttle_global_up_max_rate == 1024
    assert g.network_max_open_files == 8000
    assert g.network_port_range == '22400-22400'
    assert g.network_listen_port == 22400


def test_torrents():
    torrents = Remote(SOCK_PATH).get_torrents()
    assert len(torrents) >= 2
    for t in torrents:
        if t.hash == 'A6B69431743F085D96692A81C6282617C50243C4':
            assert t.name == 'debian-10.2.0-amd64-DVD-1.iso'
            assert t.size_bytes == 3918200832
            assert t.tracker_size == 2
            assert t.size_files == 1
        elif t.hash == '67DD1659106DCDDE0FEC4283D7B0C84B6C292675':
            assert t.name == '2ROrd80'
            assert t.size_bytes == 6964641792
            assert t.tracker_size == 6
            assert t.size_files == 1


def test_trackers():
    remote = Remote(SOCK_PATH)
    trackers = remote.get_trackers('A6B69431743F085D96692A81C6282617C50243C4')
    assert len(trackers) == 2
    for t in trackers:
        assert t.is_enabled == 1
    trackers = remote.get_trackers('67DD1659106DCDDE0FEC4283D7B0C84B6C292675')
    assert len(trackers) == 6
    for t in trackers:
        assert t.is_enabled == 1


def test_files():
    remote = Remote(SOCK_PATH)
    files = remote.get_files('A6B69431743F085D96692A81C6282617C50243C4')
    assert len(files) == 1
    assert files[0].path == 'debian-10.2.0-amd64-DVD-1.iso'
    assert files[0].size_bytes == 3918200832
    files = remote.get_files('67DD1659106DCDDE0FEC4283D7B0C84B6C292675')
    assert len(files) == 1
    assert files[0].path == '2ROrd80'
    assert files[0].size_bytes == 6964641792


def test_peers():
    remote = Remote(SOCK_PATH)
    for hash in ['A6B69431743F085D96692A81C6282617C50243C4', '67DD1659106DCDDE0FEC4283D7B0C84B6C292675']:
        peers = remote.get_peers(hash)
        assert len(peers) == 0


def test_api_10_global():
    g = Remote(SOCK_PATH).get_global()
    if g.system_api_version >= 10:
        assert g.network_http_current_open == 0


def test_api_11_global():
    g = Remote(SOCK_PATH).get_global()
    if g.system_api_version >= 11:
        values = {cmd.replace('.', '_') for cmd in Remote.COMMANDS_PER_API_VERSION[11]}
        assert len(g.__dict__.keys() & values) == len(values)


if __name__ == '__main__':
    test_global_data()
    test_torrents()
    test_trackers()
    test_files()
    test_peers()
