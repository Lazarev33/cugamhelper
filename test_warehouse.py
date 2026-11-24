import asyncio
from base64 import b64decode,b64encode
from io import BytesIO
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZIP_DEFLATED

from aiogram.methods import SendDocument
from aiogram.fsm.storage.base import StorageKey
from docx import Document
from openpyxl import Workbook,load_workbook
from test_bot import TelegramCase, dp
from dialogs import DialogStorage
from warehouse import blocks,compare,order
from warehouse_bot import Data
from warehouse_files import process,inspect_zip


def item(kind,raw,**opts):
    return {"kind":kind,"data":b64encode(raw),"opts":opts}


def saved(book):
    output=BytesIO()
    book.save(output)
    return output.getvalue()


class ProcessingTests(unittest.TestCase):
    def test_sort_and_multiplicity(self):
        values=["A-10"," A","A-2","A-02","A-1","A-2","a"]
        for mode in ("asc","desc","dups","unique"):
            ix,_=order(values,mode)
            out=[values[i] for i in ix]
            self.assertCountEqual(out,set(values) if mode=="unique" else values)
        self.assertIn("Сошлось: 3/4",compare(list("AABC"),list("ABCC")))
        self.assertIn("Обнаружены расхождения.",compare(["A"],["A","B"]))

    def test_pairs(self):
        values=[str(i) for i in range(50)]*2
        for seq,good in [(values,50),(values[:-1],49),(values+["1","1"],49)]:
            ix,report=order(seq,"pairs",50)
            self.assertIn(f"Сошлось: {good}/50",report)
            self.assertEqual(len(ix),len(seq))
        self.assertIn("Обнаружены расхождения.",order(values,"pairs",51)[1])

    def test_blocks_and_txt(self):
        self.assertEqual(blocks([""," A "," \t","","a",""]),[[" A "],["a"]])
        req={"mode":"quick","limit":50,"items":[item("txt",b"2\r\n1\r\n \t\r\n1\n2")]}
        output=process(req)["outputs"][0]
        self.assertEqual(b64decode(output["data"]),b"1\n2\n\n1\n2")
        req["items"]=[item("txt",b"a\n\nb\n\nc")]
        with self.assertRaises(ValueError):process(req)

    def test_excel_alignment(self):
        w=Workbook()
        for row in [["Key","Counted","Bin"],["02","A","X"],["A","02","Y"],["A","Z","Q"]]:
            w.active.append(row)
        source = saved(w)
        with patch("openpyxl.worksheet._writer.create_temporary_file",
                   side_effect=AssertionError("XLSX worksheet must not touch disk")):
            r=process({"mode":"align","limit":50,"items":[item("xlsx",source,a="A",b="B",link="C",header=1)]})
        s=load_workbook(BytesIO(b64decode(r["outputs"][0]["data"]))).active
        self.assertEqual(list(s.values)[1:],[("02","02","Y"),("A","A","X"),("A",None,None),(None,"Z","Q")])

    def test_docx_and_mixed_reference(self):
        d=Document()
        for v in ["B","A","","A","B"]:d.add_paragraph(v)
        r=process({"mode":"quick","limit":20,"items":[item("docx",saved(d))]})
        out=Document(BytesIO(b64decode(r["outputs"][0]["data"])))
        self.assertEqual([p.text for p in out.paragraphs],["A","B","","A","B"])
        d=Document();d.add_paragraph("B");d.add_paragraph("A")
        r=process({"mode":"ref","limit":20,"items":[item("docx",saved(d)),item("text",b"A\nB")]})
        self.assertIn("Сошлось: 2/2",r["report"])
        self.assertEqual([x["kind"] for x in r["outputs"]],["docx","text"])

    def test_zip_limits(self):
        for body in [b'x'*17000000,b'<!DOCTYPE x><x/>']:
            stream=BytesIO()
            with ZipFile(stream,"w",ZIP_DEFLATED) as z:z.writestr("a.xml",body)
            with self.assertRaises(ValueError):inspect_zip(stream.getvalue())


