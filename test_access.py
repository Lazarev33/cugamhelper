import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from catalog import Catalog,CatalogError,SCHEMA
from test_bot import TelegramCase,CHAT,USER
from aiogram.methods import AnswerCallbackQuery


class Migration(unittest.TestCase):
    def test_import_snapshot_restart_and_concurrent_add(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/"db"
            with sqlite3.connect(path) as db:
                for sql in SCHEMA:db.execute(sql)
                db.execute("INSERT INTO admins VALUES(2)")
                db.execute("INSERT INTO barcodes(name,value) VALUES('A','A')")
                db.execute("PRAGMA user_version=1")
            repo=Catalog(path,1,legacy_ids=[3,3])
            self.assertTrue(repo.allowed(2))
            self.assertTrue(repo.allowed(3))
            self.assertIsNotNone(repo.by_value("A"))
            with sqlite3.connect(path.with_name("db.schema-1.bak")) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],1)
            repo.change_employee(2,3,"disable",1)
            repo.close()
            repo=Catalog(path,1,legacy_ids=[3,4])
            self.assertFalse(repo.allowed(3))
            self.assertFalse(repo.allowed(4))
            def add(actor):
                other=Catalog(path,1)
                try:
                    other.change_employee(actor,5,"give")
                    return True
                except CatalogError:return False
                finally:other.close()
            with ThreadPoolExecutor(2) as pool:self.assertCountEqual(pool.map(add,[1,2]),[True,False])
            repo.close()


class AccessTests(TelegramCase):
    def buttons(self):
        return {b.text:b.callback_data for row in self.session.requests[-1].reply_markup.inline_keyboard for b in row}

    async def test_stranger_silent_for_text_file_and_callbacks(self):
        for command in ["/start","/help","/myid","/admin","/scan","данные"]:await self.send(command,80)
        await self.feed(message={"message_id":9,"date":1,"chat":{**CHAT,"id":80},"from":{**USER,"id":80},
                                 "document":{"file_id":"x","file_unique_id":"y","file_name":"a.xlsx"}})
        self.assertFalse(self.session.requests)
        await self.click("ad:staffadd",80)
        self.assertIsInstance(self.session.requests[-1],AnswerCallbackQuery)
        self.assertIsNone(self.session.requests[-1].text)
        self.assertNotIn(80,self.guard.users)

    async def test_employee_lifecycle_and_cancel_running_job(self):
        await self.click("ad:staffadd")
        await self.send("80")
        await self.send("Иван\nВечер")
        await self.click(self.buttons()["Предоставить доступ"])
        await self.send("/start",80)
        self.assertIn("Помощник",self.session.requests[-1].text)
        await self.click("ad:staffsearch")
        await self.send("вечер")
        await self.click(next(iter(self.buttons().values())))
        await self.click(self.buttons()["Отозвать доступ"])
        button=self.buttons()["Отозвать доступ"]
        started=asyncio.Event()
        finished=[]
        async def job():
            started.set()
            await asyncio.sleep(15)
            finished.append(True)
        self.guard.heavy.submit(80,job)
        await started.wait()
        await self.click(button)
        self.assertFalse(finished)
        self.assertFalse(self.repo.allowed(80))
        await self.click("ad:staffcard:80")
        await self.click(self.buttons()["Восстановить доступ"])
        await self.click(self.buttons()["Восстановить доступ"])
        self.assertTrue(self.repo.allowed(80))

    async def test_roles_and_pagination(self):
        self.repo.set_admin(42,90,True)
        for target in range(100,112):self.repo.change_employee(90,target,"give",name="Имя")
        self.assertEqual(len(self.repo.employees("имя",1)),3)
        for target in [42,90]:
            with self.assertRaises(CatalogError):self.repo.change_employee(90,target,"disable")
        self.repo.set_admin(42,90,False)
        self.assertFalse(self.repo.allowed(90))
        with self.assertRaises(CatalogError):self.repo.change_employee(90,100,"disable",1)

    async def test_queued_operation_removed(self):
        from protection import Jobs
        pool=Jobs(1,1)
        self.addAsyncCleanup(pool.close)
        barrier=asyncio.Event()
        ran=asyncio.Event()
        pool.submit(42,barrier.wait)
        async def work():
            ran.set()
        pool.submit(43,work)
        pool.revoke(43)
        barrier.set()
        await asyncio.gather(*pool.tasks)
        await asyncio.sleep(0)
        self.assertFalse(ran.is_set())
        self.assertNotIn(43,pool.keys)

    async def test_last_check_before_send(self):
        from aiogram.methods import SendMessage
        from unittest.mock import AsyncMock
        limiter=self.guard.outgoing
        limiter.allowed=self.repo.allowed
        self.repo.change_employee(42,43,"disable",1)
        send=AsyncMock()
        with self.assertRaises(asyncio.CancelledError):
            await limiter(send,self.bot,SendMessage(chat_id=43,text="результат"))
        send.assert_not_awaited()

    async def test_shared_user_selection_and_role_fallback(self):
        await self.click("ad:staffadd")
        request=self.session.requests[-1].reply_markup.keyboard[0][0].request_users
        await self.feed(message={"message_id":9,"date":1,"chat":CHAT,"from":USER,
                      "users_shared":{"request_id":request.request_id,"users":[{"user_id":1000}]}})
        await self.send("-")
        await self.click(self.buttons()["Предоставить доступ"])
        self.assertTrue(self.repo.allowed(1000))
        self.repo.set_admin(42,1000,True)
        self.repo.set_admin(42,1000,False)
        self.assertTrue(self.repo.allowed(1000))
        self.assertFalse(self.repo.is_admin(1000))

    async def test_admin_picker_and_contact(self):
        for target in (81,82):
            await self.click("ad:grant")
            picker=self.session.requests[-1].reply_markup.keyboard[0][0]
            self.assertEqual(picker.text,"Выбрать администратора")
            payload={"users_shared":{"request_id":picker.request_users.request_id,"users":[{"user_id":target}]}} if target==81 else {"contact":{"first_name":"Тест","phone_number":"000","user_id":target}}
            await self.feed(message={"message_id":3,"date":1,"chat":CHAT,"from":USER,**payload})
            self.assertFalse(self.repo.is_admin(target))
            self.assertIn(str(target),self.session.requests[-1].text)
            await self.click(self.buttons()["Подтвердить"])
            self.assertTrue(self.repo.is_admin(target))

    async def test_contact_without_id(self):
        await self.click("ad:grant")
        await self.feed(message={"message_id":3,"date":1,"chat":CHAT,"from":USER,
                                 "contact":{"first_name":"Тест","phone_number":"000"}})
        self.assertIn("только номер телефона",self.session.requests[-1].text)
