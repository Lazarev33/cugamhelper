"""Диалоги обработки складских данных."""
import asyncio
from base64 import b64encode,b64decode
from contextlib import suppress
from io import BytesIO
import json
from pathlib import Path
from secrets import token_hex
import sys
from time import monotonic

from aiogram import F
from aiogram.filters import StateFilter
from aiogram.fsm.state import StatesGroup,State
from aiogram.types import Message, BufferedInputFile
from aiogram.exceptions import TelegramBadRequest
from admin import keys
from parser import chunks


class Data(StatesGroup):
    input=State()
    offer=State()


MENU={
    "home":[("Сортировка","asc"),("Сверка данных","check"),("Поиск дубликатов","dups"),("Очистка номеров","menu:clean"),("Помощь","help")],
    "check":[("Быстрая сверка","quick"),("Сверка пар","pairs"),("Сверка с эталоном","ref"),("Сверка таблиц","align")],
    "asc":[("По убыванию","desc")], "desc":[("По возрастанию","asc")],
    "dups":[("Удалить дубликаты","unique")]}
TITLE={"home":"Обработка данных","check":"Сверка данных. Можно сразу прислать два блока через пустую строку.",
       "asc":"Сортировка по возрастанию.","desc":"Сортировка по убыванию.",
       "quick":"Отправь два списка через пустую строку.","ref":"Сначала пришли эталон, затем второй список.",
       "pairs":"Пришли перемешанные пары. Можно сначала указать «ожидаю 50».",
       "align":"Пришли таблицу. По умолчанию сверяются A и B, с первой строки.",
       "dups":"Найду и сгруппирую повторения.","unique":"Удаление повторов выбрано. Пришли список.",
       "help":"Поддерживаются сообщение, TXT, XLSX и DOCX. Настройки перед файлом отдельным сообщением: «лист 2», «столбцы A B C,D» (ключ, проверяемый, связанные), «заголовок 1», «область 2-15» для абзацев Word. /cancel — отмена."}


def markup(mode):
    parent="check" if mode in {"quick","ref","pairs","align"} else "home"
    options=list(MENU.get(mode,[]))
    if mode!="home": options.append(("Назад",parent))
    options.append(("Главное меню","menu:main"))
    keyboard=keys(*[(label,action if ":" in action else "data:"+action) for label,action in options])
    rows=keyboard.inline_keyboard
    if mode in {"home","check"}:
        rows[:2]=[rows[0]+rows[1]]
        if mode=="check":rows[1:3]=[rows[1]+rows[2]]
    return keyboard


async def worker(request,timeout):
    child=await asyncio.create_subprocess_exec(sys.executable,str(Path(__file__).with_name("warehouse_worker.py")),
        stdin=-1,stdout=-1,stderr=asyncio.subprocess.DEVNULL,env={"OPENBLAS_NUM_THREADS":"1","LANG":"C.UTF-8"})
    try:
        output,_=await asyncio.wait_for(child.communicate(json.dumps(request).encode()),timeout)
        if child.returncode:raise ValueError("Документ превысил лимит ресурсов.")
        result=json.loads(output)
        if "error" in result:raise ValueError(result["error"])
        return result
    finally:
        if child.returncode is None:
            with suppress(ProcessLookupError):
                child.kill()
            await child.communicate()


class Buffer(BytesIO):
    def __init__(self,limit):
        super().__init__()
        self.limit=limit

    def write(self,b):
        if self.tell()+len(b)>self.limit:raise ValueError("Файл слишком большой.")
        return super().write(b)


