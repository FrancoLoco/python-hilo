"""Define a connection to the Hilo websocket."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum
import json
from os import environ
from typing import TYPE_CHECKING, Any, Callable, Dict, cast

from aiohttp import ClientWebSocketResponse, WSMsgType
from aiohttp.client_exceptions import (
    ClientError,
    ServerDisconnectedError,
    WSServerHandshakeError,
)
from yarl import URL

from pyhilo.const import DEFAULT_USER_AGENT, LOG
from pyhilo.exceptions import (
    CannotConnectError,
    ConnectionClosedError,
    ConnectionFailedError,
    InvalidMessageError,
    NotConnectedError,
)
from pyhilo.util import schedule_callback

if TYPE_CHECKING:
    from pyhilo import API

DEFAULT_WATCHDOG_TIMEOUT = timedelta(minutes=5)


class HiloMsgType(IntEnum):
    TEXT = 0x1
    EMPTY = 0x3
    PING = 0x6
    ERROR = 0x7
    UNKNOWN = 0xFF

    @classmethod
    def has_value(cls, value: int) -> bool:
        return value in cls._value2member_map_

    @classmethod
    def value(cls, value: int) -> IntEnum:  # type: ignore
        return cls._value2member_map_.get(value, cls.UNKNOWN)  # type: ignore


@dataclass(frozen=True)
class WebsocketEvent:
    """Define a representation of a message."""

    event_type_id: int
    target: str
    arguments: list[list]
    timestamp: datetime = field(default=datetime.now())
    event_type: str | None = field(init=False)

    def __post_init__(self) -> None:
        if HiloMsgType.has_value(self.event_type_id):
            object.__setattr__(
                self, "event_type", HiloMsgType.value(self.event_type_id).name
            )
        if self.event_type_id == HiloMsgType.ERROR:
            LOG.error(f"Received error event from HiloWS: {self.arguments}")


def websocket_event_from_payload(payload: dict[str, Any]) -> WebsocketEvent:
    """Create a Message object from a websocket event payload."""
    return WebsocketEvent(
        payload["type"], payload.get("target", ""), payload.get("arguments", "")
    )


class Watchdog:
    """Define a watchdog to kick the websocket connection at intervals."""

    def __init__(
        self, action: Callable[..., Any], timeout: timedelta = DEFAULT_WATCHDOG_TIMEOUT
    ):
        """Initialize."""
        self._action = action
        self._action_task: asyncio.Task | None = None
        self._loop = asyncio.get_running_loop()
        self._timeout_seconds = timeout.total_seconds()
        self._timer_task: asyncio.TimerHandle | None = None

    def _on_expire(self) -> None:
        """Log and act when the watchdog expires."""
        LOG.info("Websocket watchdog expired")
        schedule_callback(self._action)

    def cancel(self) -> None:
        """Cancel the watchdog."""
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None

    def trigger(self) -> None:
        """Trigger the watchdog."""
        LOG.debug(
            "Websocket watchdog triggered – sleeping for %s seconds",
            self._timeout_seconds,
        )

        if self._timer_task:
            self._timer_task.cancel()

        self._timer_task = self._loop.call_later(self._timeout_seconds, self._on_expire)


class WebsocketClient:
    """A websocket connection to the Hilo cloud.
    Note that this class shouldn't be instantiated directly; it will be instantiated as
    :param api: A :meth:`pyhilo.API` object
    :type api: :meth:`pyhilo.API`
    """

    def __init__(self, api: API) -> None:
        """Initialize."""
        self._api = api
        self._connect_callbacks: list[Callable[..., None]] = []
        self._disconnect_callbacks: list[Callable[..., None]] = []
        self._event_callbacks: list[Callable[..., None]] = []
        self._loop = asyncio.get_running_loop()
        self._watchdog = Watchdog(self.async_reconnect)

        # These will get filled in after initial authentication:
        self._client: ClientWebSocketResponse | None = None

    @property
    def connected(self) -> bool:
        """Return if currently connected to the websocket."""
        return self._client is not None and not self._client.closed

    @staticmethod
    def _add_callback(
        callback_list: list, callback: Callable[..., Any]
    ) -> Callable[..., None]:
        """Add a callback callback to a particular list."""
        callback_list.append(callback)

        def remove() -> None:
            """Remove the callback."""
            callback_list.remove(callback)

        return remove

    async def _async_receive_json(self) -> dict[str, Any]:
        """Receive a JSON response from the websocket server."""
        assert self._client
        msg = await self._client.receive(300)
        if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
            LOG.error("Connection was closed")
            raise ConnectionClosedError("Connection was closed.")

        if msg.type == WSMsgType.ERROR:
            LOG.error("Connection failed")
            raise ConnectionFailedError

        if msg.type != WSMsgType.TEXT:
            LOG.error(f"Invalid message: {msg}")
            raise InvalidMessageError(f"Received non-text message: {msg.type}")

        try:
            data = json.loads(msg.data[:-1])
        except ValueError as err:
            raise InvalidMessageError("Received invalid JSON") from err

        # LOG.debug(f"Received data from websocket server: {data}")
        self._watchdog.trigger()

        return cast(Dict[str, Any], data)

    async def _async_send_json(self, payload: dict[str, Any]) -> None:
        """Send a JSON message to the websocket server.
        Raises NotConnectedError if client is not connected.
        """
        if not self.connected:
            raise NotConnectedError

        assert self._client

        if payload.get("type") == HiloMsgType.PING and len(payload) == 1:
            LOG.debug("Websocket pong!")
        else:
            LOG.debug(f"Sending data to websocket server: {json.dumps(payload)}")
        # Hilo added a control character (chr(30)) at the end of each payload they send.
        # They also expect this char to be there at the end of every payload we send them.
        await self._client.send_str(json.dumps(payload) + chr(30))

    def _parse_message(self, msg: dict[str, Any]) -> None:
        """Parse an incoming message."""
        if msg.get("type") == HiloMsgType.PING:
            LOG.debug("Websocket ping?")
            schedule_callback(self._async_pong)
            return
        # LOG.debug(f"Received message {msg}")
        if not len(msg):
            return
        event = websocket_event_from_payload(msg)
        for callback in self._event_callbacks:
            schedule_callback(callback, event)

    def add_connect_callback(self, callback: Callable[..., Any]) -> Callable[..., None]:
        """Add a callback callback to be called after connecting.
        :param callback: The method to call after connecting
        :type callback: ``Callable[..., None]``
        """
        return self._add_callback(self._connect_callbacks, callback)

    def add_disconnect_callback(
        self, callback: Callable[..., Any]
    ) -> Callable[..., None]:
        """Add a callback callback to be called after disconnecting.
        :param callback: The method to call after disconnecting
        :type callback: ``Callable[..., None]``
        """
        return self._add_callback(self._disconnect_callbacks, callback)

    def add_event_callback(self, callback: Callable[..., Any]) -> Callable[..., None]:
        """Add a callback callback to be called upon receiving an event.
        Note that callbacks should expect to receive a WebsocketEvent object as a
        parameter.
        :param callback: The method to call after receiving an event.
        :type callback: ``Callable[..., None]``
        """
        return self._add_callback(self._event_callbacks, callback)

    async def async_connect(self) -> None:
        """Connect to the websocket server."""
        if self.connected:
            LOG.debug("async_connect() called but already connected")
            return

        LOG.info(f"Connecting to websocket server: {self._api.full_ws_url}")
        headers = {
            "Sec-WebSocket-Extensions": "permessage-deflate; client_max_window_bits",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "User-Agent": DEFAULT_USER_AGENT,
            "Origin": "http://localhost",
            "Accept-Language": "en-US,en;q=0.9",
        }
        proxy_env: dict[str, Any] = {}
        if proxy := environ.get("WS_PROXY"):
            proxy_env["proxy"] = proxy
            proxy_env["verify_ssl"] = False

        try:
            self._client = await self._api.session.ws_connect(
                URL(
                    self._api.full_ws_url.replace("/DeviceHub", "%2FDeviceHub"),
                    encoded=True,
                ),
                heartbeat=55,
                headers=headers,
                **proxy_env,
            )
        except (ClientError, ServerDisconnectedError, WSServerHandshakeError) as err:
            LOG.error(f"Unable to connect to WS server {err}")
        except Exception as err:
            LOG.error(f"Unable to connect to WS server {err}")
            raise CannotConnectError(err) from err

        LOG.info("Connected to websocket server")
        await self._async_send_status()
        schedule_callback(self.async_listen)

        self._watchdog.trigger()

        for callback in self._connect_callbacks:
            LOG.debug(f"Scheduling callback {callback}")
            schedule_callback(callback)

    async def async_disconnect(self) -> None:
        """Disconnect from the websocket server."""
        if not self.connected:
            return

        assert self._client

        await self._client.close()

        LOG.info("Disconnected from websocket server")

    async def async_listen(self) -> None:
        """Start listening to the websocket server."""
        assert self._client
        LOG.debug("Listen started.")
        try:
            while not self._client.closed:
                message = await self._async_receive_json()
                self._parse_message(message)
        except ConnectionClosedError as err:
            LOG.error("Websocket closed while listening: {err}")
            LOG.exception(err)
            pass
        finally:
            LOG.debug("Listen completed; cleaning up")

            self._watchdog.cancel()

            for callback in self._disconnect_callbacks:
                schedule_callback(callback)

    async def async_reconnect(self) -> None:
        """Reconnect (and re-listen, if appropriate) to the websocket."""
        LOG.warning("Reconnecting")
        await self.async_disconnect()
        await asyncio.sleep(1)
        await self.async_connect()

    async def _async_send_status(self) -> None:
        LOG.debug("Sending status")
        await self._async_send_json({"protocol": "json", "version": 1})

    async def _async_pong(self) -> None:
        await self._async_send_json({"type": HiloMsgType.PING})

    async def async_invoke(
        self, arg: list, target: str, inv_id: int, inv_type: WSMsgType = WSMsgType.TEXT
    ) -> None:
        await self._async_send_json(
            {
                "arguments": arg,
                "invocationId": str(inv_id),
                "target": target,
                "type": inv_type,
            }
        )