"""Telegram-меню, выдача штрихкодов и очистка списков номеров."""

import asyncio
import logging
import os
from pathlib import Path
import socket

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import (
    TelegramAPIError, TelegramBadRequest, TelegramNetworkError,
)
from aiogram.enums import ChatType
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.methods import GetUpdates
from aiogram.types import (
    BotCommand, CallbackQuery, ErrorEvent, InlineKeyboardButton, Message, Update,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

from warehouse_bot import register as register_data
from admin import number, register as register_workflows, send_png, keys, open_scan
from catalog import Catalog, CatalogError
from dialogs import DialogStorage
from branding import BOT_NAME
from parser import clean_numbers, chunks
from private_process import disable_core_dumps
from protection import Protection, UserState
from settings import load_settings
from features import DEFAULT_FEATURES
from infrastructure import Infrastructure, IntegrationFeatures
from infrastructure_bot import register as register_infrastructure
from telegram_bridge import listen


dp = Dispatcher(storage=DialogStorage())
register_infrastructure(dp)
MAIN_TEXT = f"{BOT_NAME}\nВыбери рабочий инструмент."
CLEAN_TEXT = (
    "Очистка номеров\nОтправь номера по одному в строке. Можно добавить дату, например 26008178-03.11.2026. "
    "Отчёт и готовый список придут отдельно."
)
UNAVAILABLE = "Инструмент отключён на сервере. Открой /menu."


def help_text(features):
    lines = ["Помощь"]
    if features.enabled("barcode_catalog"):
        lines.append("ШК · каталог и выдача складских кодов.")
    if features.enabled("barcode_scan"):
        lines.append("ШК · отправь фото для распознавания. Прямая команда /scan.")
    if features.permits("clean"):
        lines.append("Очистка номеров · отправь список с датами или без дат.")
    if features.data:
        lines.append("Обработка данных · выбери операцию в меню или пришли входные данные в личный чат.")
    if not (features.barcodes or features.permits("clean") or features.data):
        lines.append("Рабочие инструменты сейчас выключены. По вопросам доступа обращайся к администратору.")
    lines.append("/menu — главное меню · /cancel — отменить текущую операцию.")
    return "\n\n".join(lines)


def menu_keys(admin=False, features=DEFAULT_FEATURES) -> InlineKeyboardBuilder:
    keyboard = InlineKeyboardBuilder()
    if features.barcodes:
        keyboard.button(text="ШК", callback_data="menu:shk")
    if features.permits("clean"):
        keyboard.button(text="Очистка номеров", callback_data="menu:clean")
    if features.data:
        keyboard.button(text="Обработка данных", callback_data="data:home")
    if admin:
        keyboard.button(text="Админ-панель", callback_data="ad:home")
    keyboard.button(text="Помощь", callback_data="menu:help")
    keyboard.adjust(1)
    return keyboard


def shk_keys() -> InlineKeyboardBuilder:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="Каталог ШК", callback_data="menu:barcodes")
    keyboard.button(text="Сканировать ШК", callback_data="scan:start")
    keyboard.button(text="Главное меню", callback_data="menu:main")
    keyboard.adjust(1)
    return keyboard


def section_keys(repo: Catalog | None = None, page: int = 0, *, features=DEFAULT_FEATURES) -> InlineKeyboardBuilder:
    keyboard = InlineKeyboardBuilder()
    if repo is not None:
        records, more = repo.page(page, active=True)
        row = []
        for record in records:
            button = InlineKeyboardButton(text=record.name, callback_data=f"code:{record.id}:{page}")
            if len(record.name) > 14:
                if row:
                    keyboard.row(*row)
                    row = []
                keyboard.row(button)
            else:
                row.append(button)
                if len(row) == 2:
                    keyboard.row(*row)
                    row = []
        if row:
            keyboard.row(*row)
        arrows = []
        if page:
            arrows.append(InlineKeyboardButton(text="‹ Ранее", callback_data=f"catalog:{page-1}"))
        if more:
            arrows.append(InlineKeyboardButton(text="Далее ›", callback_data=f"catalog:{page+1}"))
        if arrows:
            keyboard.row(*arrows)
    if repo is not None and features.enabled("barcode_scan"):
        keyboard.row(InlineKeyboardButton(text="Назад", callback_data="menu:shk"))
    keyboard.row(InlineKeyboardButton(text="Главное меню", callback_data="menu:main"))
    return keyboard


