"""Экраны Telegram для сотрудников. Все изменения завершаются транзакцией каталога."""
from aiogram.types import KeyboardButton, KeyboardButtonRequestUsers, ReplyKeyboardMarkup, ReplyKeyboardRemove
from secrets import randbelow
from protection import remember
from catalog import CatalogError, PAGE_SIZE


async def request_person(message, state, stage, kind, label):
    from admin import set_flow
    request_id = randbelow(2**31)
    await set_flow(state, stage, kind, request_id=request_id)
    button = KeyboardButton(text=label, request_users=KeyboardButtonRequestUsers(
        request_id=request_id, user_is_bot=False, max_quantity=1))
    await message.answer("Выбери человека кнопкой, отправь его контакт Telegram или введи числовой ID. /cancel — отмена.",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[button]], resize_keyboard=True, one_time_keyboard=True))


def person_id(message, data):
    from admin import number
    if message.users_shared:
        shared = message.users_shared
        if shared.request_id != data["request_id"] or len(shared.users) != 1:
            raise CatalogError("Выбор устарел. Используй кнопку текущего запроса.")
        target = shared.users[0].user_id
    elif message.contact:
        target = message.contact.user_id
        if target is None:
            raise CatalogError("В карточке есть только номер телефона. Нажми кнопку выбора человека или введи Telegram ID.")
    else:
        target = number(message.text or "", 2**52-1)
    if not 0 < target < 2**52:
        raise CatalogError("Нужен положительный Telegram ID.")
    return target


async def screen(message, state, guard, repo, uid, action, args):
    from admin import Flow, set_flow, keys, tell, number, cancel_button
    repo.require_admin(uid)
    back=("Назад","ad:staff")
    if action=="staff":
        count=repo.db.execute("SELECT count(*) FROM trusted_users WHERE active=1").fetchone()[0]
        await tell(message,f"Доверенные пользователи\nРежим доступа: закрытый\nДопущено сотрудников: {count}",keys(
            ("Список сотрудников","ad:stafflist:0"),("Добавить сотрудника","ad:staffadd"),
            ("Нажимали /start","ad:staffstarts:0"), ("Найти сотрудника","ad:staffsearch"),("Назад","ad:home")))
    elif action=="staffstarts":
        page=number(args[0],100000)
        rows=repo.visitors(uid,page)
        buttons=[(f"{r['name']} · {r['user_id']}",f"ad:staffvisitor:{r['user_id']}") for r in rows[:PAGE_SIZE]]
        if page:buttons.append(("‹ Ранее",f"ad:staffstarts:{page-1}"))
        if len(rows)>PAGE_SIZE:buttons.append(("Далее ›",f"ad:staffstarts:{page+1}"))
        await tell(message,"Выбери профиль для проверки." if rows else "Пока никто не нажимал /start без доступа.",
                   keys(*buttons,back))
    elif action=="staffvisitor":
        target=number(args[0],2**52-1)
        row=repo.db.execute("SELECT * FROM start_visitors WHERE user_id=?",(target,)).fetchone()
        if not row or repo.allowed(target):raise CatalogError("Запись устарела.")
        employee=repo.employee(target)
        label="".join(c for c in row["name"] if c.isprintable())[:64]
        note="@"+row["username"] if row["username"] else ""
        await confirm(message,state,target=target,change="restore" if employee else "give",
                      name=label,note=note,revision=employee["revision"] if employee else 0)
    elif action=="stafflist":
        await listing(message,state,guard,repo,uid,"",number(args[0],1000000))
    elif action=="staffview":
        runtime = guard.state(uid)
        view = runtime.staff_views.get(args[0]) if runtime else None
        if view is None:
            raise CatalogError("Список устарел. Открой список сотрудников или поиск.")
        query, page = view
        if len(args) == 2:
            page = number(args[1], 1_000_000)
        await listing(message, state, guard, repo, uid, query, page)
    elif action=="staffsearch":
        await set_flow(state,Flow.staff,"find")
        await tell(message,"Введи ID, имя или часть примечания.",keys(await cancel_button(state)))
    elif action=="staffadd":
        await request_person(message,state,Flow.staff,"staffid","Выбрать сотрудника")
    else:
        target=number(args[0],2**52-1)
        row=repo.employee(target)
        if not row:raise CatalogError("Сотрудник не найден.")
        suffix=f"{target}:{row['revision']}"
        label=row["display_name"] or "Без имени"
        if action=="staffcard":
            view = f":{args[1]}" if len(args) == 2 else ""
            if len(args) == 2:
                back = ("Назад", f"ad:staffview:{args[1]}")
            flag="допущен" if row["active"] else "отключён"
            await tell(message,f"{label}\nTelegram ID: {target}\nСтатус: {flag}\nДобавлен: {row['created_at']} UTC"
                       f"\nКем: {row['created_by'] or 'импорт'}\nПримечание: {row['note'] or '—'}",
                       keys(("Изменить имя",f"ad:staffname:{suffix}{view}"),("Изменить примечание",f"ad:staffnote:{suffix}{view}"),
                            ("Отозвать доступ" if row["active"] else "Восстановить доступ",
                             f"ad:staff{'disable' if row['active'] else 'restore'}:{suffix}{view}"),back))
        elif action in {"staffname","staffnote","staffdisable","staffrestore"} and len(args) in (2, 3):
            if number(args[1])!=row["revision"]:raise CatalogError("Карточка устарела.")
            change=action[5:]
            params=dict(target=target,revision=row["revision"],change=change)
            if len(args) == 3:
                params["view"] = args[2]
            if change in {"name","note"}:
                await set_flow(state,Flow.staff,"staffedit",**params)
                await tell(message,"Введи новое значение. Для очистки отправь «-».",keys(await cancel_button(state)))
            else:
                await confirm(message,state,**params,name=label)
        else:raise CatalogError("Кнопка устарела.")


