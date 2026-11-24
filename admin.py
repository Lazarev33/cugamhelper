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


async def cancel_button(state):
    nonce = (await state.get_data()).get("nonce")
    return ("Отмена", f"flow:cancel:{nonce}") if nonce else ("Главное меню", "menu:main")


async def open_scan(message, state, *, guard, keyboard_parent=None, edit=False):
    if not guard.features.enabled("barcode_scan"):
        return
    keyboard_parent = keyboard_parent or ("menu:shk" if guard.features.enabled("barcode_catalog") else "menu:main")
    token = edit_target.set(message) if edit else None
    try:
        await set_flow(state, Flow.photo, "scan")
        await tell(message, SCAN_PROMPT, keys(await cancel_button(state), ("Назад", keyboard_parent)))
    finally:
        if token is not None:
            edit_target.reset(token)


async def panel(message):
    await tell(message, PANEL, keys(
        ("Управление каталогом", "ad:catalog"), ("Пользователи и доступ", "ad:access"),
        ("Журнал изменений", "ad:log"), ("Главное меню", "menu:main"),
    ))


async def card(message, record: Barcode, page=0):
    prefix = f"{record.id}:{record.revision}"
    flag = "активен" if record.active else "выключен"
    await tell(message, (
        f"#{record.id} · {record.name}\n{record.format}: {record.value}\nСтатус: {flag}"
        f"\nСоздан: {record.created_at} · автор: {record.created_by or 'импорт'}"
        f"\nИзменён: {record.updated_at}"
    ), keys(
        ("Сгенерировать ШК", f"ad:generate:{record.id}:{page}"),
        ("Переименовать", f"ad:rename:{record.id}:{page}"), ("Изменить значение", f"ad:value:{record.id}:{page}"),
        ("Выключить" if record.active else "Включить", f"ad:toggle:{prefix}:{page}"),
        ("Удалить", f"ad:delete:{prefix}:{page}"), ("Назад", f"ad:list:{page}"),
    ))


async def duplicate_notice(message, state, error):
    record = error.record
    status = "активен" if record.active else "выключен"
    buttons = [("Открыть существующий ШК", f"ad:open:{record.id}")]
    if not record.active:
        buttons.append(("Восстановить ШК", f"ad:toggle:{record.id}:{record.revision}"))
    await tell(message, f"{error}\n#{record.id} · {record.name}\n{record.format}: «{record.value}»\nСтатус: {status}",
               keys(*buttons, await cancel_button(state)))


async def preview(message, state, guard, repo, data, name, value):
    old = data.get("old")
    duplicate = repo.by_value(value)
    if duplicate and (old is None or duplicate.id != old.id):
        await duplicate_notice(message, state, DuplicateBarcode(duplicate))
        return
    await set_flow(state, Flow.confirm, "save", old=old, name=name, value=value, origin=data["kind"], page=data.get("page", 0))
    nonce = (await state.get_data())["nonce"]
    comparison = f"\nБыло: «{old.value}»\nБудет: «{value}»" if old and old.value != value else f"\nCode 128: «{value}»"
    await send_png(message, name, value, guard, keys(
        ("Сохранить", f"flow:ok:{nonce}"), ("Изменить", f"flow:change:{nonce}"), await cancel_button(state),
    ), caption=f"Предпросмотр · {name}{comparison}")


