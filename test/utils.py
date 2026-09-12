import json
import os
import socket
import time
from urllib.parse import urlparse

RTR_PID_PATH = os.getenv('RTR_PID_PATH', './wss_server.pid')
RTR_WSS_SERVER_URI = os.getenv('RTR_WSS_SERVER_URI', 'wss://127.0.0.1:8765')


def wait_for_server_spawn(seconds=15):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if os.path.isfile(RTR_PID_PATH):
            break
        time.sleep(0.5)
    else:
        raise Exception('wss server not ready, pid file %s not found' % RTR_PID_PATH)
    # the server only starts listening once it has its first rtorrent snapshot
    parsed = urlparse(RTR_WSS_SERVER_URI)
    while time.time() < deadline:
        try:
            sock = socket.create_connection((parsed.hostname, parsed.port or 8765), 0.5)
            sock.close()
            return
        except OSError:
            time.sleep(0.5)
    raise Exception('wss server not ready, %s not accepting connections' % RTR_WSS_SERVER_URI)


def get_json_request(method, params=None):
    json_obj = {}
    json_obj['jsonrpc'] = '2.0'
    json_obj['id'] = 1
    json_obj['method'] = method
    if params:
        json_obj['params'] = params
    return json.dumps(json_obj)


def get_rpc_param0_value(resp):
    return next(iter(resp['methodResponse']['params']['param']['value'].values()))