@dp.update.outer_middleware()
async def restrict_updates(handler, event: Update, data: dict):
    guard: Protection = data["guard"]
    query = event.callback_query
    message = query.message if query else event.message
    user = query.from_user if query else message.from_user if message else None
    if not message or message.chat.type != ChatType.PRIVATE or not user:
        if query:
            await query.answer()
        return
    if not data["repo"].allowed(user.id):
        if not query and not user.is_bot and message.text and message.text.split(maxsplit=1)[:1] == ["/start"]:
            guard.remember_visitor(data["repo"], user)
        if query:
            await query.answer()
        return
    if guard.duplicate(event.update_id):
        return
    guard.sync_features(dp.storage)
    if query:
        name = query.data or ""
        policy = guard.features
        if (name.startswith(("barcode:", "code:", "catalog:")) or name == "menu:barcodes") and not policy.enabled("barcode_catalog"):
            await query.answer(UNAVAILABLE)
            return
        if name == "menu:clean" and not policy.permits("clean"):
            await query.answer(UNAVAILABLE)
            return
        if name == "menu:shk" and not policy.barcodes:
            await query.answer(UNAVAILABLE)
            return
    task = asyncio.current_task()
    handlers = guard.handlers.setdefault(user.id, set())
    handlers.add(task)
    try:
        return await handler(event, data)
    finally:
        handlers.discard(task)
        if not handlers:
            guard.handlers.pop(user.id, None)


def queue_menu(message: Message, guard: Protection, title: str, keyboard: InlineKeyboardBuilder,
               *, dialog=None, priority=False) -> None:
    uid = message.from_user.id
    state = guard.state(uid)
    if state is None or guard.busy(uid) or guard.remaining(state, "command"):
        return
    async def show():
        if dialog is not None:
            await dialog.clear()
        await message.answer(title, reply_markup=keyboard.as_markup())

    if guard.service(uid, show, priority=priority):
        guard.limit(state, "command", guard.settings.chat_interval)


@dp.message(Command("myid"))
async def my_id(message: Message, guard: Protection):
    queue_menu(message, guard, f"Твой Telegram ID: {message.from_user.id}", section_keys())


@dp.message(CommandStart())
@dp.message(Command("menu"))
async def start(message: Message, guard: Protection, state: FSMContext, repo: Catalog) -> None:
    uid = message.from_user.id
    if ticket := guard.heavy.ticket(uid):
        show_operation_menu(message, guard, uid, ticket)
        return
    queue_menu(message, guard, MAIN_TEXT, menu_keys(repo.is_admin(uid), guard.features), dialog=state, priority=repo.is_admin(uid))


@dp.message(Command("help"))
async def help_command(message: Message, guard: Protection, state: FSMContext) -> None:
    queue_menu(message, guard, help_text(guard.features), section_keys(), dialog=state)


def show_operation_menu(message, guard, uid, ticket):
    markup = keys(
        ("Продолжить операцию", f"work:keep:{ticket}"),
        ("Отменить и открыть меню", f"work:menu:{ticket}"),
    )
    async def show():
        if guard.heavy.ticket(uid) == ticket:
            await message.answer("Операция ещё выполняется. Что сделать?", reply_markup=markup)
    return guard.service(uid, show, control=True)


@dp.callback_query(F.data.startswith("work:"))
async def operation_navigation(query: CallbackQuery, guard: Protection, state: FSMContext, repo: Catalog):
    uid = query.from_user.id
    _, action, ticket = (query.data.split(":") + ["", ""])[:3]
    if ticket != guard.heavy.ticket(uid) or action not in {"keep", "menu"}:
        await query.answer("Операция уже завершилась. Открой /menu.")
        return
    if action == "keep":
        await query.answer("Операция продолжается.")
        return
    await guard.cancel_operation(uid, state)
    await query.answer("Операция отменена.")
    if isinstance(query.message, Message):
        guard.service(uid, lambda: query.message.answer(MAIN_TEXT, reply_markup=menu_keys(repo.is_admin(uid), guard.features).as_markup()),
                      priority=True, essential=True)