async def listing(message, state, guard, repo, uid, query, page):
    from admin import keys, tell
    repo.require_admin(uid)
    await state.clear()
    runtime = guard.state(uid)
    if runtime is None:
        return
    rows = repo.employees(query, page)
    token = remember(runtime.staff_views, (query, page))
    buttons = [(f"{'✓' if r['active'] else '○'} {r['display_name'] or 'Сотрудник'} · {r['user_id']}",
                f"ad:staffcard:{r['user_id']}:{token}") for r in rows[:PAGE_SIZE]]
    if page:
        buttons.append(("‹ Ранее", f"ad:staffview:{token}:{page-1}"))
    if len(rows) > PAGE_SIZE:
        buttons.append(("Далее ›", f"ad:staffview:{token}:{page+1}"))
    await tell(message, f"Сотрудники · страница {page+1}" if rows else "Сотрудники не найдены.",
               keys(*buttons, ("Назад", "ad:staff")))


async def confirm(message,state,**params):
    from admin import Flow,set_flow,keys,tell,cancel_button
    labels={"give":"Предоставить доступ","disable":"Отозвать доступ","restore":"Восстановить доступ",
            "name":"Сохранить имя","note":"Сохранить примечание"}
    label=labels[params["change"]]
    await set_flow(state,Flow.confirm,"employee",**params)
    nonce=(await state.get_data())["nonce"]
    await tell(message,f"{label}?\nTelegram ID: {params['target']}\nИмя: {params.get('name') or '—'}"
               f"\nПримечание: {params.get('note') or '—'}",keys((label,f"flow:ok:{nonce}"),await cancel_button(state)))


async def step(message,state,guard,repo,uid,data):
    from admin import Flow, set_flow, keys, tell
    repo.require_admin(uid)
    kind=data["kind"]
    text=message.text or ""
    if kind=="find":
        await listing(message,state,guard,repo,uid,text[:200],0)
    elif kind=="staffid":
        target=person_id(message,data)
        if repo.is_admin(target):raise CatalogError("Доступ предоставлен административной ролью.")
        row=repo.employee(target)
        if row:
            await state.clear()
            await tell(message,"Данный пользователь уже имеет доступ к боту." if row["active"] else "Запись отключена. Её можно восстановить.",
                       keys(("Открыть карточку",f"ad:staffcard:{target}")))
        else:
            await set_flow(state,Flow.staff,"staffdetails",target=target)
            await message.answer("Введи имя и примечание двумя строками. Можно только имя или «-», чтобы пропустить.",
                                 reply_markup=ReplyKeyboardRemove())
    elif kind=="staffdetails":
        name,_,note=text.partition("\n")
        if not text or len(name)>64 or len(note)>200:raise CatalogError("Имя до 64 символов, примечание до 200.")
        await confirm(message,state,target=data["target"],change="give",name="" if name=="-" else name,note=note)
    elif kind=="staffedit":
        change=data["change"]
        await confirm(message,state,target=data["target"],revision=data["revision"],change=change,
                      view=data.get("view"),
                      **{change:"" if text=="-" else text})
    else:raise CatalogError("Выбери сотрудника кнопкой.")
