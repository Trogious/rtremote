#!/usr/bin/env python3
import asyncio
import json
import ssl

import websockets

CERT_PATH = './cert/cert.pem'

ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
# localhost_pem = pathlib.Path(__file__).with_name(CERT_PATH)
# ssl_context.load_verify_locations(localhost_pem)
# ssl_context.check_hostname = False
# ssl_context.verify_mode = ssl.CERT_NONE


def get_json_request():
    json_obj = {}
    json_obj['jsonrpc'] = '2.0'
    json_obj['id'] = 1
    json_obj['method'] = 'register'
    json_obj['params'] = {'secret_key': 'abc123'}
    return json.dumps(json_obj)


def get_json_request_files(method='get_files'):
    json_obj = {}
    json_obj['jsonrpc'] = '2.0'
    json_obj['id'] = 1
    json_obj['method'] = method
    json_obj['params'] = {'hash': '67DD1659106DCDDE0FEC4283D7B0C84B6C292675'}
    return json.dumps(json_obj)


async def hello():
    uri = "wss://127.0.0.1:8765"
    async with websockets.connect(uri, ssl=ssl_context) as websocket:
        await websocket.send(get_json_request())
        await websocket.send(get_json_request_files())
        await websocket.send(get_json_request_files('get_trackers'))
        await websocket.send(get_json_request_files('get_peers'))
        async for m in websocket:
            greeting = m  # await websocket.recv()
            print(greeting)

asyncio.get_event_loop().run_until_complete(hello())
# asyncio.get_event_loop().run_forever()