@dp.callback_query(F.data.in_({
    "menu:shk", "menu:barcodes", "menu:clean", "menu:help", "menu:main", "menu:back",
}) | F.data.startswith("catalog:"))
async def navigate(query: CallbackQuery, repo: Catalog, guard: Protection, state: FSMContext) -> None:
    message = query.message
    uid = query.from_user.id
    runtime = guard.state(uid)
    if not isinstance(message, Message):
        text = "Меню устарело. Открой /menu." if runtime and guard.warning(runtime, "stale", 2) else None
        await query.answer(text, show_alert=False)
        return
    if query.data in {"menu:main", "menu:back"} and (ticket := guard.heavy.ticket(uid)):
        offered = show_operation_menu(message, guard, uid, ticket)
        await query.answer(None if offered else "Операция ещё выполняется.")
        return
    if guard.heavy.ticket(uid):
        await query.answer("Операция ещё выполняется.")
        return
    await query.answer()
    if runtime is None or guard.heavy.ticket(uid):
        return
    await state.clear()
    screen = "menu:main" if query.data == "menu:back" else query.data

    policy = guard.features
    if screen == "menu:shk" and policy.enabled("barcode_catalog") and policy.enabled("barcode_scan"):
        title, keyboard = "ШК\nВыбери действие.", shk_keys()
    elif screen == "menu:shk" and policy.enabled("barcode_scan"):
        guard.service(uid, lambda: open_scan(message, state, keyboard_parent="menu:main", edit=True, guard=guard))
        return
    elif screen in {"menu:shk", "menu:barcodes"} or screen.startswith("catalog:"):
        if not policy.enabled("barcode_catalog"):
            return
        try:
            page = number(screen.partition(":")[2], 1_000_000) if screen.startswith("catalog:") else 0
        except CatalogError:
            return
        title = f"Каталог ШК · страница {page+1}"
        keyboard = section_keys(repo, page, features=policy)
    elif screen == "menu:clean":
        if not policy.permits("clean"):
            return
        await state.set_state("clean")
        title, keyboard = CLEAN_TEXT, section_keys()
    elif screen == "menu:help":
        title, keyboard = help_text(policy), section_keys()
    else:
        title, keyboard = MAIN_TEXT, menu_keys(repo.is_admin(uid), guard.features)
    markup = keyboard.as_markup()
    screen_key = (title, markup.model_dump_json())
    if runtime.screens.get(message.message_id) == screen_key:
        return
    if message.message_id not in runtime.screens and message.text == title and message.reply_markup == markup:
        return

    async def show():
        await message.answer(title, reply_markup=markup)
        remember_screen(runtime, message.message_id, screen_key)

    async def edit():
        try:
            await message.edit_text(title, reply_markup=markup)
        except TelegramBadRequest as error:
            reason = error.message.lower()
            if "message is not modified" not in reason:
                if any(text in reason for text in (
                    "message can't be edited", "message can not be edited",
                    "message to edit not found", "no text in the message",
                )):
                    if not guard.busy(uid):
                        guard.service(uid, show, priority=repo.is_admin(uid))
                    return
                raise
        remember_screen(runtime, message.message_id, screen_key)

    if message.text is None:
        if not guard.busy(uid):
            guard.service(uid, show, priority=repo.is_admin(uid))
    else:
        guard.edits.submit(uid, edit)


def remember_screen(state: UserState, message_id: int, screen: tuple[str, str]) -> None:
    state.screens[message_id] = screen
    state.screens.move_to_end(message_id)
    if len(state.screens) > 8:
        state.screens.popitem(last=False)


@dp.callback_query(F.data.startswith(("barcode:", "code:")))
async def send_barcode(query: CallbackQuery, repo: Catalog, guard: Protection) -> None:
    uid = query.from_user.id
    state = guard.state(uid)
    if state is None:
        await query.answer()
        return
    page = 0
    try:
        if query.data.startswith("code:"):
            _, record_id, page_id = query.data.split(":")
            record = repo.get(number(record_id))
            page = number(page_id, 1_000_000)
        else:
            # Старые кнопки v2.3.0 работали со значением вместо внутреннего ID.
            record = repo.by_value(query.data.removeprefix("barcode:"))
    except ValueError:
        record = None
    if record is None or not record.active or not isinstance(query.message, Message):
        text = "Код недоступен. Открой /menu." if guard.warning(state, "stale", 2) else None
        await query.answer(text, show_alert=False)
        return

    remaining = guard.remaining(state, "barcode")
    if remaining or guard.busy(uid):
        guard.note("частый или параллельный запрос ШК")
        text = None
        if guard.warning(state, "barcode", guard.settings.barcode_cooldown):
            text = (
                f"Не спамь! Подожди {remaining} сек." if remaining
                else "Дождись завершения предыдущего запроса."
            )
        await query.answer(text, show_alert=False)
        return

    acknowledged = asyncio.Event()
    message = query.message

    async def deliver():
        await acknowledged.wait()
        current = repo.get(record.id)
        if current and current.active:
            await send_png(message, current.name, current.value, guard,
                           keys(("К каталогу ШК", f"catalog:{page}"), ("Главное меню", "menu:main")))
        else:
            await message.answer("ШК больше недоступен. Открой каталог заново.")

    if not guard.submit_feature(uid, deliver, lambda f: f.enabled("barcode_catalog")):
        guard.note("очередь тяжёлых операций заполнена")
        text = "Бот занят. Попробуй чуть позже." if guard.warning(state, "busy", 2) else None
        await query.answer(text, show_alert=False)
        return
    guard.limit(state, "barcode", guard.settings.barcode_cooldown)
    try:
        await query.answer()
    finally:
        acknowledged.set()


