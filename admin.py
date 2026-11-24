"""Telegram-диалоги каталога, управления ролями и сканирования."""

import asyncio
import employees
from contextvars import ContextVar
from dataclasses import replace
import re
from secrets import token_hex
import sqlite3

from aiohttp import ClientError
from aiogram import F
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from barcodes import render_png, validate_value
from catalog import Barcode, CatalogDenied, CatalogError, DuplicateBarcode, validate_name
from scanner import ScanError, scan_telegram

PANEL = "Админ-панель"
CANCEL = ("Отмена", "flow:cancel")
CODE_UNSUPPORTED = "Сохранить можно обычный Code 128 с допустимым значением."
SCAN_PROMPT = "Пришли фото штрихкода или изображение JPEG, PNG или WebP."
SCAN_FAILED = "Штрихкод не прочитан. Приблизь код, избегай бликов и пришли фото ещё раз или отправь оригинал как файл."
NUMBER = re.compile(r"[0-9]{1,18}")


class Flow(StatesGroup):
    name = State()
    value = State()
    photo = State()
    choose = State()
    confirm = State()
    grant = State()
    staff = State()


def keys(*buttons) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data)] for label, data in buttons
    ])


def pages(buttons, prefix: str, page: int, more: bool, back):
    row = []
    if page:
        row.append(InlineKeyboardButton(text="‹", callback_data=f"{prefix}:{page-1}"))
    if more:
        row.append(InlineKeyboardButton(text="›", callback_data=f"{prefix}:{page+1}"))
    markup = keys(*buttons)
    if row:
        markup.inline_keyboard.append(row)
    markup.inline_keyboard.extend(keys(back).inline_keyboard)
    return markup


def number(value: str, maximum=2**63 - 1) -> int:
    if not NUMBER.fullmatch(value) or int(value) > maximum:
        raise CatalogError("Некорректный идентификатор.")
    return int(value)


edit_target = ContextVar("edit_target", default=None)


async def tell(message, text, markup=None):
    if edit_target.get() is message and message.text:
        edit_target.set(None)
        try:
            return await message.edit_text(text, parse_mode=None, reply_markup=markup)
        except TelegramBadRequest as error:
            reason = error.message.lower()
            if "message is not modified" in reason:
                return
            if not any(key in reason for key in ("edit not found", "can't be edited", "can not be edited")):
                raise
    return await message.answer(text, parse_mode=None, reply_markup=markup)


async def send_png(message, name: str, value: str, guard, markup, *, caption=None):
    filename = f"{value}.png" if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) else "barcode.png"
    image = BufferedInputFile(render_png(value), filename=filename)
    answer = message.answer_photo if guard.settings.barcode_mode == "photo" else message.answer_document
    if caption is None:
        caption = name if name == value else f"{name}\nCode 128: {value}"
    await answer(image, caption=caption, parse_mode=None, reply_markup=markup)


async def set_flow(state, stage, kind, **data):
    await state.set_data({"kind": kind, "nonce": token_hex(6), **data})
    await state.set_state(stage)


async def panel(message):
    markup = keys(
        ("Каталог ШК", "ad:list:0"), ("Добавить вручную", "ad:manual"), ("Добавить по фото", "ad:scan"),
        ("Доверенные пользователи","ad:staff"), ("Администраторы", "ad:roles:0"), ("Журнал изменений", "ad:log"), ("Главное меню", "menu:main"),
    )
    rows = markup.inline_keyboard
    rows[1:3] = [rows[1] + rows[2]]
    await tell(message, PANEL, markup)


async def card(message, record: Barcode):
    prefix = f"{record.id}:{record.revision}"
    flag = "активен" if record.active else "выключен"
    await tell(message, (
        f"#{record.id} · {record.name}\n{record.format}: {record.value}\nСтатус: {flag}"
        f"\nСоздан: {record.created_at} · автор: {record.created_by or 'импорт'}"
        f"\nИзменён: {record.updated_at}"
    ), keys(
        ("Сгенерировать ШК", f"ad:generate:{record.id}"),
        ("Переименовать", f"ad:rename:{record.id}"), ("Изменить значение", f"ad:value:{record.id}"),
        ("Выключить" if record.active else "Включить", f"ad:toggle:{prefix}"),
        ("Удалить", f"ad:delete:{prefix}"), ("Назад", "ad:list:0"),
    ))


