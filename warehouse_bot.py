"""Диалоги обработки складских данных."""
import asyncio
from base64 import b64encode,b64decode
from contextlib import suppress
from io import BytesIO
import json
from pathlib import Path
import re
from secrets import token_hex
import sys
from time import monotonic

from aiogram import F
from aiogram.filters import StateFilter
from aiogram.fsm.state import StatesGroup,State
from aiogram.types import Message, BufferedInputFile
from aiohttp import ClientError
from aiogram.exceptions import ClientDecodeError, TelegramAPIError, TelegramBadRequest, TelegramNetworkError, TelegramServerError
from admin import keys
from parser import chunks
from protection import remember


DATA_DEADLINE = 45


class Data(StatesGroup):
    input=State()
    offer=State()


MENU={
    "home":[("Сортировка","asc"),("Сверка данных","check"),("Поиск дубликатов","dups"),("Очистка номеров","menu:clean"),("Помощь","help")],
    "check":[("Сверка пар","pairs"),("Сверка с эталоном","ref"),("Сверка таблиц","align")],
    "asc":[("По убыванию","desc")], "desc":[("По возрастанию","asc")],
    "dups":[("Удалить дубликаты","unique")]}
TITLE={"home":"Обработка данных","check":"Сверка данных. Можно сразу прислать два блока через пустую строку.",
       "asc":"Сортировка по возрастанию.","desc":"Сортировка по убыванию.",
       "quick":"Отправь два списка через пустую строку.","ref":"Сначала пришли эталон, затем второй список.",
       "pairs":"Пришли перемешанные пары. Можно сначала указать «ожидаю 50».",
       "align":"Пришли таблицу. По умолчанию сверяются A и B, с первой строки.",
       "dups":"Найду и сгруппирую повторения.","unique":"Удаление повторов выбрано. Пришли список.",
       "help":"Поддерживаются сообщение, TXT, XLSX и DOCX. Настройки перед файлом отдельным сообщением: «лист 2», «столбцы A B C,D» (ключ, проверяемый, связанные), «заголовок 1», «область 2-15» для абзацев Word. /cancel — отмена."}


def markup(mode, nonce=None):
    parent="check" if mode in {"quick","ref","pairs","align"} else "home"
    options=list(MENU.get(mode,[]))
    if mode!="home": options.append(("Назад",parent))
    if nonce:
        options.append(("Отмена", f"flow:cancel:{nonce}"))
    options.append(("Главное меню","menu:main"))
    keyboard=keys(*[(label,action if ":" in action else "data:"+action) for label,action in options])
    rows=keyboard.inline_keyboard
    if mode in {"home","check"}:
        rows[:2]=[rows[0]+rows[1]]
    return keyboard


