"""Minimal Telegram bot: remove dates/dashes from identifier lists."""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from dotenv import load_dotenv

from branding import BOT_NAME
from parser import clean_numbers, chunks

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

dp = Dispatcher()

INSTRUCTIONS = (
    f"Я — {BOT_NAME}.\n\n"
    "Пришли список — один номер с датой на каждой строке:\n\n"
    "26008178-03.11.2026\n"
    "26008179-03.11.2026\n\n"
    "Я верну только номера, в том же порядке. "
    "Если найдётся ошибка в формате, покажу её строку, "
    "а не буду молча пропускать номер."
)


@dp.message(CommandStart())
@dp.message(Command("help"))
async def start(message: Message) -> None:
    await message.answer(INSTRUCTIONS)


@dp.message(F.text)
async def handle_text(message: Message) -> None:
    assert message.text is not None
    identifiers, invalid_lines = clean_numbers(message.text)
    if invalid_lines:
        examples = ", ".join(str(n) for n in invalid_lines[:15])
        suffix = "…" if len(invalid_lines) > 15 else ""
        await message.answer(
            f"Не обработано. Неверный формат в строках: {examples}{suffix}.\n"
            "Исправь их и отправь список заново. "
            "Ожидаемый формат: 26008178-03.11.2026"
        )
        return
    if not identifiers:
        await message.answer(INSTRUCTIONS)
        return

    # Strict validation: one output identifier per nonempty source line.
    source_count = sum(bool(line.strip()) for line in message.text.splitlines())
    assert len(identifiers) == source_count

    for index, part in enumerate(chunks(identifiers), start=1):
        header = (f"Готово. Номеров: {len(identifiers)}. "
                  f"Потерь: 0.\n" if index == 1 else "")
        await message.answer(f"{header}<pre>{part}</pre>", parse_mode=ParseMode.HTML)


async def main() -> None:
    if not TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN. Добавь его в файл .env")
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=TOKEN)
    # Set Telegram profile display name, not the @username.
    for lang in (None, "ru"):
        try:
            await bot.set_my_name(name=BOT_NAME, language_code=lang)
        except TelegramAPIError as error:
            # Telegram-side errors must not prevent message handling.
            logging.warning("Не удалось задать имя бота (язык=%s): %s", lang, error)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
