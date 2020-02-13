from rpc import RTorrentRpc

# from utils import jl


class Remote:
    # minimum API_VERSION supported: 9
    #
    # 'network.total_handshakes', 'network.open_files', 'throttle.max_unchoked_uploads', 'throttle.max_unchoked_downloads'
    # available since: https://github.com/rakshasa/rtorrent/pull/937
    GLOBAL_COMMANDS_PER_API_VERSION = {
        10: ['network.http.current_open'],
        11: ['network.total_handshakes', 'network.open_files', 'throttle.max_unchoked_uploads', 'throttle.max_unchoked_downloads']
    }
    TORRENT_COMMANDS_PER_API_VERSION = {
        11: ['d.has_active_not_scrape=']
    }
    API_VERSION = -1

    def __init__(self, sock_path):
        self.sock_path = sock_path
        self.rpc = RTorrentRpc(sock_path)

    @staticmethod
    def append_commands_per_version(commands, commands_per_version):
        for api_version in commands_per_version.keys():
            if api_version <= Remote.API_VERSION:
                commands += commands_per_version[api_version]

    def get_global(self):
        if Remote.API_VERSION < 0:
            g = self.rpc.global_data(['system.api_version'])
            Remote.API_VERSION = g.system_api_version
        commands = ['throttle.global_down.rate', 'throttle.global_up.rate', 'throttle.global_down.max_rate', 'throttle.global_up.max_rate',
                    'network.max_open_files', 'throttle.max_downloads', 'throttle.max_uploads', 'network.http.max_open',
                    'network.open_sockets', 'network.max_open_sockets', 'throttle.unchoked_uploads', 'throttle.unchoked_downloads',
                    'network.listen.port', 'network.port_range', 'system.client_version', 'system.library_version', 'system.hostname',
                    'system.pid', 'system.cwd', 'session.path']
        Remote.append_commands_per_version(commands, Remote.GLOBAL_COMMANDS_PER_API_VERSION)
        g = self.rpc.global_data(commands)
        g.system_api_version = Remote.API_VERSION
        return g

    def get_torrents(self, view='main'):
        params = ['d.hash=', 'd.name=', 'd.size_bytes=', 'd.bytes_done=', 'd.complete=', 'd.up.rate=', 'd.down.rate=', 'd.up.total=',
                  'd.down.total=', 'd.ratio=', 'd.size_files=', 'd.tracker_size=', 'd.peers_connected=', 'd.tied_to_file=',
                  'd.ignore_commands=', 'd.is_open=', 'd.is_active=', 'd.hashing=', 'd.is_hash_checking=', 'd.chunks_hashed=', 'd.message=',
                  'd.size_chunks=', 'd.completed_chunks=']
        Remote.append_commands_per_version(params, Remote.TORRENT_COMMANDS_PER_API_VERSION)
        torrents = self.rpc.d_multicall(params, view)
        for t in torrents:
            if hasattr(t, 'has_active_not_scrape'):
                if t.has_active_not_scrape == 1:
                    t.trackers = [x.__dict__ for x in Remote.optimize_trackers_digest(self.get_trackers_digest(t.hash))]
            else:
                trackers = self.get_trackers_digest(t.hash)
                found_has_active_not_scrape = False
                for tracker in trackers:
                    if tracker.is_busy_not_scrape == 1:
                        found_has_active_not_scrape = True
                        break
                # add all only if at least one is_busy_not_scrape==1
                if found_has_active_not_scrape:
                    t.trackers = [x.__dict__ for x in Remote.optimize_trackers_digest(trackers)]
                t.__setattr__('has_active_not_scrape', 1 if found_has_active_not_scrape else 0)
        return torrents

    def get_torrents_hashes(self, view):
        return self.rpc.d_multicall(['d.hash='], view)

    def get_files(self, hash):
        params = ['f.size_chunks=', 'f.completed_chunks=', 'f.priority=', 'f.size_bytes=', 'f.path=']
        files = self.rpc.f_multicall(hash, params)
        return files

    def get_peers(self, hash):
        # missing in RPC:
        # is_down_choked_limited, is_down_queued, is_blocked, is_down_choked, is_down_interested, is_up_choked, is_up_interested,
        # outgoing_queue_size, incoming_queue_size, transfer->index, failed_counter
        params = ['p.address=', 'p.up_rate=', 'p.down_rate=', 'p.peer_rate=',
                  'p.is_preferred=', 'p.is_encrypted=', 'p.is_incoming=', 'p.completed_percent=', 'p.client_version=']
        peers = self.rpc.p_multicall(hash, params)
        return peers

    def get_trackers(self, hash):
        # missing in RPC: key, is_requesting, is_promiscuous_mode, is_failure_mode
        # is_busy_not_scrape: latest_event != EVENT_SCRAPE && is_busy
        params = ['t.group=', 't.url=', 't.is_busy=', 't.latest_event=', 't.id=', 't.failed_counter=', 't.success_counter=',
                  't.scrape_counter=', 't.is_usable=', 't.is_enabled=', 't.scrape_complete=', 't.scrape_incomplete=',
                  't.scrape_downloaded=', 't.latest_new_peers=', 't.latest_sum_peers=']
        trackers = self.rpc.t_multicall(hash, params)
        return trackers

    def get_trackers_digest(self, hash):
        params = ['t.group=', 't.url=', 't.is_busy=', 't.latest_event=']
        trackers = self.rpc.t_multicall(hash, params)
        return trackers

    @staticmethod
    def optimize_trackers_digest(trackers):
        for t in trackers:
            del t.is_busy
            del t.latest_event
        return trackers