def register(dp, clean):
    active=set()

    async def clear_current(state, nonce):
        if (await state.get_data()).get("nonce") == nonce:
            await state.clear()

    @dp.callback_query(F.data.startswith("data:"))
    async def select(query,state,guard):
        mode=query.data[5:]
        await query.answer()
        if mode not in TITLE or not isinstance(query.message,Message):return
        uid=query.from_user.id
        if guard.busy(uid):return
        async def screen():
            runtime = guard.state(uid)
            if runtime is not None:
                runtime.screens.pop(query.message.message_id, None)
            await state.clear()
            if mode not in {"home","help"}:
                await state.set_state(Data.input)
                await state.set_data({"mode":"quick" if mode=="check" else mode,"nonce":token_hex(8),"opts":{},"at":monotonic()})
            try:
                await query.message.edit_text(TITLE[mode],reply_markup=markup(mode))
            except TelegramBadRequest as e:
                if "not modified" not in e.message.lower():
                    await query.message.answer(TITLE[mode],reply_markup=markup(mode))
        guard.light.submit(uid,screen)

    @dp.message(StateFilter(None, Data.offer), F.document | (F.text & ~F.text.startswith("/")))
    async def suggest(message, state, guard):
        uid = message.from_user.id
        if guard.busy(uid) or guard.state(uid) is None:
            return
        doc = message.document
        kind = Path(doc.file_name or "").suffix.lower()[1:] if doc else "text"
        async def offer():
            if kind not in {"xlsx", "docx", "txt", "text"}:
                await message.answer("Для обработки пришли XLSX, DOCX или TXT.")
                return
            if doc and (doc.file_size or 0) > guard.settings.data_mib * 1024**2:
                await message.answer("Файл превышает лимит размера.")
                return
            if not doc and len(message.text) > guard.settings.max_input_chars:
                await message.answer("Раздели текст на несколько сообщений.")
                return
            source = {"kind": kind, "opts": {}}
            if doc:
                source["id"] = doc.file_id
            else:
                source["text"] = message.text
            nonce = token_hex(6)
            actions = [("Сортировать", "asc"), ("Найти дубликаты", "dups"),
                       ("Проверить пары", "pairs"), ("Использовать как эталон", "ref")]
            if kind in {"xlsx", "docx"}:
                actions.insert(0, ("Сопоставить столбцы", "align"))
            if kind != "xlsx":
                actions.insert(0, ("Сверить два списка", "quick"))
            if kind == "text":
                actions.append(("Очистить номера", "clean"))
            actions.append(("Отмена", "cancel"))
            buttons = keys(*[(label, f"offer:{nonce}:{mode}") for label, mode in actions])
            rows = buttons.inline_keyboard
            buttons.inline_keyboard = [sum(rows[i:i+2], []) for i in range(0,len(rows),2)]
            await state.set_state(Data.offer)
            await state.set_data({"source":source, "nonce":nonce, "at":monotonic()})
            try:
                await message.answer("Что сделать с файлом?" if doc else "Что сделать с текстом?", reply_markup=buttons)
            except (Exception, asyncio.CancelledError):
                await clear_current(state, nonce)
                raise
        guard.light.submit(uid, offer)

    @dp.callback_query(F.data.startswith("offer:"))
    async def choose(query, state, guard):
        uid = query.from_user.id
        pending = await state.get_data()
        fields = query.data.split(":")
        if (len(fields)!=3 or await state.get_state()!=Data.offer.state or
                fields[1]!=pending.get("nonce") or monotonic()-pending["at"]>guard.settings.data_ttl):
            await query.answer("Выбор устарел. Пришли данные ещё раз.")
            return
        mode = fields[2]
        source = pending["source"]
        if mode not in {"asc","dups","pairs","ref","align","quick","clean","cancel"} or mode=="clean" and source["kind"]!="text":
            await query.answer("Действие недоступно.")
            return
        if not isinstance(query.message,Message) or guard.busy(uid):
            await query.answer("Подожди немного.")
            return
        active.intersection_update(guard.heavy.keys)
        user = guard.state(uid)
        if user is None or mode not in {"cancel","ref"} and (guard.remaining(user,"clean" if mode=="clean" else "data") or len(active)>=guard.settings.data_parallel):
            await query.answer("Подожди немного и нажми ещё раз.")
            return
        await state.clear()
        if mode not in {"clean","cancel"}:
            data={"mode":mode,"nonce":token_hex(8),"opts":{},"at":monotonic()}
            await state.set_state(Data.input)
            await state.set_data(data)
        await query.answer()
        if mode=="cancel":
            await query.message.edit_text("Действие отменено.")
        elif mode=="clean":
            await clean(query.message, guard, uid, source["text"])
        else:
            await submit(query.message,state,guard,uid,data,source)

    @dp.message(StateFilter(Data.input))
    async def receive(message,state,guard):
        uid=message.from_user.id
        cfg=guard.settings
        data=await state.get_data()
        mode=data.get("mode")
        if not mode:return
        if monotonic()-data["at"]>cfg.data_ttl:
            await state.clear()
            await message.answer("Срок операции истёк.",reply_markup=markup("home"));return
        user=guard.state(uid)
        if not user or guard.busy(uid):return
        text=message.text or ""
        cmd,_,arg=text.partition(" ")
        opts=dict(data["opts"])
        if "\n" not in text and cmd in {"лист","столбцы","заголовок","ожидаю","кодировка","область"}:
            try:
                if cmd=="столбцы":
                    a,*rest=arg.upper().split()
                    opts.update(a=a,b=rest[0] if rest else "B",link=rest[1] if len(rest)>1 else "")
                elif cmd=="кодировка" and arg=="1251":opts["encoding"]="cp1251"
                elif cmd=="заголовок" and arg in {"0","1"}:opts["header"]=int(arg)
                elif cmd=="область":opts["area"]=arg;opts["sheet"]="0"
                elif cmd=="лист" and 1<=int(arg)<=16:opts["sheet"]=arg
                elif cmd=="ожидаю" and 1<=int(arg)<=cfg.data_rows:opts["expected"]=int(arg)
                else:raise ValueError()
            except ValueError:
                await message.answer("Проверь параметр в помощи раздела.");return
            await state.update_data(opts=opts)
            await message.answer("Настройка принята. Пришли данные.");return
        doc=message.document
        source={"kind":"text","text":text,"opts":opts}
        if doc:
            kind=Path(doc.file_name or "").suffix.lower()[1:]
            if kind not in {"txt","xlsx","docx"} or (doc.file_size or 0)>cfg.data_mib*1024**2:
                await message.answer("Нужен TXT, XLSX или DOCX в пределах лимита размера.");return
            source={"kind":kind,"id":doc.file_id,"opts":opts}
        elif not text or len(text)>cfg.max_input_chars:
            await message.answer("Отправь текст или поддерживаемый файл.");return
        await submit(message,state,guard,uid,data,source)

    async def submit(message,state,guard,uid,data,source):
        cfg=guard.settings
        user=guard.state(uid)
        mode=data["mode"]
        opts=source["opts"]
        if mode=="ref" and "first" not in data:
            await state.update_data(first=source,opts={})
            try:
                await message.answer("Эталон получен. Пришли проверяемый список.")
            except (Exception, asyncio.CancelledError):
                await clear_current(state, data["nonce"])
                raise
            return
        active.intersection_update(guard.heavy.keys)
        if guard.remaining(user,"data") or len(active)>=cfg.data_parallel:
            if guard.warning(user,"data",cfg.warning_interval):
                await message.answer("Подожди немного и повтори.")
            return
        async def run():
            try:
                if (await state.get_data()).get("nonce") != data["nonce"]:
                    return
                items=[]
                for item in ([data["first"],source] if "first" in data else [source]):
                    if "id" in item:
                        with Buffer(cfg.data_mib*1024**2) as buf:
                            await asyncio.wait_for(message.bot.download(item["id"],destination=buf),15)
                            raw=buf.getvalue()
                    else: raw=item["text"].encode()
                    items.append({"kind":item["kind"],"opts":item["opts"],"data":b64encode(raw).decode()})
                result=await worker({"items":items,"mode":mode,"limit":cfg.data_rows,"expected":opts.get("expected")},cfg.data_timeout)
                if (await state.get_data()).get("nonce")!=data["nonce"]:return
                outputs=[]
                for out in result["outputs"]:
                    if out["kind"]=="text":
                        for group in out["data"]:outputs.extend(chunks(group))
                    else:
                        outputs.append(BufferedInputFile(b64decode(out["data"]),filename="Результат."+out["kind"]))
                report=result["report"]+f"\nСообщений с результатом: {len(outputs)}"
                if len(report.encode("utf-16-le"))>8192:
                    await message.answer_document(BufferedInputFile(report.encode(),filename="Отчёт.txt"),caption="Подробный отчёт")
                else:
                    await message.answer(report,parse_mode=None,reply_markup=keys(("Повторить", "data:"+mode),("Обработка данных","data:home")))
                for part in outputs:
                    if isinstance(part,str):await message.answer(part,parse_mode=None)
                    else:await message.answer_document(part)
            except (ValueError,asyncio.TimeoutError) as error:
                if (await state.get_data()).get("nonce")==data["nonce"]:
                    await state.set_data({"mode":mode,"nonce":token_hex(8),"opts":{},"at":monotonic()})
                    await message.answer((str(error) or "Время обработки истекло.")+" Пришли данные заново.",reply_markup=markup(mode))
            finally:
                await clear_current(state, data["nonce"])
                active.discard(uid)
        if guard.heavy.submit(uid,run):
            active.add(uid)
            guard.limit(user,"data",cfg.clean_cooldown)
        elif guard.warning(user,"queue",cfg.warning_interval):await message.answer("Очередь заполнена. Попробуй позже.")
