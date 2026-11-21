"""Ограниченное хранилище временных шагов aiogram FSM."""

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

    async def close(self):
        self.storage.clear()
        self.expires.clear()

    def record(self, key, create=False):
        now = self.clock()
        while self.expires and next(iter(self.expires.values())) <= now:
            stale, _ = self.expires.popitem(last=False)
            self.storage.pop(stale, None)
        if key not in self.storage and create:
            if len(self.storage) >= self.capacity:
                oldest, _ = self.expires.popitem(last=False)
                self.storage.pop(oldest)
            self.storage[key] = MemoryStorageRecord()
        if create:
            self.expires[key] = now + self.ttl
            self.expires.move_to_end(key)
        return self.storage.get(key)

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
        self.remove_empty(key, record)

    async def set_data(self, key, data):
        record = self.record(key, create=bool(data))
        if record:
            record.data = dict(data)
        self.remove_empty(key, record)

    async def get_value(self, storage_key, dict_key, default=None):
        return (await self.get_data(storage_key)).get(dict_key, default)

    def remove_empty(self, key, record):
        if record and record.state is None and not record.data:
            self.storage.pop(key, None)
            self.expires.pop(key, None)