async def duplicate_notice(message, error):
    record = error.record
    status = "активен" if record.active else "выключен"
    buttons = [("Открыть существующий ШК", f"ad:open:{record.id}")]
    if not record.active:
        buttons.append(("Восстановить ШК", f"ad:toggle:{record.id}:{record.revision}"))
    await tell(message, f"{error}\n#{record.id} · {record.name}\n{record.format}: «{record.value}»\nСтатус: {status}",
               keys(*buttons, CANCEL))


async def preview(message, state, guard, repo, data, name, value):
    old = data.get("old")
    duplicate = repo.by_value(value)
    if duplicate and (old is None or duplicate.id != old.id):
        await duplicate_notice(message, DuplicateBarcode(duplicate))
        return
    await set_flow(state, Flow.confirm, "save", old=old, name=name, value=value, origin=data["kind"])
    nonce = (await state.get_data())["nonce"]
    comparison = f"\nБыло: «{old.value}»\nБудет: «{value}»" if old and old.value != value else f"\nCode 128: «{value}»"
    await send_png(message, name, value, guard, keys(
        ("Сохранить", f"flow:ok:{nonce}"), ("Изменить", f"flow:change:{nonce}"), CANCEL,
    ), caption=f"Предпросмотр · {name}{comparison}")


async def admin_action(message, state, guard, repo, uid, action, args):
    repo.require_admin(uid)
    await state.clear()
    if action.startswith("staff") and (not args and action not in {"staff","staffadd","staffsearch"}):
        raise CatalogError("Некорректная кнопка.")
    if action == "home" and not args:
        await panel(message)
    elif action.startswith("staff"):
        await employees.screen(message,state,repo,uid,action,args)
    elif action == "list" and len(args) == 1:
        page = number(args[0], 1_000_000)
        records, more = repo.page(page)
        buttons = [(f"{'✓' if item.active else '○'} {item.name}", f"ad:open:{item.id}") for item in records]
        await tell(message, f"Каталог ШК · страница {page+1}", pages(buttons, "ad:list", page, more, ("Назад", "ad:home")))
    elif action in {"open", "generate", "rename", "value", "toggle", "delete"}:
        await record_action(message, state, guard, repo, action, args, uid)
    elif action in {"manual", "scan"} and not args:
        if action == "manual":
            await set_flow(state, Flow.name, "manual")
            await tell(message, "Введи название кнопки.", keys(CANCEL))
        else:
            await set_flow(state, Flow.photo, "add_photo")
            await tell(message, SCAN_PROMPT, keys(CANCEL))
    elif action == "roles" and len(args) == 1:
        page = number(args[0], 1_000_000)
        admins, more = repo.admins(page)
        buttons = []
        if uid == repo.owner:
            buttons.extend((f"Отозвать {target}", f"ad:revoke:{target}") for target in admins)
            buttons.append(("Назначить администратора", "ad:grant"))
        roles = "\n".join(map(str, admins)) if admins else "назначений на этой странице нет"
        await tell(message, f"Владелец: {repo.owner}\nАдминистраторы · страница {page+1}\n{roles}",
                   pages(buttons, "ad:roles", page, more, ("Назад", "ad:home")))
    elif action == "log" and not args:
        lines = [f"{row['at'][:19]} UTC · {row['actor']} · {row['action']} · {row['subject']}" for row in repo.recent()]
        await tell(message, "\n".join(lines) or "Журнал пока пуст.", keys(("Назад", "ad:home")))
    elif action in {"grant", "revoke"} and uid == repo.owner:
        if action == "grant" and not args:
            await employees.request_person(message, state, Flow.grant, "grant", "Выбрать администратора")
        elif action == "revoke" and len(args) == 1:
            target = number(args[0], 2**52-1)
            employee = repo.employee(target)
            remaining = "Сохранится доступ сотрудника." if employee and employee["active"] else "Доступ к боту прекратится."
            await confirmation(message, state, action, target=target, remaining=remaining)
        else:
            raise CatalogError("Кнопка устарела. Открой /admin.")
    else:
        raise CatalogError("Действие недоступно. Открой /admin.")


