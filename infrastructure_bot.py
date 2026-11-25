from aiogram import F
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from infrastructure import InfraError



def keyboard(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=callback) for text, callback in row] for row in rows
    ])


async def open_link(message, command, infrastructure, guard):
    uid = message.from_user.id
    challenge = command.args[5:]
    link = infrastructure.link_for_telegram(uid, challenge)
    if not link:
        guard.service(uid, lambda: message.answer("Запрос подключения недоступен или истёк."))
        return
    label = ("Android · " + link["name"]) if link["scope"] == "android" else "Вход в Control Center"
    guard.service(uid, lambda: message.answer(
        f"Подключение Cugam Helper\n{label}\nРазрешить подключение?",
        parse_mode=None, reply_markup=keyboard((("Подтвердить", "infra:yes:" + challenge),
                                               ("Отклонить", "infra:no:" + challenge)))))


async def confirm_link(query, infrastructure, guard):
    uid = query.from_user.id
    _, action, challenge = query.data.split(":")
    link = infrastructure.link_for_telegram(uid, challenge, action == "yes")
    await query.answer()
    if not link:
        guard.service(uid, lambda: query.message.edit_text("Запрос подключения недоступен или истёк."))
        return
    text = ("Подключение разрешено. Вернись в приложение или браузер." if action == "yes"
            else "Запрос подключения отклонён.")
    guard.service(uid, lambda: query.message.edit_text(text))


async def devices(message, infrastructure, guard):
    uid = message.from_user.id
    with infrastructure.repository() as repo:
        records = [row for row in infrastructure.devices(repo, uid) if not row["revoked"]]
    keys = keyboard(*[((row["name"], "infra:revoke:" + row["id"]),) for row in records])
    guard.service(uid, lambda: message.answer(
        "Подключённые устройства. Выбери устройство для отключения." if records else "Подключённых устройств нет.",
        reply_markup=keys if records else None))


async def device_action(query, infrastructure, guard):
    uid = query.from_user.id
    _, action, device = query.data.split(":")
    await query.answer()
    if action == "revoke":
        guard.service(uid, lambda: query.message.edit_text("Отключить устройство?", reply_markup=keyboard(
            (("Отключить", "infra:disconnect:" + device), ("Отмена", "infra:cancel")))))
        return
    try:
        infrastructure.revoke(uid, uid, device)
        text = "Устройство отключено."
    except InfraError:
        text = "Устройство уже отключено или недоступно."
    guard.service(uid, lambda: query.message.edit_text(text))


async def cancel(query, guard):
    await query.answer()
    guard.service(query.from_user.id, lambda: query.message.edit_text("Отключение отменено."))


def register(dispatcher):
    dispatcher.message.register(open_link, CommandStart(deep_link=True, magic=F.args.regexp(r"^link_[A-Za-z0-9_-]{24}$")))
    dispatcher.callback_query.register(confirm_link, F.data.regexp(r"^infra:(yes|no):[A-Za-z0-9_-]{24}$"))
    dispatcher.message.register(devices, Command("devices"))
    dispatcher.callback_query.register(device_action, F.data.regexp(r"^infra:(revoke|disconnect):[A-Za-z0-9_-]{24}$"))
    dispatcher.callback_query.register(cancel, F.data == "infra:cancel")
