import json
import os
import time

RTR_PID_PATH = os.getenv('RTR_PID_PATH', './wss_server.pid')


def wait_for_server_spawn(seconds=2):
    for _ in range(seconds):
        if os.path.isfile(RTR_PID_PATH):
            return
        time.sleep(1)
    raise Exception('wss server not ready, pid file %s not found' % RTR_PID_PATH)


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
