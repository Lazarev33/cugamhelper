"""Ограниченное хранилище временных шагов aiogram FSM."""

import asyncio
from collections import OrderedDict
from time import monotonic

from aiogram.fsm.storage.memory import MemoryStorage, MemoryStorageRecord


class DialogStorage(MemoryStorage):
    def __init__(self, clock=monotonic, capacity=512, ttl=600):
        super().__init__()
        self.storage = OrderedDict()
        self.expires = OrderedDict()
        self.clock = clock
        self.capacity = capacity
        self.ttl = ttl
        self.data_ttl = ttl
        self.timers = {}

    async def close(self):
        for timer in self.timers.values():
            timer.cancel()
        self.timers.clear()
        self.storage.clear()
        self.expires.clear()

    def record(self, key, create=False):
        if self.expires.get(key, float("inf")) <= self.clock():
            self.remove(key)
        if key not in self.storage and create:
            if len(self.storage) >= self.capacity:
                self.remove(next(iter(self.storage)))
            self.storage[key] = MemoryStorageRecord()
        return self.storage.get(key)

    def remove(self, key):
        self.storage.pop(key, None)
        self.expires.pop(key, None)
        timer = self.timers.pop(key, None)
        if timer:
            timer.cancel()

    def update_expiration(self, key, record):
        if record.state is None and not record.data:
            self.remove(key)
            return
        now = self.clock()
        deadline = now + self.ttl
        if (record.state or "").startswith("Data:") and "at" in record.data:
            deadline = min(deadline, record.data["at"] + self.data_ttl)
        if timer := self.timers.get(key):
            timer.cancel()
        self.storage.move_to_end(key)
        self.expires[key] = deadline
        self.timers[key] = asyncio.get_running_loop().call_later(
            max(0, deadline - now), self.remove, key,
        )

    async def get_state(self, key):
        record = self.record(key)
        return record.state if record else None

    async def get_data(self, key):
        record = self.record(key)
        return record.data.copy() if record else {}

    async def set_state(self, key, state=None):
        record = self.record(key, create=state is not None)
        if record:
            record.state = getattr(state, "state", state)
            self.update_expiration(key, record)

    async def set_data(self, key, data):
        record = self.record(key, create=bool(data))
        if record:
            record.data = dict(data)
            self.update_expiration(key, record)

    async def get_value(self, storage_key, dict_key, default=None):
        return (await self.get_data(storage_key)).get(dict_key, default)