async def worker(request,timeout):
    child = None

    async def transfer():
        nonlocal child
        child = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).with_name("warehouse_worker.py")),
            stdin=-1, stdout=-1, stderr=asyncio.subprocess.DEVNULL,
            env={"OPENBLAS_NUM_THREADS": "1", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        return await child.communicate(json.dumps(request).encode())

    try:
        output,_=await asyncio.wait_for(transfer(),timeout)
        if child.returncode:raise ValueError("Документ превысил лимит ресурсов.")
        result=json.loads(output)
        if "error" in result:raise ValueError(result["error"])
        return result
    finally:
        if child is not None and child.returncode is None:
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


def update_options(opts, command, arg, row_limit):
    if command == "столбцы":
        columns = arg.upper().split()
        if not 1 <= len(columns) <= 3 or not all(
            re.fullmatch(r"[A-Z]{1,3}(?:,[A-Z]{1,3})*" if i == 2 else r"[A-Z]{1,3}", col)
            for i, col in enumerate(columns)
        ) or len(columns[-1]) > 40:
            raise ValueError
        opts.update(a=columns[0], b=columns[1] if len(columns) >= 2 else "B",
                    link=columns[2] if len(columns) == 3 else "")
    elif command == "кодировка" and arg == "1251":
        opts["encoding"] = "cp1251"
    elif command == "заголовок" and arg in {"0", "1"}:
        opts["header"] = int(arg)
    elif command == "область" and re.fullmatch(r"[0-9]{1,5}-[0-9]{1,5}", arg):
        opts.update(area=arg, sheet="0")
    elif command == "лист" and re.fullmatch(r"[0-9]{1,2}", arg) and 1 <= int(arg) <= 16:
        opts["sheet"] = str(int(arg))
        opts.pop("area", None)
    elif command == "ожидаю" and re.fullmatch(r"[0-9]{1,5}", arg) and 1 <= int(arg) <= row_limit:
        opts["expected"] = int(arg)
    else:
        raise ValueError


def register(dp, clean):
    async def current(state, nonce):
        return (await state.get_data()).get("nonce") == nonce

    async def clear_current(state, nonce):
        if await current(state, nonce):
            await state.clear()

    async def set_mode(state, mode, opts=None, next_opts=None):
        data = {"mode": mode, "nonce": token_hex(8), "opts": dict(opts or {}), "at": monotonic()}
        if next_opts:
            data["next_opts"] = dict(next_opts)
        await state.set_state(Data.input)
        await state.set_data(data)
        return data

    def reply(message, guard, uid, text, **kwargs):
        return guard.service(uid, lambda: message.answer(text, **kwargs))

    async def edit_screen(message, title, keyboard):
        try:
            await message.edit_text(title, reply_markup=keyboard)
        except TelegramBadRequest as error:
            reason = error.message.lower()
            if "not modified" in reason:
                return
            if not any(reason_part in reason for reason_part in (
                "can't be edited", "can not be edited", "to edit not found", "no text in the message",
            )):
                raise
            await message.answer(title, reply_markup=keyboard)

    @dp.callback_query(F.data.startswith(("data:", "again:")))
    async def select(query, state, guard):
        namespace, _, selection = query.data.partition(":")
        uid = query.from_user.id
        if not isinstance(query.message, Message) or guard.busy(uid):
            await query.answer("Подожди завершения текущего действия.")
            return
        if namespace == "again":
            runtime = guard.state(uid)
            parameters = runtime.repeats.get(selection) if runtime else None
            if parameters is None:
                await query.answer("Повтор устарел. Выбери операцию заново.")
                return
            mode, opts, next_opts = parameters
            screen_mode = mode
        else:
            screen_mode, opts, next_opts = selection, {}, {}
            mode = "quick" if screen_mode == "check" else screen_mode
        if screen_mode not in TITLE:
            await query.answer("Действие недоступно.")
            return

        async def screen():
            runtime = guard.state(uid)
            if runtime is not None:
                runtime.screens.pop(query.message.message_id, None)
            await state.clear()
            if screen_mode in {"home", "help"}:
                keyboard = markup(screen_mode)
            else:
                data = await set_mode(state, mode, opts, next_opts)
                keyboard = markup(screen_mode, data["nonce"])
            await edit_screen(query.message, TITLE[screen_mode], keyboard)

        offered = guard.service(uid, screen)
        await query.answer(None if offered else "Подожди немного и повтори.")

    @dp.message(StateFilter(None, Data.offer), F.document | (F.text & ~F.text.startswith("/")))
    async def suggest(message, state, guard):
        uid = message.from_user.id
        if guard.busy(uid):
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
            buttons.inline_keyboard = [sum(rows[i:i+2], []) for i in range(0, len(rows), 2)]
            await state.set_state(Data.offer)
            await state.set_data({"source": source, "nonce": nonce, "at": monotonic()})
            try:
                await message.answer("Что сделать с файлом?" if doc else "Что сделать с текстом?", reply_markup=buttons)
            except (Exception, asyncio.CancelledError):
                await clear_current(state, nonce)
                raise
        guard.service(uid, offer)

    @dp.callback_query(F.data.startswith("offer:"))
    async def choose(query, state, guard):
        uid = query.from_user.id
        pending = await state.get_data()
        fields = query.data.split(":")
        if (len(fields) != 3 or await state.get_state() != Data.offer.state or
                fields[1] != pending.get("nonce") or monotonic() - pending["at"] >= guard.settings.data_ttl):
            await query.answer("Выбор устарел. Пришли данные ещё раз.")
            return
        mode = fields[2]
        source = pending["source"]
        if mode not in {"asc", "dups", "pairs", "ref", "align", "quick", "clean", "cancel"} or (
            mode == "clean" and source["kind"] != "text"
        ):
            await query.answer("Действие недоступно.")
            return
        if not isinstance(query.message, Message) or (mode != "cancel" and guard.busy(uid)):
            await query.answer("Подожди немного.")
            return
        runtime = guard.state(uid)
        if mode not in {"cancel", "ref"} and (runtime is None or guard.remaining(runtime, "heavy")):
            await query.answer("Подожди немного и нажми ещё раз.")
            return
        message = query.message
        if mode == "cancel":
            await guard.cancel_operation(uid, state)
            guard.service(uid, lambda: edit_screen(message, "Действие отменено.", markup("home")),
                          priority=True, essential=True)
        elif mode == "clean":
            await state.clear()
            await clean(message, guard, uid, source["text"])
        else:
            data = await set_mode(state, mode)
            await submit(message, state, guard, uid, data, source)
        await query.answer()

    @dp.message(StateFilter(Data.input))
    async def receive(message, state, guard):
        uid = message.from_user.id
        cfg = guard.settings
        data = await state.get_data()
        mode = data.get("mode")
        if not mode or guard.busy(uid):
            return
        if monotonic() - data["at"] >= cfg.data_ttl:
            await state.clear()
            reply(message, guard, uid, "Срок операции истёк.", reply_markup=markup("home"))
            return
        text = message.text or ""
        cmd, _, arg = text.partition(" ")
        opts = dict(data["opts"])
        if "\n" not in text and cmd in {"лист", "столбцы", "заголовок", "ожидаю", "кодировка", "область"}:
            try:
                update_options(opts, cmd, arg, cfg.data_rows)
            except ValueError:
                reply(message, guard, uid, "Проверь параметр в помощи раздела.")
                return
            if reply(message, guard, uid, "Настройка принята. Пришли данные."):
                await state.update_data(opts=opts)
            return
        doc = message.document
        source = {"kind": "text", "text": text, "opts": opts}
        if doc:
            kind = Path(doc.file_name or "").suffix.lower()[1:]
            if kind not in {"txt", "xlsx", "docx"} or (doc.file_size or 0) > cfg.data_mib * 1024**2:
                reply(message, guard, uid, "Нужен TXT, XLSX или DOCX в пределах лимита размера.")
                return
            source = {"kind": kind, "id": doc.file_id, "opts": opts}
        elif not text or len(text) > cfg.max_input_chars:
            reply(message, guard, uid, "Отправь текст или поддерживаемый файл.")
            return
        await submit(message, state, guard, uid, data, source)

    async def submit(message, state, guard, uid, data, source):
        cfg = guard.settings
        runtime = guard.state(uid)
        mode, nonce = data["mode"], data["nonce"]
        if runtime is None:
            return
        if mode == "ref" and "first" not in data:
            async def confirm_reference():
                try:
                    await message.answer("Эталон получен. Пришли проверяемый список.")
                except (Exception, asyncio.CancelledError):
                    await clear_current(state, nonce)
                    raise

            if guard.service(uid, confirm_reference):
                await state.update_data(first=source, opts=data.get("next_opts", {}))
            return
        if guard.remaining(runtime, "heavy"):
            if guard.warning(runtime, "data", cfg.warning_interval):
                reply(message, guard, uid, "Подожди немного и повтори.")
            return

        sources = (data["first"], source) if "first" in data else (source,)
        options = tuple(dict(item["opts"]) for item in sources)
        bot, chat_id = message.bot, message.chat.id
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DATA_DEADLINE

        def remaining():
            budget = deadline - loop.time()
            if budget <= 0:
                raise asyncio.TimeoutError
            return budget

        async def ensure_current():
            if not await current(state, nonce) or not guard.heavy.ticket(uid):
                raise asyncio.CancelledError

        async def send_status(report_id, report_document, status):
            try:
                if report_id and report_document:
                    await bot.edit_message_caption(chat_id=chat_id, message_id=report_id, caption=status, parse_mode=None)
                elif report_id:
                    await bot.edit_message_text(chat_id=chat_id, message_id=report_id, text=status, parse_mode=None)
                else:
                    await bot.send_message(chat_id, status, parse_mode=None)
            except TelegramBadRequest:
                if report_id:
                    await bot.send_message(chat_id, status, parse_mode=None)

        def notify(status, report_id=None, report_document=False):
            guard.service(uid, lambda: send_status(report_id, report_document, status), priority=True, essential=True)

        def expired():
            async def finish_expired():
                if await current(state, nonce):
                    await state.clear()
                    await send_status(None, False, "Время обработки истекло в очереди. Пришли данные заново.")
            guard.service(uid, finish_expired, priority=True, essential=True)

        async def run():
            report_id = None
            report_document = False
            total = sent = 0
            sending = False
            processed = False
            try:
                await ensure_current()
                items = []
                for item in sources:
                    await ensure_current()
                    if "id" in item:
                        with Buffer(cfg.data_mib*1024**2) as buf:
                            await asyncio.wait_for(bot.download(item["id"], destination=buf), min(15, remaining()))
                            raw = buf.getvalue()
                    else:
                        raw = item["text"].encode()
                    items.append({"kind": item["kind"], "opts": item["opts"], "data": b64encode(raw).decode()})
                result = await worker({"items": items, "mode": mode, "limit": cfg.data_rows,
                                       "expected": options[-1].get("expected")}, min(cfg.data_timeout, remaining()))
                await ensure_current()
                outputs = []
                for out in result["outputs"]:
                    if out["kind"] == "text":
                        for group in out["data"]:
                            outputs.extend(chunks(group))
                    else:
                        outputs.append(BufferedInputFile(b64decode(out["data"]), filename="Результат." + out["kind"]))
                total = len(outputs)
                processed = True
                repeat = remember(runtime.repeats, (mode, options[0], options[-1] if len(options) == 2 else {}))
                report = result["report"] + f"\nСообщений с результатом: {total}. Доставка результата отдельными сообщениями."
                report_document = len(report.encode("utf-16-le")) > 8192
                keyboard = keys(("Повторить", f"again:{repeat}"), ("Обработка данных", "data:home"))
                await ensure_current()
                timeout = remaining()
                sending = True
                if report_document:
                    request = bot.send_document(chat_id, BufferedInputFile(report.encode(), filename="Отчёт.txt"),
                                                caption=f"Отчёт об обработке. Ожидается результатов: {total}.",
                                                reply_markup=keyboard)
                else:
                    request = bot.send_message(chat_id, report, parse_mode=None, reply_markup=keyboard)
                report_message = await asyncio.wait_for(request, timeout)
                report_id = report_message.message_id
                sending = False
                for part in outputs:
                    await ensure_current()
                    timeout = remaining()
                    sending = True
                    request = (bot.send_message(chat_id, part, parse_mode=None) if isinstance(part, str)
                               else bot.send_document(chat_id, part))
                    await asyncio.wait_for(request, timeout)
                    sent += 1
                    sending = False
            except (TelegramAPIError, ClientDecodeError, ClientError, asyncio.TimeoutError, ValueError) as error:
                if not await current(state, nonce):
                    return
                if processed:
                    uncertain = sending and isinstance(error, (TelegramNetworkError, TelegramServerError,
                                                               ClientDecodeError, ClientError, asyncio.TimeoutError))
                    prefix = "Статус последней отправки неизвестен." if uncertain else "Отправка завершилась не полностью."
                    status = f"Данные обработаны. {prefix}\nПодтверждено частей результата: {sent} из {total}."
                    notify(status, report_id, report_document)
                else:
                    description = (str(error) if isinstance(error, ValueError) else
                                   "Время обработки истекло." if isinstance(error, asyncio.TimeoutError) else
                                   "Не удалось получить данные.")
                    notify(description + " Пришли данные заново.")
                    await set_mode(state, mode, options[0], options[-1] if len(options) == 2 else {})
            finally:
                await clear_current(state, nonce)

        if guard.heavy.submit(uid, run, long=True, expires=deadline, on_expire=expired):
            guard.limit(runtime, "heavy", cfg.clean_cooldown)
            # В очереди остаётся одна копия входных данных, FSM удерживает только идентификатор.
            await state.set_data({"mode": mode, "nonce": nonce, "at": data["at"], "opts": {}})
        elif guard.warning(runtime, "queue", cfg.warning_interval):
            reply(message, guard, uid, "Очередь заполнена. Попробуй позже.")
