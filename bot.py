"""Telegram-меню, выдача штрихкодов и очистка списков номеров."""

import asyncio
import logging
import os
from pathlib import Path
import socket

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import (
    TelegramAPIError, TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter,
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.fsm.strategy import FSMStrategy
from aiogram.methods import GetUpdates
from aiogram.types import (
    BotCommand, BufferedInputFile, CallbackQuery, ErrorEvent, InlineKeyboardButton, Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

from barcodes import load_catalog
from branding import BOT_NAME
from parser import clean_numbers, chunks


dp = Dispatcher(fsm_strategy=FSMStrategy.CHAT, events_isolation=SimpleEventIsolation())
MAIN_TEXT = f"Я — {BOT_NAME}. Выбери действие."
CLEAN_TEXT = (
    "Отправь список в этом чате, по одной строке на номер. Пример:\n\n"
    "26008178-03.11.2026\n"
    "26008179-03.11.2026\n\n"
    "Сначала придёт отчёт, затем — номера для копирования. "
    "Уже очищенные номера тоже принимаются."
)
HELP_TEXT = (
    "ШК — выбери складской код и открой PNG для сканирования.\n"
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


@dp.message(CommandStart())
@dp.message(Command("menu"))
async def start(message: Message) -> None:
    await message.answer(MAIN_TEXT, reply_markup=menu_keys().as_markup())


@dp.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=section_keys().as_markup())


@dp.callback_query(F.data.in_({
    "menu:barcodes", "menu:clean", "menu:help", "menu:main", "menu:back",
}))
async def navigate(query: CallbackQuery, catalog: dict[str, bytes]) -> None:
    await query.answer()
    if not isinstance(query.message, Message):
        return
    screens = {
        "menu:barcodes": ("Выбери штрихкод", section_keys(catalog)),
        "menu:clean": (CLEAN_TEXT, section_keys()),
        "menu:help": (HELP_TEXT, section_keys()),
    }
    title, keyboard = screens.get(query.data, (MAIN_TEXT, menu_keys()))
    try:
        await query.message.edit_text(title, reply_markup=keyboard.as_markup())
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise


@dp.callback_query(F.data.startswith("barcode:"))
async def send_barcode(query: CallbackQuery, catalog: dict[str, bytes]) -> None:
    assert query.data is not None
    code = query.data.removeprefix("barcode:")
    image = catalog.get(code)
    if image is None:
        await query.answer("Код удалён из каталога. Открой /menu.", show_alert=True)
        return
    await query.answer()
    if isinstance(query.message, Message):
        await query.message.answer_document(
            BufferedInputFile(image, filename=f"{code}.png"),
            caption=code, parse_mode=None,
            reply_markup=section_keys(catalog).as_markup(),
        )


async def send_plain(message: Message, text: str) -> Message:
    # Ответ 429 означает, что это сообщение ещё не отправлено.
    for attempt in range(5):
        try:
            return await message.answer(text, parse_mode=None)
        except TelegramRetryAfter as error:
            if attempt == 4:
                raise
            await asyncio.sleep(error.retry_after)


@dp.message(F.text)
async def handle_text(message: Message) -> None:
    assert message.text is not None
    identifiers, invalid_lines = clean_numbers(message.text)
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
            logging.warning("Сбой отправки результата: %s", type(error).__name__)
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
    except (OSError, ValueError) as error:
        raise SystemExit(f"Ошибка каталога ШК: {error}") from None

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
        await dp.start_polling(bot, catalog=catalog, close_bot_session=False)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except Exception as error:
        logging.error("Запуск бота завершился ошибкой: %s", type(error).__name__)
        raise SystemExit(1) from None
