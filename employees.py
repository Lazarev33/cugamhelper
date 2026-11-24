"""Экраны Telegram для сотрудников. Все изменения завершаются транзакцией каталога."""
from aiogram.types import KeyboardButton, KeyboardButtonRequestUsers, ReplyKeyboardMarkup, ReplyKeyboardRemove
from secrets import randbelow
from catalog import CatalogError, PAGE_SIZE


async def screen(message, state, repo, uid, action, args):
    from admin import Flow, set_flow, keys, tell, number, CANCEL
    repo.require_admin(uid)
    back=("Назад","ad:staff")
    if action=="staff":
        count=repo.db.execute("SELECT count(*) FROM trusted_users WHERE active=1").fetchone()[0]
        await tell(message,f"Доверенные пользователи\nРежим доступа: закрытый\nДопущено сотрудников: {count}",keys(
            ("Список сотрудников","ad:stafflist:0"),("Добавить сотрудника","ad:staffadd"),
            ("Найти сотрудника","ad:staffsearch"),("Назад","ad:home")))
    elif action=="stafflist":
        await listing(message,state,repo,uid,"",number(args[0],1000000))
    elif action=="staffsearch":
        await set_flow(state,Flow.staff,"find")
        await tell(message,"Введи ID, имя или часть примечания.",keys(CANCEL))
    elif action=="staffadd":
        request_id=randbelow(2**31)
        await set_flow(state,Flow.staff,"staffid",request_id=request_id)
        button=KeyboardButton(text="Выбрать сотрудника",request_users=KeyboardButtonRequestUsers(
            request_id=request_id,user_is_bot=False,max_quantity=1))
        await message.answer("Введи числовой Telegram ID или выбери человека кнопкой. /cancel — отмена.",
            reply_markup=ReplyKeyboardMarkup(keyboard=[[button]],resize_keyboard=True,one_time_keyboard=True))
    else:
        target=number(args[0],2**52-1)
        row=repo.employee(target)
        if not row:raise CatalogError("Сотрудник не найден.")
        suffix=f"{target}:{row['revision']}"
        label=row["display_name"] or "Без имени"
        if action=="staffcard":
            flag="допущен" if row["active"] else "отключён"
            await tell(message,f"{label}\nTelegram ID: {target}\nСтатус: {flag}\nДобавлен: {row['created_at']} UTC"
                       f"\nКем: {row['created_by'] or 'импорт'}\nПримечание: {row['note'] or '—'}",
                       keys(("Изменить имя",f"ad:staffname:{suffix}"),("Изменить примечание",f"ad:staffnote:{suffix}"),
                            ("Отозвать доступ" if row["active"] else "Восстановить доступ",
                             f"ad:staff{'disable' if row['active'] else 'restore'}:{suffix}"),back))
        elif action in {"staffname","staffnote","staffdisable","staffrestore"} and len(args)==2:
            if number(args[1])!=row["revision"]:raise CatalogError("Карточка устарела.")
            change=action[5:]
            params=dict(target=target,revision=row["revision"],change=change)
            if change in {"name","note"}:
                await set_flow(state,Flow.staff,"staffedit",**params)
                await tell(message,"Введи новое значение. Для очистки отправь «-».",keys(CANCEL))
            else:
                await confirm(message,state,**params,name=label)
        else:raise CatalogError("Кнопка устарела.")


async def listing(message,state,repo,uid,query,page):
    from admin import Flow,set_flow,keys,tell
    repo.require_admin(uid)
    rows=repo.employees(query,page)
    await set_flow(state,Flow.staff,"staffresults",query=query)
    nonce=(await state.get_data())["nonce"]
    buttons=[(f"{'✓' if r['active'] else '○'} {r['display_name'] or 'Сотрудник'} · {r['user_id']}",
              f"ad:staffcard:{r['user_id']}") for r in rows[:PAGE_SIZE]]
    if page:buttons.append(("‹ Ранее",f"flow:staffpage:{nonce}:{page-1}"))
    if len(rows)>PAGE_SIZE:buttons.append(("Далее ›",f"flow:staffpage:{nonce}:{page+1}"))
    await tell(message,f"Сотрудники · страница {page+1}" if rows else "Сотрудники не найдены.",
               keys(*buttons,("Назад","ad:staff")))


async def confirm(message,state,**params):
    from admin import Flow,set_flow,keys,tell,CANCEL
    labels={"give":"Предоставить доступ","disable":"Отозвать доступ","restore":"Восстановить доступ",
            "name":"Сохранить имя","note":"Сохранить примечание"}
    label=labels[params["change"]]
    await set_flow(state,Flow.confirm,"employee",**params)
    nonce=(await state.get_data())["nonce"]
    await tell(message,f"{label}?\nTelegram ID: {params['target']}\nИмя: {params.get('name') or '—'}"
               f"\nПримечание: {params.get('note') or '—'}",keys((label,f"flow:ok:{nonce}"),CANCEL))


async def step(message,state,repo,uid,data):
    from admin import Flow,set_flow,keys,tell,number,CANCEL
    repo.require_admin(uid)
    kind=data["kind"]
    text=message.text or ""
    if kind=="find":
        await listing(message,state,repo,uid,text[:200],0)
    elif kind=="staffid":
        shared=message.users_shared
        if shared and (shared.request_id!=data["request_id"] or len(shared.users)!=1):
            raise CatalogError("Выбор пользователя устарел.")
        target=shared.users[0].user_id if shared else number(text,2**52-1)
        if target<=0:raise CatalogError("Введи положительный Telegram ID.")
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
                      **{change:"" if text=="-" else text})
    else:raise CatalogError("Выбери сотрудника кнопкой.")
