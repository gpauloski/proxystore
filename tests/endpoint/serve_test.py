from __future__ import annotations

import asyncio
import contextlib
import logging
import multiprocessing
import os
import sys
import time
import uuid
from typing import AsyncGenerator
from unittest import mock

import pytest
import pytest_asyncio
import quart
import requests

from proxystore.endpoint.endpoint import Endpoint
from proxystore.endpoint.serve import create_app
from proxystore.endpoint.serve import MAX_CHUNK_LENGTH
from proxystore.endpoint.serve import serve
from proxystore.utils import chunk_bytes
from testing.compat import randbytes

if sys.version_info >= (3, 8):  # pragma: >=3.8 cover
    from unittest.mock import AsyncMock
else:  # pragma: <3.8 cover
    from asynctest import CoroutineMock as AsyncMock


@pytest_asyncio.fixture
@pytest.mark.asyncio
async def quart_app() -> AsyncGenerator[quart.typing.TestAppProtocol, None]:
    async with Endpoint(
        name='my-endpoint',
        uuid=uuid.uuid4(),
    ) as endpoint:
        app = create_app(endpoint)
        async with app.test_app() as test_app:
            test_app.endpoint = endpoint  # type: ignore
            yield test_app


@pytest.mark.asyncio
async def test_running(quart_app) -> None:
    client = quart_app.test_client()
    response = await client.get('/')
    assert response.status_code == 200

    response = await client.get('/endpoint')
    assert len((await response.get_json())['uuid']) > 0


@pytest.mark.asyncio
async def test_set_request(quart_app) -> None:
    client = quart_app.test_client()
    data = randbytes(100)
    set_response = await client.post(
        '/set',
        headers={'Content-Type': 'application/octet-stream'},
        query_string={'key': 'my-key'},
        data=data,
    )
    assert set_response.status_code == 200

    # overwrite key should be okay
    data = randbytes(100)
    set_response = await client.post(
        '/set',
        headers={'Content-Type': 'application/octet-stream'},
        query_string={'key': 'my-key'},
        data=data,
    )
    assert set_response.status_code == 200


@pytest.mark.asyncio
async def test_get_request(quart_app) -> None:
    client = quart_app.test_client()
    data = randbytes(100)
    set_response = await client.post(
        '/set',
        headers={'Content-Type': 'application/octet-stream'},
        query_string={'key': 'my-key'},
        data=data,
    )
    assert set_response.status_code == 200

    get_response = await client.get('/get', query_string={'key': 'my-key'})
    assert get_response.status_code == 200
    assert (await get_response.get_data()) == data

    get_response = await client.get(
        '/get',
        query_string={'key': 'missing-key'},
    )
    assert get_response.status_code == 400


@pytest.mark.asyncio
async def test_chunked_data(quart_app) -> None:
    client = quart_app.test_client()
    # Data needs to be larger than MAX_CHUNK_LENGTH
    data = randbytes((2 * MAX_CHUNK_LENGTH) + 1)

    async with client.request(
        '/set',
        method='POST',
        headers={'Content-Type': 'application/octet-stream'},
        query_string={'key': 'my-key'},
    ) as connection:
        for chunk in chunk_bytes(data, MAX_CHUNK_LENGTH):
            await connection.send(chunk)
            # Small sleep to simulate transfer time of chunks
            await asyncio.sleep(0.01)
        await connection.send_complete()
    set_response = await connection.as_response()
    assert set_response.status_code == 200

    get_response = await client.get('/get', query_string={'key': 'my-key'})
    assert get_response.status_code == 200
    assert (await get_response.get_data()) == data


@pytest.mark.asyncio
async def test_empty_chunked_data(quart_app) -> None:
    client = quart_app.test_client()

    async with client.request(
        '/set',
        method='POST',
        headers={'Content-Type': 'application/octet-stream'},
        query_string={'key': 'my-key'},
    ) as connection:
        await connection.send_complete()
    set_response = await connection.as_response()
    assert set_response.status_code == 400


@pytest.mark.asyncio
async def test_exists_request(quart_app) -> None:
    client = quart_app.test_client()
    exists_response = await client.get(
        'exists',
        query_string={'key': 'my-key'},
    )
    assert exists_response.status_code == 200
    assert not (await exists_response.get_json())['exists']

    data = randbytes(100)
    set_response = await client.post(
        '/set',
        headers={'Content-Type': 'application/octet-stream'},
        query_string={'key': 'my-key'},
        data=data,
    )
    assert set_response.status_code == 200

    exists_response = await client.get(
        'exists',
        query_string={'key': 'my-key'},
    )
    assert exists_response.status_code == 200
    assert (await exists_response.get_json())['exists']


@pytest.mark.asyncio
async def test_evict_request(quart_app) -> None:
    client = quart_app.test_client()
    evict_response = await client.post('evict', query_string={'key': 'my-key'})
    # No error if key does not exist
    assert evict_response.status_code == 200

    data = randbytes(100)
    set_response = await client.post(
        '/set',
        headers={'Content-Type': 'application/octet-stream'},
        query_string={'key': 'my-key'},
        data=data,
    )
    assert set_response.status_code == 200

    exists_response = await client.get(
        'exists',
        query_string={'key': 'my-key'},
    )
    assert exists_response.status_code == 200
    assert (await exists_response.get_json())['exists']

    evict_response = await client.post('evict', query_string={'key': 'my-key'})
    assert evict_response.status_code == 200

    exists_response = await client.get(
        'exists',
        query_string={'key': 'my-key'},
    )
    assert exists_response.status_code == 200
    assert not (await exists_response.get_json())['exists']