async def record_action(message, state, guard, repo, action, args, uid):
    expected = 2 if action in {"toggle", "delete"} else 1
    if len(args) != expected:
        raise CatalogError("Кнопка устарела. Открой /admin.")
    record = repo.get(number(args[0]))
    if record is None:
        raise CatalogError("ШК больше нет в каталоге.")
    if action == "open":
        await card(message, record)
    elif action == "generate":
        await send_png(message, record.name, record.value, guard, keys(("Назад", f"ad:open:{record.id}")))
    elif action in {"rename", "value"}:
        await set_flow(state, Flow.name if action == "rename" else Flow.value, action, old=record, name=record.name)
        prompt = "Введи новое название кнопки." if action == "rename" else f"Текущее значение: {record.value}\nВведи новое значение."
        await tell(message, prompt, keys(CANCEL))
    elif record.revision != number(args[1]):
        raise CatalogError("Запись изменилась. Открой её заново.")
    elif action == "delete":
        await confirmation(message, state, action, old=record)
    else:
        repo.change(uid, record)
        await card(message, repo.get(record.id))


async def confirmation(message, state, kind, **data):
    await set_flow(state, Flow.confirm, kind, **data)
    nonce = (await state.get_data())["nonce"]
    if kind == "delete":
        prompt = f"Удалить «{data['old'].name}» ({data['old'].value})?"
    else:
        action = "Назначить администратора" if kind == "grant" else "Отозвать права"
        prompt = f"{action} для Telegram ID {data['target']}?"
        if kind == "revoke":
            prompt += "\n" + data["remaining"]
    await tell(message, prompt, keys(("Подтвердить", f"flow:ok:{nonce}"), CANCEL))


async def apply_flow(message, state, guard, repo, uid, action, args):
    data = await state.get_data()
    if not args or data.get("nonce") != args[0]:
        raise CatalogError("Кнопка устарела. Начни действие заново.")
    kind = data["kind"]
    if kind != "scan":
        repo.require_admin(uid)
    stage = await state.get_state()
    if action=="staffpage" and kind=="staffresults" and len(args)==2:
        await employees.listing(message,state,repo,uid,data["query"],number(args[1],1000000))
    elif action == "pick" and stage == Flow.choose.state and len(args) == 2:
        index = number(args[1], 15)
        if index >= len(data["codes"]):
            raise CatalogError("Выбери код из списка.")
        await show_detected(message, state, repo, uid, kind, data["codes"][index])
    elif action == "add" and stage == Flow.photo.state and kind == "scan" and "decoded" in data:
        repo.require_admin(uid)
        await show_detected(message, state, repo, uid, "add_photo", data["decoded"])
    elif action == "change" and stage == Flow.confirm.state and kind == "save" and len(args) == 1:
        origin = data["origin"]
        old = data["old"]
        await set_flow(state, Flow.name if origin in {"rename", "add_photo", "manual"} else Flow.value, origin,
                       old=old, name=data["name"], value=data["value"])
        await tell(message, "Введи название кнопки." if origin != "value" else "Введи новое значение.", keys(CANCEL))
    elif action == "ok" and stage == Flow.confirm.state and len(args) == 1:
        if kind == "employee":
            target=data["target"]
            repo.change_employee(uid,target,data["change"],data.get("revision",0),data.get("name",""),data.get("note",""))
            if data["change"]=="disable":
                guard.revoke(target)
                await FSMContext(state.storage,replace(state.key,chat_id=target,user_id=target)).clear()
            await state.clear()
            await tell(message,"Изменение сохранено.",keys(("Карточка сотрудника",f"ad:staffcard:{target}")))
        elif kind == "save":
            record = repo.save(uid, data["name"], data["value"], data["old"])
            await state.clear()
            await tell(message, "ШК сохранён.", keys(("Открыть запись", f"ad:open:{record.id}"), ("Панель", "ad:home")))
        elif kind == "delete":
            repo.change(uid, data["old"], delete=True)
            await state.clear()
            await tell(message, "ШК удалён.", keys(("Каталог ШК", "ad:list:0")))
        elif kind in {"grant", "revoke"}:
            target = data["target"]
            repo.set_admin(uid, target, kind == "grant")
            if kind == "revoke":
                guard.revoke(target)
                revoked = FSMContext(state.storage, replace(state.key, chat_id=target, user_id=target))
                await revoked.clear()
            await state.clear()
            await tell(message, ("Права обновлены." if kind=="grant" else "Роль отозвана. " + ("Сохранён доступ сотрудника." if repo.allowed(target) else "Доступ к боту закрыт.")), keys(("Администраторы", "ad:roles:0")))
    else:
        raise CatalogError("Кнопка устарела. Начни действие заново.")


