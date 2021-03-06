from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest

from proxystore.p2p import messages
from proxystore.p2p.connection import PeerConnection
from proxystore.p2p.exceptions import PeerConnectionError
from proxystore.p2p.exceptions import PeerConnectionTimeout
from proxystore.p2p.server import connect


@pytest.mark.asyncio
async def test_p2p_connection(signaling_server) -> None:
    uuid1, name1, websocket1 = await connect(signaling_server.address)
    connection1 = PeerConnection(uuid1, name1, websocket1)

    uuid2, name2, websocket2 = await connect(signaling_server.address)
    connection2 = PeerConnection(uuid2, name2, websocket2)

    await connection1.send_offer(uuid2)
    offer = messages.decode(cast(str, await websocket2.recv()))
    assert isinstance(offer, messages.PeerConnection)
    await connection2.handle_server_message(offer)
    answer = messages.decode(cast(str, await websocket1.recv()))
    assert isinstance(answer, messages.PeerConnection)
    await connection1.handle_server_message(answer)

    await connection1.ready()
    await connection2.ready()

    assert connection1.state == 'connected'
    assert connection2.state == 'connected'

    await connection1.send('hello')
    assert await connection2.recv() == 'hello'
    await connection2.send('hello hello')
    assert await connection1.recv() == 'hello hello'

    await websocket1.close()
    await websocket2.close()
    await connection1.close()
    await connection2.close()


@pytest.mark.asyncio
async def test_p2p_connection_timeout(signaling_server) -> None:
    uuid1, name1, websocket1 = await connect(signaling_server.address)
    connection1 = PeerConnection(uuid1, name1, websocket1)

    uuid2, name2, websocket2 = await connect(signaling_server.address)
    connection2 = PeerConnection(uuid2, name2, websocket2)

    await connection1.send_offer(uuid2)
    # Don't finish offer/answer sending so wait() times out

    with pytest.raises(PeerConnectionTimeout):
        await connection1.ready(timeout=0.5)

    await websocket1.close()
    await websocket2.close()
    await connection1.close()
    await connection2.close()


@pytest.mark.asyncio
async def test_p2p_connection_error(signaling_server) -> None:
    uuid, name, websocket = await connect(signaling_server.address)
    connection = PeerConnection(uuid, name, websocket)

    class MyException(Exception):
        pass

    await connection.handle_server_message(
        messages.PeerConnection(
            source_uuid=uuid,
            source_name=name,
            peer_uuid=uuid4(),
            description_type='offer',
            description='',
            error=str(MyException()),
        ),
    )

    with pytest.raises(PeerConnectionError):
        await connection.ready()

    await websocket.close()
