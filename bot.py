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
from aiogram.methods import GetUpdates
from aiogram.types import (
    BotCommand, BufferedInputFile, CallbackQuery, ErrorEvent, InlineKeyboardButton, Message, Update,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

from barcodes import load_catalog
from branding import BOT_NAME
from parser import clean_numbers, chunks
from protection import Protection, UserState
from settings import load_settings


dp = Dispatcher(disable_fsm=True)
MAIN_TEXT = f"Я — {BOT_NAME}. Выбери действие."
CLEAN_TEXT = (
    "Отправь список в этом чате, по одной строке на номер. Пример:\n\n"
    "26008178-03.11.2026\n"
    "26008179-03.11.2026\n\n"
    "Сначала придёт отчёт, затем — номера для копирования. "
    "Уже очищенные номера тоже принимаются."
)
HELP_TEXT = (
    "ШК — выбери складской код и открой изображение для сканирования.\n"
    "Очистка номеров — отправь список номеров с датами или без дат. "
    "Исправь указанные строки, если бот сообщит об ошибках.\n\n"
    "/menu — главное меню."
)


def menu_keys() -> InlineKeyboardBuilder:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="ШК", callback_data="menu:barcodes")
    keyboard.button(text="Очистка номеров", callback_data="menu:clean")
    keyboard.button(text="Помощь", callback_data="menu:help")
    keyboard.adjust(1)
    return keyboard


def section_keys(codes=()) -> InlineKeyboardBuilder:
    keyboard = InlineKeyboardBuilder()
    for code in codes:
        keyboard.button(text=code, callback_data=f"barcode:{code}")
    keyboard.adjust(2)
    keyboard.row(
        InlineKeyboardButton(text="Назад", callback_data="menu:back"),
        InlineKeyboardButton(text="Главное меню", callback_data="menu:main"),
    )
    return keyboard


@dp.update.outer_middleware()
async def restrict_updates(handler, event: Update, data: dict):
    guard: Protection = data["guard"]
    if guard.duplicate(event.update_id):
        return
    query = event.callback_query
    message = query.message if query else event.message
    user = query.from_user if query else message.from_user if message else None
    if not message or message.chat.type != ChatType.PRIVATE or not user:
        if query:
            await query.answer()
        return
    allowed = guard.settings.allowed_user_ids
    if allowed is not None and user.id not in allowed:
        if query:
            await query.answer()
        return
    return await handler(event, data)


def queue_menu(message: Message, guard: Protection, title: str, keyboard: InlineKeyboardBuilder) -> None:
    uid = message.from_user.id
    state = guard.state(uid)
    if state is None or guard.busy(uid) or guard.remaining(state, "command"):
        return
    if guard.light.submit(uid, lambda: message.answer(title, reply_markup=keyboard.as_markup())):
        guard.limit(state, "command", guard.settings.chat_interval)


@dp.message(CommandStart())
@dp.message(Command("menu"))
async def start(message: Message, guard: Protection) -> None:
    queue_menu(message, guard, MAIN_TEXT, menu_keys())


@dp.message(Command("help"))
async def help_command(message: Message, guard: Protection) -> None:
    queue_menu(message, guard, HELP_TEXT, section_keys())


@dp.callback_query(F.data.in_({
    "menu:barcodes", "menu:clean", "menu:help", "menu:main", "menu:back",
}))
async def navigate(query: CallbackQuery, catalog: dict[str, bytes], guard: Protection) -> None:
    message = query.message
    uid = query.from_user.id
    state = guard.state(uid)
    if not isinstance(message, Message):
        text = "Меню устарело. Открой /menu." if state and guard.warning(state, "stale", 2) else None
        await query.answer(text, show_alert=False)
        return
    await query.answer()
    if state is None:
        return
    screen = "menu:main" if query.data == "menu:back" else query.data
    if state.screens.get(message.message_id) == screen:
        return

    if screen == "menu:barcodes":
        title, keyboard = "Выбери штрихкод", section_keys(catalog)
    elif screen == "menu:clean":
        title, keyboard = CLEAN_TEXT, section_keys()
    elif screen == "menu:help":
        title, keyboard = HELP_TEXT, section_keys()
    else:
        title, keyboard = MAIN_TEXT, menu_keys()
    markup = keyboard.as_markup()
    # Пока бот не менял это сообщение, доверяем состоянию из callback Telegram.
    if message.message_id not in state.screens and message.text == title and message.reply_markup == markup:
        return

    async def show():
        await message.answer(title, reply_markup=markup)
        remember_screen(state, message.message_id, screen)

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
                        guard.light.submit(uid, show)
                    return
                raise
        remember_screen(state, message.message_id, screen)

    if message.text is None:
        if not guard.busy(uid):
            guard.light.submit(uid, show)
    else:
        guard.edits.submit(uid, edit)