async def show_detected(message, state, repo, uid, kind, code):
    if kind == "add_photo":
        repo.require_admin(uid)
        try:
            validate_value(code.value)
        except ValueError:
            valid = False
        else:
            valid = code.addable
        if valid:
            existing = repo.by_value(code.value)
            if existing:
                await set_flow(state, Flow.photo, kind)
                await duplicate_notice(message, DuplicateBarcode(existing))
                return
            await set_flow(state, Flow.name, kind, value=code.value)
            await tell(message, f"Формат: {code.format}\nЗначение: {code.value}\nВведи название кнопки.", keys(CANCEL))
            return

    await set_flow(state, Flow.photo, kind, decoded=code)
    buttons = [CANCEL]
    if kind == "scan" and code.addable and repo.is_admin(uid):
        nonce = (await state.get_data())["nonce"]
        buttons.insert(0, ("Добавить в каталог", f"flow:add:{nonce}"))
    value = "".join(char if char.isprintable() or char in "\n\t" else f"\\u{ord(char):04x}" for char in code.value)
    text = f"Формат: {code.format}\nЗначение:\n{value}"
    if kind == "add_photo":
        text += "\n" + CODE_UNSUPPORTED
    for offset in range(0, len(text), 1800):
        await tell(message, text[offset:offset+1800], keys(*buttons) if offset + 1800 >= len(text) else None)


async def form_text(message, state, guard, repo, uid, data, stage):
    kind = data["kind"]
    if kind != "scan":
        repo.require_admin(uid)
    if stage == Flow.staff.state:
        await employees.step(message,state,repo,uid,data)
    elif stage == Flow.name.state and message.text is not None:
        name = validate_name(message.text)
        old = data.get("old")
        if kind in {"rename", "add_photo"}:
            await preview(message, state, guard, repo, data, name, old.value if old else data["value"])
        else:
            await set_flow(state, Flow.value, kind, name=name)
            await tell(message, "Введи значение Code 128. Регистр, пробелы и ведущие нули сохраняются.", keys(CANCEL))
    elif stage == Flow.value.state and message.text is not None:
        value = validate_value(message.text)
        await preview(message, state, guard, repo, data, data["name"], value)
    elif stage == Flow.grant.state:
        if uid != repo.owner:
            raise CatalogDenied("Управлять правами может только владелец.")
        await confirmation(message, state, "grant", target=employees.person_id(message, data))
    else:
        hints = {Flow.photo.state: SCAN_PROMPT, Flow.choose.state: "Выбери значение под результатом сканирования.",
                 Flow.confirm.state: "Используй кнопки под предпросмотром или подтверждением."}
        await tell(message, hints.get(stage, "Отправь текст для текущего шага."), keys(CANCEL))