async def admin_action(message, state, guard, repo, uid, action, args):
    repo.require_admin(uid)
    await state.clear()
    if action.startswith("staff") and (not args and action not in {"staff","staffadd","staffsearch"}):
        raise CatalogError("Некорректная кнопка.")
    if action == "home" and not args:
        await panel(message)
    elif action == "catalog" and not args:
        await tell(message, "Управление каталогом ШК", keys(
            ("Каталог ШК", "ad:list:0"), ("Добавить вручную", "ad:manual"),
            ("Добавить по фото", "ad:scan"), ("Назад", "ad:home"),
        ))
    elif action == "access" and not args:
        await tell(message, "Пользователи и доступ", keys(
            ("Доверенные пользователи", "ad:staff"), ("Администраторы", "ad:roles:0"), ("Назад", "ad:home"),
        ))
    elif action.startswith("staff"):
        await employees.screen(message,state,guard,repo,uid,action,args)
    elif action == "list" and len(args) == 1:
        page = number(args[0], 1_000_000)
        records, more = repo.page(page)
        buttons = [(f"{'✓' if item.active else '○'} {item.name}", f"ad:open:{item.id}:{page}") for item in records]
        await tell(message, f"Каталог ШК · страница {page+1}", pages(buttons, "ad:list", page, more, ("Назад", "ad:catalog")))
    elif action in {"open", "generate", "rename", "value", "toggle", "delete"}:
        await record_action(message, state, guard, repo, action, args, uid)
    elif action in {"manual", "scan"} and not args:
        if action == "manual":
            await set_flow(state, Flow.name, "manual")
            await tell(message, "Введи название кнопки.", keys(await cancel_button(state)))
        else:
            await set_flow(state, Flow.photo, "add_photo")
            await tell(message, SCAN_PROMPT, keys(await cancel_button(state)))
    elif action == "roles" and len(args) == 1:
        page = number(args[0], 1_000_000)
        admins, more = repo.admins(page)
        buttons = []
        if uid == repo.owner:
            buttons.extend((f"Отозвать {target}", f"ad:revoke:{target}") for target in admins)
            buttons.append(("Назначить администратора", "ad:grant"))
        roles = "\n".join(map(str, admins)) if admins else "назначений на этой странице нет"
        await tell(message, f"Владелец: {repo.owner}\nАдминистраторы · страница {page+1}\n{roles}",
                   pages(buttons, "ad:roles", page, more, ("Назад", "ad:access")))
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
    if len(args) not in (expected, expected+1):
        raise CatalogError("Кнопка устарела. Открой /admin.")
    page = number(args[expected], 1_000_000) if len(args) > expected else 0
    record = repo.get(number(args[0]))
    if record is None:
        raise CatalogError("ШК больше нет в каталоге.")
    if action == "open":
        await card(message, record, page)
    elif action == "generate":
        await send_png(message, record.name, record.value, guard, keys(("Назад", f"ad:open:{record.id}:{page}")))
    elif action in {"rename", "value"}:
        await set_flow(state, Flow.name if action == "rename" else Flow.value, action, old=record, name=record.name, page=page)
        prompt = "Введи новое название кнопки." if action == "rename" else f"Текущее значение: {record.value}\nВведи новое значение."
        await tell(message, prompt, keys(await cancel_button(state)))
    elif record.revision != number(args[1]):
        raise CatalogError("Запись изменилась. Открой её заново.")
    elif action == "delete":
        await confirmation(message, state, action, old=record, page=page)
    else:
        repo.change(uid, record)
        await card(message, repo.get(record.id), page)


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
    await tell(message, prompt, keys(("Подтвердить", f"flow:ok:{nonce}"), await cancel_button(state)))


async def apply_flow(message, state, guard, repo, uid, action, args):
    data = await state.get_data()
    if not args or data.get("nonce") != args[0]:
        raise CatalogError("Кнопка устарела. Начни действие заново.")
    kind = data["kind"]
    if kind == "scan" and not guard.features.enabled("barcode_scan"):
        await state.clear()
        raise ValueError("Сканирование ШК отключено.")
    if kind != "scan":
        repo.require_admin(uid)
    stage = await state.get_state()
    if action == "pick" and stage == Flow.choose.state and len(args) == 2:
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
                       old=old, name=data["name"], value=data["value"], page=data.get("page", 0))
        await tell(message, "Введи название кнопки." if origin != "value" else "Введи новое значение.", keys(await cancel_button(state)))
    elif action == "ok" and stage == Flow.confirm.state and len(args) == 1:
        if kind == "employee":
            target=data["target"]
            repo.change_employee(uid,target,data["change"],data.get("revision",0),data.get("name",""),data.get("note",""))
            if data["change"]=="disable":
                guard.revoke(target)
                await FSMContext(state.storage,replace(state.key,chat_id=target,user_id=target)).clear()
            await state.clear()
            view = f":{data['view']}" if data.get("view") else ""
            await tell(message,"Изменение сохранено.",keys(("Карточка сотрудника",f"ad:staffcard:{target}{view}")))
        elif kind == "save":
            record = repo.save(uid, data["name"], data["value"], data["old"])
            await state.clear()
            await tell(message, "ШК сохранён.", keys(("Открыть запись", f"ad:open:{record.id}:{data.get('page', 0)}"),
                                                    ("Панель", "ad:home")))
        elif kind == "delete":
            repo.change(uid, data["old"], delete=True)
            await state.clear()
            await tell(message, "ШК удалён.", keys(("Каталог ШК", f"ad:list:{data.get('page', 0)}")))
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
                await duplicate_notice(message, state, DuplicateBarcode(existing))
                return
            await set_flow(state, Flow.name, kind, value=code.value)
            await tell(message, f"Формат: {code.format}\nЗначение: {code.value}\nВведи название кнопки.", keys(await cancel_button(state)))
            return

    buttons = []
    if kind == "scan" and code.addable and repo.is_admin(uid):
        await set_flow(state, Flow.photo, kind, decoded=code)
        nonce = (await state.get_data())["nonce"]
        buttons.append(("Добавить в каталог", f"flow:add:{nonce}"))
    else:
        await set_flow(state, Flow.photo, kind)
    buttons.append(await cancel_button(state))
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
        await employees.step(message,state,guard,repo,uid,data)
    elif stage == Flow.name.state and message.text is not None:
        name = validate_name(message.text)
        old = data.get("old")
        if kind in {"rename", "add_photo"}:
            await preview(message, state, guard, repo, data, name, old.value if old else data["value"])
        else:
            await set_flow(state, Flow.value, kind, name=name)
            await tell(message, "Введи значение Code 128. Регистр, пробелы и ведущие нули сохраняются.", keys(await cancel_button(state)))
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
        await tell(message, hints.get(stage, "Отправь текст для текущего шага."), keys(await cancel_button(state)))


