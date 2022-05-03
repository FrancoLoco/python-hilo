#!/usr/bin/env python
import asyncio
from aiohttp import ClientSession
import logging
from os import environ
import pyhilo
from pyhilo import API

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(name)s:%(filename)s:%(lineno)d %(threadName)s %(levelname)s %(message)s",
)


async def async_main() -> None:
    username="x@gmail.com"
    password="yyyyyy"
    api = await API.async_auth_password(username, password, session=ClientSession())
    print(api.device_attributes)


loop = asyncio.get_event_loop()
loop.create_task(async_main())
loop.run_forever()
