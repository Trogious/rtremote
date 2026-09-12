def extract_value(value):
    # a value is a single-key dict like {'string': 'x'} or {'i8': 1}; an empty
    # element (e.g. <string/>) comes through as None
    if isinstance(value, dict) and value:
        return next(iter(value.values()))
    return value


def add_attribute(obj, params, record):
    r = record['array']['data']['value']
    if not isinstance(r, list):  # single value is not wrapped in a list
        r = [r]
    for i in range(len(params)):
        attr = params[i]
        if len(attr) > 1 and attr[1] == '.':
            attr = attr[2:]
        if attr[-1] == '=':
            attr = attr[:-1]
        attr = attr.replace('.', '_')
        obj.__setattr__(attr, extract_value(r[i]))


class Global:
    def __init__(self):
        pass

    def add_attributes(self, data, params):
        data = data['methodResponse']['params']['param']['value']['array']['data']['value']
        if not isinstance(data, list):  # single result is not wrapped in a list
            data = [data]
        for i in range(len(data)):
            add_attribute(self, [params[i]], data[i])


class Torrent:
    def __init__(self):
        self.trackers = []
        self.peers = []
        self.files = []

    @staticmethod
    def get_torrents(data, params):
        torrents = []
        # jl(data)
        for record in data:
            t = Torrent()
            torrents.append(t)
            add_attribute(t, params, record)
        return torrents

    def __eq__(self, o):
        return self.hash == o.hash


class Tracker:
    EVENT_SCRAPE = 4

    @staticmethod
    def get_trackers(data, params):
        trackers = []
        for record in data:
            t = Tracker()
            trackers.append(t)
            add_attribute(t, params, record)
            is_busy_not_scrape = 1 if (t.latest_event != Tracker.EVENT_SCRAPE and t.is_busy == 1) else 0
            t.__setattr__('is_busy_not_scrape', is_busy_not_scrape)
        return trackers


class Peer:
    @staticmethod
    def get_peers(data, params):
        peers = []
        for record in data:
            p = Peer()
            peers.append(p)
            add_attribute(p, params, record)
        return peers


class File:
    @staticmethod
    def get_files(data, params):
        files = []
        # jl(data)
        for record in data:
            f = File()
            files.append(f)
            add_attribute(f, params, record)
        return files


class Client:
    def __init__(self, websocket, register_req_id, view_name):
        self.websocket = websocket
        self.req_id = register_req_id
        self.view_name = view_name
        if websocket.remote_address:
            self.ip = websocket.remote_address[0]
            self.port = websocket.remote_address[1]
        else:
            self.ip = 'unknown'
            self.port = -1

    def __repr__(self):
        # req_id is whatever JSON value the client sent as its request id
        return '%s:%d,%s,%s' % (self.ip, self.port, self.req_id, self.view_name)

    def __hash__(self):
        return self.websocket.__hash__()

    def __eq__(self, o):
        return self.websocket == o.websocket