register_workflows(dp)
register_data(dp, lambda message, guard, uid, text: clean_input(message, guard, uid, text))


@dp.callback_query()
async def unknown_callback(query: CallbackQuery, guard: Protection) -> None:
    state = guard.state(query.from_user.id)
    text = "Кнопка устарела. Открой /menu." if state and guard.warning(state, "stale", 2) else None
    await query.answer(text, show_alert=False)


async def send_plain(message: Message, text: str) -> Message:
    return await message.answer(text, parse_mode=None)


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message, guard: Protection) -> None:
    await clean_input(message, guard, message.from_user.id, message.text)


async def clean_input(message, guard, uid, text):
    if not guard.features.permits("clean", "text"):
        guard.service(uid, lambda: send_plain(message, UNAVAILABLE))
        return
    state = guard.state(uid)
    if state is None:
        return
    remaining = guard.remaining(state, "heavy")
    if guard.busy(uid):
        guard.note("параллельная операция одного пользователя")
        return
    if remaining:
        guard.note("частый запрос очистки")
        if guard.warning(state, "clean", guard.settings.warning_interval):
            guard.service(uid, lambda: send_plain(message, f"Слишком частые запросы. Подожди {remaining} сек."))
        return
    if guard.submit_feature(uid, lambda: process_numbers(message, text, guard), lambda f: f.permits("clean", "text")):
        guard.limit(state, "heavy", guard.settings.clean_cooldown)
    else:
        guard.note("очередь тяжёлых операций заполнена")
        if guard.warning(state, "clean", guard.settings.warning_interval):
            guard.service(uid, lambda: send_plain(message, "Бот занят. Попробуй чуть позже."))

async def process_numbers(message: Message, text: str, guard: Protection) -> None:
    settings = guard.settings
    if len(text) > settings.max_input_chars or len(text.splitlines()) > settings.max_input_lines:
        await send_plain(message, (
            f"Список слишком большой. Максимум {settings.max_input_chars} символов и "
            f"{settings.max_input_lines} строк. Раздели список и отправь заново."
        ))
        return
    identifiers, invalid_lines = clean_numbers(text)
    if invalid_lines:
        lines = [f"Строка {number}" for number in invalid_lines]
        notice = "Список не обработан. Исправь указанные строки и отправь его заново.\n"
        notice += "Ожидаемый формат: 26008178-03.11.2026\n\n"
        details = chunks(lines, limit=4096 - len(notice))
        await send_plain(message, notice + details[0])
        for detail in details[1:]:
            await send_plain(message, detail)
        return
    if not identifiers:
        await message.answer(CLEAN_TEXT, reply_markup=section_keys().as_markup())
        return

    try:
        parts = chunks(identifiers)
    except ValueError:
        await send_plain(
            message,
            "Список не обработан. Один из номеров длиннее лимита Telegram в 4096 символов. "
            "Проверь длину номера и отправь список заново.",
        )
        return
    report_text = f"Обработано: {len(identifiers)}\nОшибок: 0\nПотерь: 0"
    if len(parts) > 1:
        report_text += f"\nЧастей: {len(parts)}"
    report = await send_plain(message, report_text)
    for index, part in enumerate(parts):
        try:
            await send_plain(message, part)
        except TelegramAPIError as error:
            guard.note(f"сбой отправки результата ({type(error).__name__})")
            await report.edit_text(
                f"Обработано: {len(identifiers)}\n"
                f"Подтверждена отправка частей: {index} из {len(parts)}.\n"
                "Telegram не подтвердил полную доставку. Отправь исходный список заново.",
                parse_mode=None,
            )
            return