def register(dispatcher):
    """Подключить сценарии перед универсальными обработчиками текста и callback."""

    async def safe(message, state, operation):
        try:
            await operation()
        except CatalogDenied as error:
            await state.clear()
            await tell(message, str(error))
        except DuplicateBarcode as error:
            await duplicate_notice(message, error)
        except ValueError as error:
            await tell(message, str(error), keys(CANCEL, ("Панель", "ad:home")))
        except sqlite3.Error:
            await tell(message, "Каталог временно недоступен. Повтори действие.")

    @dispatcher.message(Command("admin", "scan", "cancel"))
    async def commands(message: Message, state: FSMContext, guard, repo, command):
        uid = message.from_user.id
        if command.command == "cancel":
            await state.clear()
            guard.light.submit(uid, lambda: tell(message, "Действие отменено.", keys(("Главное меню", "menu:main"))))
            return
        if guard.state(uid) is None or guard.busy(uid):
            return

        async def run():
            if command.command == "admin":
                repo.require_admin(uid)
                await state.clear()
                await panel(message)
            else:
                await set_flow(state, Flow.photo, "scan")
                await tell(message, SCAN_PROMPT, keys(CANCEL))

        guard.light.submit(uid, lambda: safe(message, state, run))

    @dispatcher.callback_query(F.data.startswith(("ad:", "flow:")) | (F.data == "scan:start"))
    async def callback(query, state: FSMContext, guard, repo):
        uid = query.from_user.id
        message = query.message
        if not isinstance(message, Message):
            await query.answer("Кнопка устарела. Открой /menu.")
            return
        if query.data.startswith("ad:") and not repo.is_admin(uid):
            await query.answer("Доступ закрыт.", show_alert=False)
            return
        if query.data == "flow:cancel":
            await state.clear()
            await query.answer("Действие отменено.")
            guard.light.submit(uid, lambda: tell(message, "Действие отменено.", keys(("Главное меню", "menu:main"))))
            return
        if guard.state(uid) is None or guard.busy(uid):
            await query.answer()
            return
        namespace, action, *args = query.data.split(":")
        if namespace == "flow":
            current = await state.get_data()
            if not args or current.get("nonce") != args[0]:
                await query.answer("Кнопка устарела.", show_alert=False)
                return
            if current.get("kind") != "scan" and not repo.is_admin(uid):
                await state.clear()
                await query.answer("Доступ закрыт.", show_alert=False)
                return
        acknowledged = asyncio.Event()

        async def run():
            await acknowledged.wait()
            if namespace == "ad":
                await admin_action(message, state, guard, repo, uid, action, args)
            elif namespace == "scan":
                await set_flow(state, Flow.photo, "scan")
                await tell(message, SCAN_PROMPT, keys(CANCEL))
            else:
                await apply_flow(message, state, guard, repo, uid, action, args)

        async def show_screen():
            runtime = guard.state(uid)
            if runtime is not None:
                runtime.screens.pop(message.message_id, None)
            token = edit_target.set(message)
            try:
                await safe(message, state, run)
            finally:
                edit_target.reset(token)

        if guard.light.submit(uid, show_screen):
            try:
                await query.answer()
            finally:
                acknowledged.set()
        else:
            await query.answer("Бот занят. Попробуй позже.", show_alert=False)

    @dispatcher.message(StateFilter(*Flow.__all_states__))
    async def receive(message: Message, state: FSMContext, guard, repo):
        uid = message.from_user.id
        user = guard.state(uid)
        if user is None or guard.busy(uid):
            return
        data = await state.get_data()
        stage = await state.get_state()
        if not data or stage is None:
            return
        media = message.photo[-1] if message.photo else message.document
        if stage == Flow.photo.state and media and (message.photo or (media.mime_type or "").startswith("image/")):
            await queue_scan(message, state, guard, repo, uid, data, media, user)
        else:
            guard.light.submit(uid, lambda: safe(
                message, state, lambda: form_text(message, state, guard, repo, uid, data, stage),
            ))

    async def queue_scan(message, state, guard, repo, uid, data, media, user):
        if data["kind"] == "add_photo" and not repo.is_admin(uid):
            await state.clear()
            guard.light.submit(uid, lambda: tell(message, "Доступ к админ-панели закрыт."))
            return
        if guard.remaining(user, "scan"):
            if guard.warning(user, "scan", guard.settings.warning_interval):
                guard.light.submit(uid, lambda: tell(message, "Подожди перед повторным сканированием."))
            return
        guard.scans.intersection_update(guard.heavy.keys)
        if len(guard.scans) >= 2:
            if guard.warning(user, "scan", guard.settings.warning_interval):
                guard.light.submit(uid, lambda: tell(message, "Сканер занят. Попробуй чуть позже."))
            return

        async def scan():
            live = await state.get_data()
            if live.get("nonce") != data["nonce"]:
                return
            if data["kind"] == "add_photo":
                repo.require_admin(uid)
            try:
                codes = await scan_telegram(message.bot, media.file_id, media.file_size)
            except ScanError as error:
                codes, problem = [], str(error)
            except (TelegramAPIError, ClientError, asyncio.TimeoutError, OSError):
                codes, problem = [], "Не удалось загрузить или обработать фото. Повтори попытку."
            else:
                problem = SCAN_FAILED
            if (await state.get_data()).get("nonce") != data["nonce"]:
                return
            kind = data["kind"]
            if kind == "add_photo":
                repo.require_admin(uid)
            if not codes:
                await tell(message, problem, keys(CANCEL))
            elif len(codes) == 1:
                await show_detected(message, state, repo, uid, kind, codes[0])
            else:
                await set_flow(state, Flow.choose, kind, codes=tuple(codes))
                nonce = (await state.get_data())["nonce"]
                buttons = [
                    (f"{index+1}. {code.format} · {' '.join(code.value.split())[:35] or '(пусто)'}",
                     f"flow:pick:{nonce}:{index}")
                    for index, code in enumerate(codes)
                ]
                await tell(message, f"Найдено кодов: {len(codes)}. Выбери нужный.", keys(*buttons, CANCEL))

        if guard.heavy.submit(uid, lambda: safe(message, state, scan)):
            guard.scans.add(uid)
            guard.limit(user, "scan", guard.settings.clean_cooldown)
        elif guard.warning(user, "scan", guard.settings.warning_interval):
            guard.light.submit(uid, lambda: tell(message, "Очередь заполнена. Попробуй чуть позже."))
