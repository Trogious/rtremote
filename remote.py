from rpc import RTorrentRpc


class Remote:
    # Requires rtorrent >= 0.16 (system.api_version >= 26). rtorrent 0.9.x is NOT
    # supported by this version; rtremote v1.5.0 is the last release for 0.9.x.
    #
    # The JSON field names are part of the Android app protocol and must stay
    # stable, so rtorrent commands that were renamed in 0.16 are requested under
    # their new names and aliased back to the field names the app knows.
    GLOBAL_COMMANDS = [
        'throttle.global_down.rate', 'throttle.global_up.rate', 'throttle.global_down.max_rate', 'throttle.global_up.max_rate',
        'network.max_open_files', 'throttle.max_downloads', 'throttle.max_uploads', 'network.http.max_total_connections',
        'system.sockets.size', 'system.sockets.max_size', 'throttle.unchoked_uploads', 'throttle.unchoked_downloads',
        'network.listen.port', 'network.listen.port.range', 'system.client_version', 'system.library_version', 'system.hostname',
        'system.pid', 'system.cwd', 'session.path', 'system.api_version',
        'network.http.current_open', 'network.total_handshakes', 'network.open_files',
        'throttle.max_unchoked_uploads', 'throttle.max_unchoked_downloads',
    ]
    # attribute produced by the rtorrent command -> wire field name the app expects
    GLOBAL_ALIASES = {
        'network_listen_port_range': 'network_port_range',
        'network_http_max_total_connections': 'network_http_max_open',
        'system_sockets_size': 'network_open_sockets',
        'system_sockets_max_size': 'network_max_open_sockets',
    }
    TORRENT_ALIASES = {
        'tracker_has_active_not_scrape': 'has_active_not_scrape',
    }

    def __init__(self, sock_path):
        self.sock_path = sock_path
        self.rpc = RTorrentRpc(sock_path)

    @staticmethod
    def apply_aliases(obj, aliases):
        for attr, wire_name in aliases.items():
            obj.__dict__[wire_name] = obj.__dict__.pop(attr)

    def get_global(self):
        g = self.rpc.global_data(Remote.GLOBAL_COMMANDS)
        Remote.apply_aliases(g, Remote.GLOBAL_ALIASES)
        return g

    def get_torrents(self, view='main'):
        params = ['d.hash=', 'd.name=', 'd.size_bytes=', 'd.bytes_done=', 'd.complete=', 'd.up.rate=', 'd.down.rate=', 'd.up.total=',
                  'd.down.total=', 'd.ratio=', 'd.size_files=', 'd.tracker_size=', 'd.peers_connected=', 'd.tied_to_file=',
                  'd.ignore_commands=', 'd.is_open=', 'd.is_active=', 'd.hashing=', 'd.is_hash_checking=', 'd.chunks_hashed=', 'd.message=',
                  'd.size_chunks=', 'd.completed_chunks=', 'd.tracker.has_active_not_scrape=']
        torrents = self.rpc.d_multicall(params, view)
        for t in torrents:
            Remote.apply_aliases(t, Remote.TORRENT_ALIASES)
            if t.has_active_not_scrape == 1:
                t.trackers = [x.__dict__ for x in Remote.optimize_trackers_digest(self.get_trackers_digest(t.hash))]
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