def remember_screen(state: UserState, message_id: int, screen: str) -> None:
    state.screens[message_id] = screen
    state.screens.move_to_end(message_id)
    if len(state.screens) > 8:
        state.screens.popitem(last=False)


@dp.callback_query(F.data.startswith("barcode:"))
async def send_barcode(query: CallbackQuery, catalog: dict[str, bytes], guard: Protection) -> None:
    uid = query.from_user.id
    state = guard.state(uid)
    if state is None:
        await query.answer()
        return
    code = query.data.removeprefix("barcode:")
    image = catalog.get(code)
    if image is None or not isinstance(query.message, Message):
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
        answer = message.answer_photo if guard.settings.barcode_mode == "photo" else message.answer_document
        await answer(
            BufferedInputFile(image, filename=f"{code}.png"), caption=code, parse_mode=None,
            reply_markup=section_keys(catalog).as_markup(),
        )

    if not guard.heavy.submit(uid, deliver):
        guard.note("очередь тяжёлых операций заполнена")
        text = "Бот занят. Попробуй чуть позже." if guard.warning(state, "busy", 2) else None
        await query.answer(text, show_alert=False)
        return
    guard.limit(state, "barcode", guard.settings.barcode_cooldown)
    try:
        await query.answer()
    finally:
        acknowledged.set()


@dp.callback_query()
async def unknown_callback(query: CallbackQuery, guard: Protection) -> None:
    state = guard.state(query.from_user.id)
    text = "Кнопка устарела. Открой /menu." if state and guard.warning(state, "stale", 2) else None
    await query.answer(text, show_alert=False)


async def send_plain(message: Message, text: str) -> Message:
    return await message.answer(text, parse_mode=None)


@dp.message(F.text)
async def handle_text(message: Message, guard: Protection) -> None:
    uid = message.from_user.id
    state = guard.state(uid)
    if state is None:
        return
    remaining = guard.remaining(state, "clean")
    if guard.busy(uid):
        guard.note("параллельная операция одного пользователя")
        return
    if remaining:
        guard.note("частый запрос очистки")
        if guard.warning(state, "clean", guard.settings.warning_interval):
            guard.light.submit(uid, lambda: send_plain(message, f"Слишком частые запросы. Подожди {remaining} сек."))
        return
    if guard.heavy.submit(uid, lambda: process_numbers(message, message.text, guard)):
        guard.limit(state, "clean", guard.settings.clean_cooldown)
    else:
        guard.note("очередь тяжёлых операций заполнена")
        if guard.warning(state, "clean", guard.settings.warning_interval):
            guard.light.submit(uid, lambda: send_plain(message, "Бот занят. Попробуй чуть позже."))

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
async def log_handler_error(event: ErrorEvent) -> None:
    logging.error("Ошибка обработки Telegram-события: %s", type(event.exception).__name__)


async def main() -> None:
    load_dotenv(Path(__file__).with_name(".env"))
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Не задан BOT_TOKEN. На VPS используй sudo numberbot token.")
    try:
        catalog = load_catalog()
        settings = load_settings()
    except (OSError, ValueError) as error:
        raise SystemExit(f"Ошибка конфигурации бота: {error}") from None

    notify_address = os.getenv("NOTIFY_SOCKET")
    async with Bot(token=token) as bot:
        @bot.session.middleware()
        async def notify_polling_ready(make_request, client, method):
            nonlocal notify_address
            try:
                result = await make_request(client, method)
            except TelegramNetworkError:
                # У некоторых сетевых исключений URL запроса содержит токен.
                raise TelegramNetworkError(method=method, message="Сетевая ошибка Telegram") from None
            if notify_address and isinstance(method, GetUpdates):
                address = notify_address
                if address.startswith("@"):
                    address = "\0" + address[1:]
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
                    notifier.sendto(b"READY=1", address)
                notify_address = None
            return result

        # Получение профиля проверяет токен до запуска цикла polling.
        await bot.me()
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
            ], request_timeout=5)
        except TelegramAPIError as error:
            logging.warning("Не удалось задать команды бота: %s", type(error).__name__)
        guard = Protection(settings)
        bot.session.middleware.register(guard.outgoing)
        try:
            await dp.start_polling(
                bot, catalog=catalog, guard=guard, close_bot_session=False,
                tasks_concurrency_limit=32, allowed_updates=["message", "callback_query"],
            )
        finally:
            await guard.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    try:
        asyncio.run(main())
    except Exception as error:
        logging.error("Запуск бота завершился ошибкой: %s", type(error).__name__)
        raise SystemExit(1) from None