@dp.error()
async def handle_update_error(event: ErrorEvent) -> bool:
    # Ошибка Telegram может содержать исходный запрос с рабочими данными.
    return True


async def main() -> None:
    disable_core_dumps()
    load_dotenv(Path(__file__).with_name(".env"))
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Не задан BOT_TOKEN. На VPS используй sudo numberbot token.")
    try:
        settings = load_settings()
        dp.storage.data_ttl = settings.data_ttl
        location = Path(os.getenv("STATE_DIRECTORY") or Path(__file__).with_name(".state"))
        repo = Catalog(location / "catalog.sqlite3", settings.owner_telegram_id, legacy_ids=settings.allowed_user_ids)
    except (OSError, ValueError) as error:
        raise SystemExit(f"Ошибка конфигурации бота: {error}") from None

    notify_address = os.getenv("NOTIFY_SOCKET")
    async with Bot(token=token) as bot:
        @bot.session.middleware()
        async def notify_polling_ready(make_request, client, method):
            nonlocal notify_address
            try:
                result = await make_request(client, method)
            except Exception as error:
                if isinstance(method, GetUpdates) or isinstance(error, TelegramNetworkError):
                    # aiogram выводит ошибки опроса целиком, включая тело ответа при сбое декодирования.
                    raise TelegramNetworkError(
                        method=method, message=f"Сбой обмена с Telegram ({type(error).__name__})",
                    ) from None
                raise
            if notify_address and isinstance(method, GetUpdates):
                address = notify_address
                if address.startswith("@"):
                    address = "\0" + address[1:]
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
                    notifier.sendto(b"READY=1", address)
                notify_address = None
            return result

        # Получение профиля проверяет токен до запуска цикла polling.
        profile = await bot.me()
        infrastructure = Infrastructure(location / "catalog.sqlite3")
        infrastructure.prepare()
        with infrastructure.repository() as data, data.transaction():
            infrastructure.put_meta(data, "bot_username", profile.username or "")
        for lang in (None, "ru"):
            try:
                await bot.set_my_name(name=BOT_NAME, language_code=lang, request_timeout=5)
            except TelegramAPIError as error:
                logging.warning("Не удалось задать имя бота (%s): %s", lang, type(error).__name__)
        try:
            await bot.set_my_commands([
                BotCommand(command="start", description="Запустить бота"),
                BotCommand(command="menu", description="Главное меню"),
                BotCommand(command="help", description="Помощь"),
                BotCommand(command="cancel", description="Отменить действие"),
                BotCommand(command="devices", description="Подключённые устройства"),
            ], request_timeout=5)
        except TelegramAPIError as error:
            logging.warning("Не удалось задать команды бота: %s", type(error).__name__)
        guard = Protection(settings, feature_store=IntegrationFeatures(infrastructure).telegram())
        guard.outgoing.allowed = repo.allowed
        if repo.owner is None:
            logging.warning("Доступ закрыт. Назначь владельца через sudo numberbot owner.")
        bot.session.middleware.register(guard.outgoing)
        bridge = await listen(infrastructure, bot, guard.feature_store)

        async def watch_features():
            seq = repo.db.execute("SELECT coalesce(max(seq),0) FROM infra_revocations").fetchone()[0]
            while True:
                await asyncio.sleep(1)
                guard.sync_features(dp.storage)
                keys = tuple(dp.storage.storage)
                users = set(guard.users) | set(guard.handlers) | {key.user_id for key in keys}
                changes = repo.db.execute("SELECT seq,uid FROM infra_revocations WHERE seq>? ORDER BY seq", (seq,)).fetchall()
                revoked = {change["uid"] for change in changes}
                if changes:
                    seq = changes[-1]["seq"]
                for uid in users:
                    if uid in revoked or not repo.allowed(uid):
                        guard.revoke(uid)
                        for key in keys:
                            if key.user_id == uid:
                                dp.storage.remove(key)

        watcher = asyncio.create_task(watch_features())
        try:
            await dp.start_polling(
                bot, repo=repo, guard=guard, infrastructure=infrastructure, close_bot_session=False,
                tasks_concurrency_limit=32, allowed_updates=["message", "callback_query"],
            )
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            if bridge:
                await bridge.cleanup()
            await guard.close()
            repo.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("aiogram.event").disabled = True
    logging.getLogger("protection").disabled = True
    try:
        asyncio.run(main())
    except Exception as error:
        logging.error("Запуск бота завершился ошибкой: %s", type(error).__name__)
        raise SystemExit(1) from None