@pytest.mark.asyncio
async def test_payload_too_big() -> None:
    async with Endpoint(
        name='my-endpoint',
        uuid=uuid.uuid4(),
    ) as endpoint:
        app = create_app(endpoint, max_content_length=10)
        async with app.test_app() as quart_app:
            client = quart_app.test_client()
            data = randbytes(100)
            set_response = await client.post(
                '/set',
                headers={'Content-Type': 'application/octet-stream'},
                query_string={'key': 'my-key'},
                data=data,
            )
            assert set_response.status_code == 413


@pytest.mark.asyncio
async def test_bad_endpoint_uuid(quart_app) -> None:
    client = quart_app.test_client()
    bad_uuid = 'not a uuid'

    evict_response = await client.post(
        'evict',
        query_string={'key': 'my-key', 'endpoint': bad_uuid},
    )
    assert evict_response.status_code == 400

    exists_response = await client.get(
        'exists',
        query_string={'key': 'my-key', 'endpoint': bad_uuid},
    )
    assert exists_response.status_code == 400

    get_response = await client.get(
        'get',
        query_string={'key': 'my-key', 'endpoint': bad_uuid},
    )
    assert get_response.status_code == 400

    data = randbytes(100)
    set_response = await client.post(
        'set',
        headers={'Content-Type': 'application/octet-stream'},
        query_string={'key': 'my-key', 'endpoint': bad_uuid},
        data=data,
    )
    assert set_response.status_code == 400


@pytest.mark.asyncio
async def test_unknown_endpoint_uuid(quart_app) -> None:
    client = quart_app.test_client()
    unknown_uuid = uuid.uuid4()

    with mock.patch(
        'proxystore.endpoint.endpoint.Endpoint._is_peer_request',
        return_value=True,
    ):
        quart_app.endpoint._peer_manager = AsyncMock()
        quart_app.endpoint._peer_manager.send = AsyncMock(
            side_effect=Exception(),
        )
        quart_app.endpoint._peer_manager.close = AsyncMock()

        evict_response = await client.post(
            'evict',
            query_string={'key': 'my-key', 'endpoint': unknown_uuid},
        )
        assert evict_response.status_code == 400

        exists_response = await client.get(
            'exists',
            query_string={'key': 'my-key', 'endpoint': unknown_uuid},
        )
        assert exists_response.status_code == 400

        get_response = await client.get(
            'get',
            query_string={'key': 'my-key', 'endpoint': unknown_uuid},
        )
        assert get_response.status_code == 400

        data = randbytes(100)
        set_response = await client.post(
            'set',
            headers={'Content-Type': 'application/octet-stream'},
            query_string={'key': 'my-key', 'endpoint': unknown_uuid},
            data=data,
        )
        assert set_response.status_code == 400


@pytest.mark.asyncio
async def test_missing_key(quart_app) -> None:
    client = quart_app.test_client()

    evict_response = await client.post('evict')
    assert evict_response.status_code == 400

    exists_response = await client.get('exists')
    assert exists_response.status_code == 400

    get_response = await client.get('get')
    assert get_response.status_code == 400

    data = randbytes(100)
    set_response = await client.post(
        'set',
        headers={'Content-Type': 'application/octet-stream'},
        data=data,
    )
    assert set_response.status_code == 400


@pytest.mark.timeout(5)
def test_serve() -> None:
    name = 'my-endpoint'
    uuid_ = uuid.uuid4()
    host = 'localhost'
    port = 5823

    def serve_without_stdout() -> None:
        with contextlib.redirect_stdout(None), contextlib.redirect_stderr(
            None,
        ):
            logging.disable(10000)
            serve(name=name, uuid=uuid_, host=host, port=port)

    process = multiprocessing.Process(target=serve_without_stdout)
    process.start()

    try:
        while True:
            try:
                r = requests.get(f'http://{host}:{port}/')
            except requests.exceptions.ConnectionError:
                time.sleep(0.01)
                continue
            if r.status_code == 200:  # pragma: no branch
                break
    finally:
        process.terminate()


@mock.patch('quart.Quart.run')
def test_serve_logging(mock_run, tmp_dir) -> None:
    # Make directory if necessary
    log_file = os.path.join(tmp_dir, 'log.txt')
    serve(
        'name',
        uuid.uuid4(),
        '0.0.0.0',
        1234,
        server=None,
        log_level='INFO',
        log_file=log_file,
    )
    assert os.path.isdir(tmp_dir)
    assert os.path.exists(log_file)

    # Write log to existing log directory
    log_file2 = os.path.join(tmp_dir, 'log2.txt')
    serve(
        'name',
        uuid.uuid4(),
        '0.0.0.0',
        1234,
        server=None,
        log_level='INFO',
        log_file=log_file2,
    )
    assert os.path.isdir(tmp_dir)
    assert os.path.exists(log_file2)