class TelegramProcessing(TelegramCase):
    async def test_direct_text_offer_and_stale_button(self):
        await self.send("B\nA\n\nA\nB")
        prompt = self.session.requests[-1]
        button = next(b for row in prompt.reply_markup.inline_keyboard for b in row if b.text == "Сверить два списка")
        await self.click(button.callback_data)
        report, first, second = self.session.requests[-3:]
        self.assertIn("Сошлось: 2/2", report.text)
        self.assertEqual(first.text, "A\nB")
        self.assertEqual(second.text, first.text)
        await self.click(button.callback_data)
        self.assertIn("устарел", self.session.requests[-1].text)

    async def test_direct_file_becomes_reference(self):
        await self.feed(message={"message_id":6,"date":1,"chat":{"id":42,"type":"private"},
            "from":{"id":42,"first_name":"A","is_bot":False},
            "document":{"file_id":"example","file_unique_id":"u","file_name":"stock.xlsx"}})
        prompt = self.session.requests[-1]
        buttons = [b for row in prompt.reply_markup.inline_keyboard for b in row]
        self.assertTrue(any(b.text=="Сопоставить столбцы" for b in buttons))
        await self.click(next(b.callback_data for b in buttons if b.callback_data.endswith(":ref")))
        self.assertIn("Эталон получен", self.session.requests[-1].text)

    async def test_file_result_remains_in_chat_and_session_is_cleared(self):
        book = Workbook()
        book.active.append(["PALLET-002"])
        book.active.append(["PALLET-001"])
        source = saved(book)

        async def download(file_id, destination):
            destination.write(source)

        await self.click("data:asc")
        with patch.object(self.bot, "download", side_effect=download):
            await self.feed(message={"message_id": 6, "date": 1, "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "first_name": "A", "is_bot": False},
                "document": {"file_id": "example", "file_unique_id": "u", "file_name": "stock.xlsx"}})
        report, result = self.session.requests[-2:]
        self.assertIn("Сортировка завершена", report.text)
        self.assertIsInstance(result, SendDocument)
        actual = load_workbook(BytesIO(result.document.data))
        self.assertEqual(list(actual.active.values), [("PALLET-001",), ("PALLET-002",)])
        self.assertFalse(any("deleteMessage" in request.__api_method__ for request in self.session.requests))
        ctx = dp.fsm.get_context(bot=self.bot, chat_id=42, user_id=42)
        self.assertIsNone(await ctx.get_state())
        self.assertEqual(await ctx.get_data(), {})

    async def test_quick_three_messages(self):
        await self.click("data:check")
        before=len(self.session.requests)
        await self.send("123456\n123455\n12376\n19876\n\n19876\n123455\n12376\n123456")
        report,a,b=self.session.requests[before:]
        self.assertIn("Сошлось: 4/4",report.text)
        self.assertEqual(a.text,b.text)
        self.assertEqual(a.text,"12376\n19876\n123455\n123456")
        for m in (a,b):
            self.assertIsNone(m.parse_mode)
            self.assertIsNone(m.entities)
            self.assertIsNone(m.reply_markup)

    async def test_reference_and_cancel(self):
        await self.click("data:ref")
        await self.send("A\nA\nB")
        await self.send("A\nB\nB")
        report,a,b=self.session.requests[-3:]
        self.assertIn("Сошлось: 2/3",report.text)
        self.assertNotEqual(a.text,b.text)
        await self.click("data:ref")
        await self.send("private reference")
        await self.send("/cancel")
        self.clock.now+=10
        await self.click("menu:clean")
        await self.send("0009")
        self.assertEqual(self.session.requests[-1].text,"0009")

    async def test_expired_dialog(self):
        with patch("warehouse_bot.monotonic", self.clock), patch.object(dp.storage, "clock", self.clock):
            await self.click("data:ref")
            await self.send("A")
            self.clock.now += 1000
            await self.send("B")
        self.assertEqual(self.session.requests[-1].text, "Что сделать с текстом?")

    async def test_dialog_expires_with_no_new_updates(self):
        storage = DialogStorage(ttl=600)
        storage.data_ttl = 0.01
        self.addAsyncCleanup(storage.close)
        key = StorageKey(bot_id=1, chat_id=42, user_id=42)
        await storage.set_state(key, Data.input)
        await storage.set_data(key, {"at": storage.clock(), "first": {"text": "PALLET-001"}})
        self.assertIn(key, storage.storage)
        await asyncio.sleep(0.05)
        self.assertNotIn(key, storage.storage)
        self.assertNotIn(key, storage.timers)


class TableTests(unittest.TestCase):
    def test_word_table_extras(self):
        d=Document()
        t=d.add_table(rows=0,cols=2)
        for a,b in [("B","A"),("A","C"),("A","B")]:
            cells=t.add_row().cells
            cells[0].text=a
            cells[1].text=b
        r=process({"mode":"align","limit":20,"items":[item("docx",saved(d))]})
        out=Document(BytesIO(b64decode(r["outputs"][0]["data"])))
        self.assertEqual([[c.text for c in row.cells] for row in out.tables[0].rows],
                         [["B","B"],["A","A"],["A",""],["","C"]])

    def test_excel_sort_whole_rows(self):
        w=Workbook()
        w.active.append(["Key","Place"])
        w.active.append(["A-10","west"])
        w.active.append(["A-2","east"])
        r=process({"mode":"asc","limit":20,"items":[item("xlsx",saved(w),a="A",header=1)]})
        out=load_workbook(BytesIO(b64decode(r["outputs"][0]["data"])))
        self.assertEqual(list(out.active.values),[("Key","Place"),("A-2","east"),("A-10","west")])