def register(dispatcher):
    """Подключить сценарии перед универсальными обработчиками текста и callback."""

    async def safe(message, state, operation):
        try:
            await operation()
        except CatalogDenied as error:
            await state.clear()
            await tell(message, str(error))
        except DuplicateBarcode as error:
            await duplicate_notice(message, state, error)
        except ValueError as error:
            await tell(message, str(error), keys(await cancel_button(state), ("Панель", "ad:home")))
        except sqlite3.Error:
            await tell(message, "Каталог временно недоступен. Повтори действие.")

    @dispatcher.message(Command("admin", "scan", "cancel"))
    async def commands(message: Message, state: FSMContext, guard, repo, command):
        uid = message.from_user.id
        if command.command == "cancel":
            await guard.cancel_operation(uid, state)
            guard.service(uid, lambda: tell(message, "Действие отменено.", keys(("Главное меню", "menu:main"))),
                          priority=True, essential=True)
            return
        if guard.state(uid) is None or guard.busy(uid):
            return

        async def run():
            if command.command == "admin":
                repo.require_admin(uid)
                await state.clear()
                await panel(message)
            elif guard.features.enabled("barcode_scan"):
                await open_scan(message, state, guard=guard)
            else:
                await tell(message, "Сканирование ШК отключено. Открой /menu.")

        guard.service(uid, lambda: safe(message, state, run), priority=repo.is_admin(uid))

    @dispatcher.callback_query(F.data.startswith(("ad:", "flow:")) | (F.data == "scan:start"))
    async def callback(query, state: FSMContext, guard, repo):
        uid = query.from_user.id
        message = query.message
        if not isinstance(message, Message):
            await query.answer("Кнопка устарела. Открой /menu.")
            return
        if query.data == "scan:start" and not guard.features.enabled("barcode_scan"):
            await query.answer("Сканирование ШК отключено.")
            return
        if query.data.startswith("ad:") and not repo.is_admin(uid):
            await query.answer("Доступ закрыт.", show_alert=False)
            return
        if query.data == "flow:cancel" or query.data.startswith("flow:cancel:"):
            nonce = (await state.get_data()).get("nonce")
            if not nonce or query.data != f"flow:cancel:{nonce}":
                await query.answer("Кнопка отмены устарела.")
                return
            await guard.cancel_operation(uid, state)
            await query.answer("Действие отменено.")
            guard.service(uid, lambda: tell(message, "Действие отменено.", keys(("Главное меню", "menu:main"))),
                          priority=True, essential=True)
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
                await open_scan(message, state, guard=guard)
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

        if guard.service(uid, show_screen, priority=repo.is_admin(uid)):
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
            guard.service(uid, lambda: safe(
                message, state, lambda: form_text(message, state, guard, repo, uid, data, stage),
            ), priority=repo.is_admin(uid))

    async def queue_scan(message, state, guard, repo, uid, data, media, user):
        if data["kind"] == "add_photo" and not repo.is_admin(uid):
            await state.clear()
            guard.service(uid, lambda: tell(message, "Доступ к админ-панели закрыт."))
            return
        if guard.remaining(user, "heavy"):
            if guard.warning(user, "scan", guard.settings.warning_interval):
                guard.service(uid, lambda: tell(message, "Подожди перед повторным сканированием."))
            return
        guard.scans.intersection_update(guard.heavy.keys)
        if len(guard.scans) >= 2:
            if guard.warning(user, "scan", guard.settings.warning_interval):
                guard.service(uid, lambda: tell(message, "Сканер занят. Попробуй чуть позже."))
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
            if kind == "scan" and not guard.features.enabled("barcode_scan"):
                await state.clear()
                return
            if not codes:
                await tell(message, problem, keys(await cancel_button(state)))
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
                await tell(message, f"Найдено кодов: {len(codes)}. Выбери нужный.", keys(*buttons, await cancel_button(state)))

        def permitted(features):
            return data["kind"] == "add_photo" or features.enabled("barcode_scan")

        if guard.submit_feature(uid, lambda: safe(message, state, scan), permitted, on_disabled=state.clear):
            guard.scans.add(uid)
            guard.limit(user, "heavy", guard.settings.clean_cooldown)
        elif guard.warning(user, "scan", guard.settings.warning_interval):
            guard.service(uid, lambda: tell(message, "Очередь заполнена. Попробуй чуть позже."))
