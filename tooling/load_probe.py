"""Синтетический короткий замер. Рабочий Telegram и рабочая БД не используются."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from statistics import median
from sys import path
from tempfile import TemporaryDirectory
from time import perf_counter, process_time
import json
import asyncio
import resource

path.insert(0, str(Path(__file__).resolve().parents[1]))
from barcodes import render_png
from catalog import Catalog
from warehouse_files import process
from infrastructure import Identity, InfraError
from telegram_bridge import Exports
from base64 import b64encode
from openpyxl import Workbook


def encoded(data):
    return b64encode(data).decode()


def work_db(db, index):
    repo = Catalog(db, owner=42)
    try:
        if index % 5 == 0:
            repo.save(42, f"Тест {index}", "TST-" + str(index))
        return bool(repo.page(0))
    finally:
        repo.close()


def report(name, function, count, threads):
    def timed(index):
        start = perf_counter()
        try:
            function(index)
            failed = False
        except Exception:
            failed = True
        return perf_counter() - start, failed
    start, cpu = perf_counter(), process_time()
    with ThreadPoolExecutor(threads) as pool:
        entries = list(pool.map(timed, range(count)))
    duration = perf_counter() - start
    intervals = sorted(x[0] for x in entries)
    return {"case":name,"operations":count,"parallel":threads,"seconds":round(duration,3),
            "cpu_seconds":round(process_time()-cpu,3),"rss_peak_kib_process":resource.getrusage(0).ru_maxrss,
            "median_ms":round(1000*median(intervals),2),"p95_ms":round(1000*intervals[int(count*.95)-1],2),
            "requests_per_s":round(count/duration,2),"errors":sum(x[1] for x in entries)}


async def queue_probe():
    waits = []
    starts = {}
    async def synthetic(session, text):
        waits.append(perf_counter() - starts[session])
        await asyncio.sleep(.002)
        return "delivered"
    pool = Exports(lambda _: True, synthetic)
    accepted = rejected = 0
    started = perf_counter()
    for index in range(24):
        user = Identity(str(index), index, "android", "device-"+str(index), "")
        starts[user.id] = perf_counter()
        try:
            pool.submit(user, "synthetic-export", ["отчёт", "SYN-0001"])
            accepted += 1
        except InfraError as limited:
            if limited.status != 429:
                raise
            rejected += 1
    while pool.pending:
        await pool.drain()
    restored = pool.submit(Identity("recovered", 90, "android", "recovered", ""), "recovered-job", ["отчёт"])
    starts["recovered"] = perf_counter()
    await pool.drain()
    return {"case":"export_queue","accepted":accepted,"capacity_rejections":rejected,
            "delivered":sum(record.status=="delivered" for record in pool.records.values()),
            "mean_send_wait_ms":round(1000*sum(waits)/len(waits),2),
            "wall_seconds":round(perf_counter()-started,3),"accepted_after_drain":restored.status=="delivered"}


def main():
    raw = encoded("\n".join(f"SYN-{index:06}" for index in reversed(range(1500))).encode())
    workbook = Workbook()
    for index in reversed(range(300)):
        workbook.active.append([str(index).zfill(5), index])
    stream = BytesIO()
    workbook.save(stream)
    xlsx = encoded(stream.getvalue())
    def transform(kind, data):
        return process({"items":[{"kind":kind,"opts":{"a":"A"} if kind == "xlsx" else {},"data":data}], "mode":"asc","limit":2000,
                        "expected":None})
    with TemporaryDirectory() as folder:
        db = Path(folder)/"catalog.db"
        Catalog(db, owner=42, seed=lambda: ["TEST-BASE"]).close()
        results = [
            report("code128", lambda _: sha256(render_png("TEST-000150")).digest(), 36, 4),
            report("text_1500", lambda _: transform("text", raw), 48, 4),
            report("xlsx_300", lambda _: transform("xlsx", xlsx), 16, 4),
            report("sqlite_read_write", lambda i: work_db(db, i), 100, 4),
        ]
    results.append(asyncio.run(queue_probe()))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
