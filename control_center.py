"""Серверная веб-панель, использующая доменные операции действующего каталога."""

from dataclasses import replace
from html import escape
from pathlib import Path
from fastapi.security import APIKeyCookie
from fastapi import Security
from pydantic import BaseModel
from typing import Annotated
import os
from time import monotonic, time
from telegram_bridge import telegram_check
from secrets import token_urlsafe
from urllib.parse import parse_qs, urlencode
from hmac import compare_digest
from shutil import disk_usage
import re
import subprocess

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from barcodes import render_png
from admin_guard import consume, fingerprint
from retention import sweep
from catalog import CatalogError
from features import GROUPS
from infrastructure import CLIENT_FEATURES, TELEGRAM_OVERRIDES, InfraError, digest
from infra_features import revision, write_common
from features import LABELS
from settings import LIMITS

ROOT = Path(__file__).resolve().parent
SECRET = re.compile(r"[A-Za-z0-9_-]{43}")
ID = re.compile(r"[A-Za-z0-9_-]{24}")
PAGES = {
    "/": "Обзор", "/telegram": "Telegram-бот", "/android": "Android", "/devices": "Устройства",
    "/employees": "Сотрудники", "/admins": "Администраторы", "/catalog": "Каталог ШК",
    "/features": "Функциональность", "/security": "Безопасность", "/storage": "Хранилище",
    "/diagnostics": "Диагностика", "/audit": "Журнал изменений",
}
MESSAGES = {
    "ok": "Изменения сохранены.", "form_error": "Проверь введённые данные.",
    "forbidden": "Для этого действия недостаточно полномочий.",
    "invalid_login": "Код недействителен, просрочен или уже использован.",
    "pending": "Сначала подтверди вход в личном чате с ботом.",
    "unavailable": "Бот пока не зарегистрировал адрес в системе.",
    "conflict": "Запись изменилась или такое значение уже существует. Проверь данные и повтори.",
}
CLIENT_LABELS = dict(zip(CLIENT_FEATURES[:13], (
    "Привязка Android через Telegram", "Приём экспорта в Telegram", "НСУ", "Каталог",
    "Камера", "Сканер HID", "Очистка", "Сортировка", "Сверка", "Документы",
    "Дубликаты", "Отправка в Telegram", "Синхронизация каталога",
), strict=True))
CLIENT_LABELS.update({"telegram_" + key: "Telegram · " + LABELS[key] for key in TELEGRAM_OVERRIDES})


def e(value):
    return escape(str(value), quote=True)


def url(path, **query):
    return path + ("?" + urlencode(query) if query else "")


def redirect(path, notice=None):
    return RedirectResponse(path + (("&" if "?" in path else "?") + "notice=" + notice if notice else ""),
                            status_code=303)


def field(name, label, value="", *, mode="text", required=False, length=200):
    return (f'<label>{e(label)}<input name="{e(name)}" type="{mode}" value="{e(value)}" '
            f'maxlength="{length}" {"required" if required else ""} autocomplete="off"></label>')


def hidden(name, value):
    return f'<input type="hidden" name="{e(name)}" value="{e(value)}">'


def form(path, body, title, *, csrf=None, confirmation="", quiet=False):
    confirm = f' data-confirm="{e(confirmation)}"' if confirmation else ""
    return (f'<form method="post" action="{e(path)}"{confirm}>'
            + (hidden("csrf", csrf) if csrf else "") + body
            + f'<button type="submit" class="{"quiet" if quiet else "primary"}">{e(title)}</button></form>')


def card(title, content):
    return f'<section class="card"><h2>{e(title)}</h2>{content}</section>'


def table(headers, rows):
    if not rows:
        return '<p class="empty">Записей пока нет.</p>'
    return ('<div class="scroll"><table><thead><tr>' + "".join(f"<th>{e(title)}</th>" for title in headers)
            + "</tr></thead><tbody>" + "".join("<tr>" + "".join("<td>" + value + "</td>" for value in row)
                                              + "</tr>" for row in rows) + "</tbody></table></div>")


def link(path, label):
    return f'<a href="{e(path)}">{e(label)}</a>'


def metrics(values):
    return '<div class="metrics">' + "".join(
        f'<section class="metric"><span>{e(label)}</span><strong>{e(value)}</strong></section>'
        for label, value in values) + "</div>"


def pager(path, page, more, **filters):
    return '<div class="pager">' + (link(url(path, page=page - 1, **filters), "Назад") if page else "")         + f'<span>Страница {page + 1}</span>' + (link(url(path, page=page + 1, **filters), "Далее") if more else "") + "</div>"


def badge(active, yes="Активен", no="Отключён"):
    return f'<span class="badge {"good" if active else "muted"}">{e(yes if active else no)}</span>'


def integer(value):
    if not re.fullmatch(r"[0-9]{1,16}", value or "") or not 0 < int(value) < 2**52:
        raise ValueError
    return int(value)


def clock(value):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(value, timezone.utc).strftime("%d.%m.%Y %H:%M UTC") if value else "—"


def layout(title, content, *, identity=None, current="/", notice=None, owner=False):
    heading = '<a class="brand" href="/">CUGAM HELPER<small>CONTROL CENTER</small></a>'
    items = ""
    sections = ("Инфраструктура", "Управление", "Система")
    if identity:
        for section, paths in zip(sections, [list(PAGES)[:4], list(PAGES)[4:8], list(PAGES)[8:]]):
            items += f'<span class="group">{section}</span>'
            for path in paths:
                active = ' aria-current="page"' if current == path else ""
                items += f'<a href="{path}"{active}>{PAGES[path]}</a>'
        items += form("/web/logout", "", "Выйти", csrf=identity.csrf, quiet=True)
        items += form("/web/logout-all", "", "Выйти на всех устройствах", csrf=identity.csrf, quiet=True,
                      confirmation="Завершить все веб-сессии?")
    role = ("Владелец" if owner else "Администратор") if identity else ""
    alert = f'<p class="notice" role="status">{e(MESSAGES[notice])}</p>' if notice in MESSAGES else ""
    return HTMLResponse(f'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} · Cugam Helper</title><link rel="stylesheet" href="/assets/control.css">
<script src="/assets/control.js" defer></script></head>
<body><div class="shell {"session" if identity else "guest"}">
<aside><div class="sidehead">{heading}<button type="button" class="navtoggle" aria-expanded="false"
aria-controls="nav">Меню</button></div><nav id="nav" aria-label="Разделы">{items}</nav></aside>
<main><div class="topline"><span>{e(role)}</span><label>Тема<select id="theme" aria-label="Тема">
<option value="system">Системная</option><option value="dark">Тёмная</option><option value="light">Светлая</option>
</select></label></div><header><h1>{e(title)}</h1></header>{alert}{content}</main></div>
<dialog id="confirmation"><h2>Подтверждение</h2><p></p><form method="dialog">
<button value="cancel" autofocus>Отмена</button><button value="accept" class="danger">Подтвердить</button></form></dialog>
</body></html>''')


def service_info():
    try:
        result = subprocess.run(["systemctl", "show", "tg-number-cleaner.service",
                                 "-p", "ActiveState", "-p", "MemoryCurrent", "-p", "ActiveEnterTimestampMonotonic"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=1, text=True)
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    except (OSError, subprocess.SubprocessError):
        values = {}
    state = {"active": "Работает", "inactive": "Остановлен", "failed": "Ошибка",
             "activating": "Запускается", "deactivating": "Останавливается"}.get(values.get("ActiveState"), "Нет данных")
    memory = values.get("MemoryCurrent", "")
    stamp = values.get("ActiveEnterTimestampMonotonic", "")
    if state == "Работает" and stamp.isdigit() and int(stamp):
        from time import monotonic
        seconds = max(0, int(monotonic() - int(stamp) / 1000000))
        uptime = f"{seconds // 3600} ч {(seconds % 3600) // 60} мин"
    else:
        uptime = "—"
    return state, f"{int(memory)/1048576:.1f} МиБ" if memory.isdigit() and int(memory)<2**60 else "—", uptime


def install(app, service, features, config):
    session_cookie = "cugam_session" if config.local else "__Host-cugam_session"
    proof_cookie = "cugam_link" if config.local else "__Host-cugam_link"
    browser_session = APIKeyCookie(name="__Host-cugam_session", auto_error=False)
    last_check = {}
    shared_help = "<p>Подтверди вход в личном чате с Telegram-ботом. Для первичного доступа владелец может выпустить одноразовый код на VPS.</p>"

    def cookie(response, name, value, seconds):
        response.set_cookie(name, value, max_age=seconds, secure=not config.local, httponly=True,
                            samesite="strict", path="/")
        return response

    def clear_cookie(response, name):
        response.delete_cookie(name, secure=not config.local, httponly=True, samesite="strict", path="/")
        return response

    def identify(request, repo):
        return service.identify(repo, request.cookies.get(session_cookie), "web")

    async def fields(request):
        if request.headers.get("origin") != config.origin:
            raise InfraError(403, "origin_rejected")
        try:
            raw = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True,
                           strict_parsing=True, max_num_fields=64, errors="strict")
        except (ValueError, UnicodeError):
            raise InfraError(422, "invalid_form") from None
        if any(len(item) != 1 for item in raw.values()):
            raise InfraError(422, "invalid_form")
        return {key: item[0] for key, item in raw.items()}

    @app.get("/assets/{asset}", include_in_schema=False)
    def assets(asset):
        resources = {"control.css": ("control.css", "text/css"), "control.js": ("control.js", "text/javascript")}
        if asset not in resources:
            return Response(status_code=404)
        filename, mime = resources[asset]
        return Response((ROOT / filename).read_bytes(), media_type=mime)

    @app.get("/login", include_in_schema=False)
    def login(request: Request):
        return layout("Вход в Control Center", shared_help + '<div class="columns">' +
                      card("Через Telegram", form("/web/link/start", field("uid", "Telegram ID", required=True,
                                                                         length=16), "Запросить вход")) +
                      card("Одноразовый код владельца", form("/web/recovery", field("code", "Код с VPS", mode="password",
                                                                                    required=True, length=43), "Войти")) +
                      "</div>", notice=request.query_params.get("notice"))

    @app.post("/web/link/start", include_in_schema=False)
    async def web_start(request: Request):
        data = await fields(request)
        verifier = token_urlsafe(32)
        try:
            uid = integer(data.get("uid"))
            challenge, _, username = service.link_start(uid, "Браузер", "", digest(verifier), "web")
        except ValueError:
            return redirect("/login", "form_error")
        except InfraError as failure:
            if failure.status != 503:
                raise
            return redirect("/login", "unavailable")
        if not username:
            return redirect("/login", "unavailable")
        return cookie(redirect("/login/confirm"), proof_cookie, challenge + "." + verifier, 300)

    def pending(request):
        raw = request.cookies.get(proof_cookie, "")
        parts = raw.split(".")
        if len(parts) != 2 or not ID.fullmatch(parts[0]) or not SECRET.fullmatch(parts[1]):
            raise InfraError(401, "link_unavailable")
        return parts

    @app.get("/login/confirm", include_in_schema=False)
    def web_wait(request: Request):
        try:
            challenge, _ = pending(request)
        except InfraError:
            return redirect("/login", "invalid_login")
        with service.repository() as repo:
            username = service.meta(repo, "bot_username")
        if not username:
            return redirect("/login", "unavailable")
        button = '<p class="open-telegram">' + link(f"https://t.me/{username}?start=link_{challenge}", "Открыть Telegram") + "</p>"
        return layout("Подтверждение в Telegram", card("Продолжить вход", button +
                      form("/web/link/exchange", hidden("confirm", "yes"), "Я подтвердил вход")),
                      notice=request.query_params.get("notice"))

    def session_response(new):
        response = cookie(redirect("/"), session_cookie, new["access_token"], new["expires_in"])
        return clear_cookie(response, proof_cookie)

    @app.post("/web/link/exchange", include_in_schema=False)
    async def web_exchange(request: Request):
        await fields(request)
        try:
            return session_response(service.exchange(*pending(request), "web"))
        except InfraError as failure:
            if failure.code == "link_pending":
                return redirect("/login/confirm", "pending")
            return clear_cookie(redirect("/login", "invalid_login"), proof_cookie)

    @app.post("/web/recovery", include_in_schema=False)
    async def recovery(request: Request):
        data = await fields(request)
        code = data.get("code", "")
        if SECRET.fullmatch(code):
            try:
                return session_response(service.recovery_exchange(code))
            except InfraError:
                pass
        return redirect("/login", "invalid_login")

    @app.post("/web/logout", include_in_schema=False)
    async def logout(request: Request):
        data = await fields(request)
        with service.repository() as repo, repo.transaction():
            current = identify(request, repo)
            if not compare_digest(data.get("csrf", ""), current.csrf):
                raise InfraError(403, "csrf")
            repo.db.execute("DELETE FROM infra_sessions WHERE id=?", (current.id,))
        return clear_cookie(redirect("/login"), session_cookie)

    @app.post("/web/logout-all", include_in_schema=False)
    async def logout_all(request: Request):
        data = await fields(request)
        with service.repository() as repo, repo.transaction():
            current = identify(request, repo)
            if not compare_digest(data.get("csrf", ""), current.csrf):
                raise InfraError(403, "csrf")
            repo.db.execute("DELETE FROM infra_sessions WHERE uid=? AND scope='web'", (current.uid,))
            repo.log(current.uid, "Завершены веб-сессии", str(current.uid))
        return clear_cookie(redirect("/login"), session_cookie)

    def employee_card(repo, actor, uid):
        record = repo.employee(uid)
        csrf = actor.csrf
        if not record:
            raise InfraError(404, "employee_not_found")
        base = hidden("uid", uid) + hidden("revision", record["revision"])
        root = "/web/employee"
        fallback = record["display_name"] or str(uid)
        content = '<p>' + e(record["note"] or "Примечания нет") + "</p>" + badge(record["active"], "Допущен")
        for action, key, label in (("name", "display_name", "Имя"), ("note", "note", "Примечание")):
            content += form(root, base + hidden("action", action) + field("value", label, record[key], length=64 if action == "name" else 200),
                            "Сохранить", csrf=csrf)
        action = "disable" if record["active"] else "restore"
        label = "Отозвать доступ" if record["active"] else "Восстановить доступ"
        content += form(root, base + hidden("action", action), label, csrf=csrf, confirmation=label + "?", quiet=True)
        target_devices = service.devices(repo, uid)
        return (fallback, '<div class="columns">' + card("Доступ и профиль", content)
                + device_list(repo, actor, target_devices, str(uid), scoped=True) + "</div>")

    def device_list(repo, actor, rows, label="", *, scoped=False):
        items = []
        for row in rows:
            controls = ""
            if not row["revoked"] and (actor.uid == repo.owner or not repo.is_admin(row["uid"]) or actor.uid == row["uid"]):
                controls = form("/web/device", hidden("uid", row["uid"]) + hidden("id", row["id"]) +
                                hidden("back", "employee" if scoped else ""), "Отключить", csrf=actor.csrf,
                                confirmation="Отозвать сессию устройства?", quiet=True)
            items.append((e(row["name"]), e(row["uid"]), e(row["app_version"] or "—"), e(clock(row["seen"])),
                          badge(row["revoked"] is None), controls))
        revoke_all = ""
        if scoped and any(row["revoked"] is None for row in rows):
            revoke_all = form("/web/device", hidden("uid", label) + hidden("back", "employee"), "Отключить все",
                              csrf=actor.csrf, confirmation="Отключить все устройства сотрудника?", quiet=True)
        return card("Подключённые устройства", table(("Название", "Telegram ID", "Версия", "Активность", "Статус", ""), items) + revoke_all)

    def dashboard(repo):
        bot, memory, uptime = service_info()
        counts = [(label, repo.db.execute(statement).fetchone()[0]) for label, statement in (
            ("Администраторы", "SELECT count(*) FROM admins"),
            ("Сотрудники", "SELECT count(*) FROM trusted_users WHERE active=1"),
            ("Устройства", "SELECT count(*) FROM infra_devices WHERE revoked IS NULL"),
            ("Штрихкоды", "SELECT count(*) FROM barcodes WHERE active=1"),
        )]
        try:
            load = f"{os.getloadavg()[0]:.2f}"
        except OSError:
            load = "—"
        used = disk_usage(service.path.parent)
        active_sessions = repo.db.execute(
            "SELECT count(*) FROM infra_sessions WHERE scope='android' AND renewal>?", (service.now(),)).fetchone()[0]
        queue = sum(not record.finished for record in app.state.exports.records.values())
        extra = [("Сессии Android", active_sessions), ("Очередь экспорта", queue),
                 ("Нагрузка CPU · 1 мин", load), ("Диск занят", f"{100*used.used/used.total:.0f}%")]
        return metrics([("Telegram", bot), ("API", "Доступен"), ("Память бота", memory), ("Работа бота", uptime),
                        *counts, *extra]) + card("Состояние платформы", "<p>SQLite доступна. Закрытый режим входа.</p><p>Версия " +
                 e((ROOT / "VERSION").read_text().strip()) + " · " +
                 e((ROOT / "REVISION").read_text().strip()[:12] if (ROOT / "REVISION").exists() else "ревизия не записана") + "</p>")

    @app.get("/catalog/{record_id}/preview", include_in_schema=False)
    def preview(record_id: int, request: Request):
        with service.repository() as repo:
            identify(request, repo)
            record = repo.get(record_id) if 0 < record_id < 2**63 else None
            if not record:
                return Response(status_code=404)
        return Response(render_png(record.value), media_type="image/png")

    def barcode_rows(repo, actor, page, q, status):
        filter_code = {"active": " AND active=1", "disabled": " AND active=0"}.get(status, "")
        rows = repo.db.execute("SELECT * FROM barcodes WHERE (instr(lower(name),lower(?)) OR instr(value,?))"
                               + filter_code + " ORDER BY id LIMIT 21 OFFSET ?", (q, q, page * 20)).fetchall()
        items = [(link(url("/catalog", id=row["id"]), row["name"]), e(row["value"]), badge(row["active"])) for row in rows[:20]]
        return table(("Название", "Значение", "Статус"), items) + pager("/catalog", page, len(rows)>20, q=q, status=status)

    def catalog_card(record, actor):
        info = hidden("id", record.id) + hidden("revision", record.revision)
        edit = form("/web/barcode", info + field("name", "Название", record.name, required=True, length=64)
                    + field("value", "Code 128", record.value, required=True, length=128), "Сохранить", csrf=actor.csrf)
        mode = "Отключить" if record.active else "Восстановить"
        toggle = form("/web/barcode", info + hidden("action", "toggle"), mode, csrf=actor.csrf, confirmation=mode + " запись?", quiet=True)
        delete = form("/web/barcode", info + hidden("action", "delete"), "Удалить", csrf=actor.csrf,
                      confirmation="Удалить запись каталога?", quiet=True)
        return '<div class="columns">' + card("Редактирование", edit + '<div class="actions">' + toggle + delete
                                                  + "</div>") + card("Предпросмотр", f'<img class="barcode" alt="Code 128" src="/catalog/{record.id}/preview">') + "</div>"

    def features_panel(actor, repo):
        common = features.current().options()
        client = service.policy(repo)
        def switches(values, labels, mode):
            rows = "".join(f'<label class="switch"><span>{e(label)}</span><input name="{key}" type="checkbox"'
                           f'{" checked" if values[key] else ""} {"disabled" if actor.uid != repo.owner else ""}></label>'
                           for key, label in labels)
            if actor.uid != repo.owner:
                return rows
            return form("/web/features", hidden("mode", mode) + hidden("revision", revision(values)) + rows,
                        "Применить", csrf=actor.csrf)
        labels = [item for _, items in GROUPS for item in items]
        return '<div class="columns">' + card("Общие функции", switches(common, labels, "common")) + \
            card("Telegram и Android", switches(client, list(CLIENT_LABELS.items()), "client")) + "</div>"

    def search(path, query, status):
        options = (("all", "Все записи"), ("active", "Активные"), ("disabled", "Отключённые"))
        if path == "/employees":
            options += (("visitors", "Нажимали /start"),)
        select = '<label>Статус<select name="status">' + "".join(
            f'<option value="{key}"{" selected" if key == status else ""}>{title}</option>' for key, title in options) + "</select></label>"
        return (f'<form action="{path}" class="search">' + field("q", "Поиск", query, mode="search", length=64) +
                select + '<button class="quiet">Найти</button></form>')

    def employees_panel(repo, actor, page, q, status):
        if status == "visitors":
            rows = repo.db.execute("""SELECT * FROM start_visitors WHERE instr(CAST(user_id AS TEXT),?) OR
                instr(lower(name),lower(?)) OR instr(lower(username),lower(?)) ORDER BY seen DESC LIMIT 21 OFFSET ?""",
                                   (q, q, q, 20 * page)).fetchall()
            entries = [(e(row["user_id"]), e(row["name"]), e(row["username"]),
                        link(url("/employees", add=row["user_id"]), "Допустить")) for row in rows[:20]]
            return table(("Telegram ID", "Имя", "Имя профиля", ""), entries) + pager("/employees", page, len(rows)>20, q=q, status=status)
        repo.db.create_function("fold", 1, str.casefold)
        condition = {"active": "AND active=1", "disabled": "AND active=0"}.get(status, "")
        rows = repo.db.execute(f"""SELECT * FROM trusted_users WHERE
            (instr(CAST(user_id AS TEXT),?) OR instr(fold(display_name),?) OR instr(fold(note),?))
            {condition} ORDER BY active DESC,user_id LIMIT 21 OFFSET ?""", (q, q.casefold(), q.casefold(), 20 * page)).fetchall()
        entries = [(link(url("/employees", id=row["user_id"]), row["display_name"] or str(row["user_id"])),
                    e(row["user_id"]), e(row["note"]), badge(row["active"], "Допущен")) for row in rows[:20]]
        return table(("Сотрудник", "Telegram ID", "Примечание", "Доступ"), entries) + pager("/employees", page, len(rows)>20, q=q, status=status)

    def page_content(path, request, repo, actor, page, q, status):
        params = request.query_params
        if path == "/":
            return PAGES[path], dashboard(repo)
        if path == "/employees":
            if value := params.get("id"):
                return employee_card(repo, actor, integer(value))
            create = form("/web/employee", hidden("action", "give") + field("uid", "Telegram ID", params.get("add", ""),
                           required=True, length=16) + field("name", "Имя", length=64) + field("note", "Примечание"),
                          "Предоставить доступ", csrf=actor.csrf)
            return PAGES[path], card("Записи доступа", search(path, q, status) + employees_panel(repo, actor, page, q, status)) + card("Новый сотрудник", create)
        if path == "/admins":
            rows, more = repo.admins(page)
            data = [(e(uid), form("/web/admin", hidden("uid", uid) + hidden("action", "remove"), "Отозвать",
                                 csrf=actor.csrf, confirmation="Отозвать права администратора?", quiet=True) if actor.uid == repo.owner else "")
                    for uid in rows]
            content = table(("Telegram ID", "Полномочия"), data) + pager(path, page, more)
            if actor.uid == repo.owner:
                content += form("/web/admin", field("uid", "Telegram ID", required=True, length=16),
                                "Назначить администратора", csrf=actor.csrf, confirmation="Предоставить административные права?")
            return PAGES[path], card("Административный доступ", content)
        if path == "/catalog":
            if value := params.get("id"):
                record = repo.get(integer(value))
                if not record:
                    raise InfraError(404, "barcode_not_found")
                return record.name, catalog_card(record, actor)
            create = form("/web/barcode", field("name", "Название", required=True, length=64) +
                          field("value", "Code 128", required=True, length=128), "Добавить", csrf=actor.csrf)
            return PAGES[path], card("Каталог", search(path, q, status) + barcode_rows(repo, actor, page, q, status)) + card("Новый штрихкод", create)
        if path == "/devices":
            rows = [dict(row) for row in repo.db.execute(
                "SELECT * FROM infra_devices ORDER BY created DESC,id LIMIT 21 OFFSET ?", (page * 20,))]
            return PAGES[path], device_list(repo, actor, rows[:20]) + pager(path, page, len(rows)>20)
        if path == "/features":
            return PAGES[path], features_panel(actor, repo)
        if path == "/telegram":
            bot, memory, uptime = service_info()
            return PAGES[path], metrics([("Служба", bot), ("Память", memory), ("Время работы", uptime)]) + card(
                "Настройки Telegram", "<p>Формат выдачи ШК: " + e(service.settings().barcode_mode) +
                ".</p><p>Запуск, остановка, токен и владелец управляются через sudo numberbot.</p>")
        if path == "/android":
            counts = repo.db.execute("SELECT count(*),count(revoked) FROM infra_devices").fetchone()
            versions = repo.db.execute("SELECT app_version,count(*) FROM infra_devices WHERE app_version<>'' GROUP BY app_version ORDER BY app_version LIMIT 50").fetchall()
            current = service.policy(repo)
            return PAGES[path], metrics([("Контракт API", "v1"), ("Активные устройства", counts[0]-counts[1]),
                                         ("Отозванные устройства", counts[1]), ("Telegram Export", "Включён" if current["telegram_export"] and current["android_export"] else "Отключён")]) + \
                card("Версии клиентов", table(("Версия", "Устройств"), [(e(v), e(n)) for v, n in versions])) + \
                card("Синхронизация", '<p>Каталог синхронизируется по ревизии. Android-клиент ещё не выпущен.</p>')
        if path == "/audit":
            return PAGES[path], card("Последние изменения", table(("Время", "Инициатор", "Действие", "Объект"),
                       [tuple(e(value) for value in row) for row in repo.recent()]))
        if path == "/storage":
            root = service.path.parent
            usage = disk_usage(root)
            preview = sweep(service.path, backups=None)
            counts = table(("Категория", "Подлежит очистке"),
                           [(e(name), e(value)) for name, value in preview["counts"].items()])
            return PAGES[path], card("Предварительный просмотр очистки",
                                      "<p>Режим: " + e("утверждён" if preview["enabled"] else "без удаления") +
                                      "</p>" + counts) + metrics([("SQLite", "Доступна"), ("Размер базы", f"{service.path.stat().st_size/1024:.1f} КиБ"),
                                         ("Свободно", f"{usage.free/1024**3:.1f} ГиБ")]) + card("Резервное копирование", "<p>Создание копий и восстановление выполняются через sudo numberbot.</p>")
        if path == "/security":
            settings = service.settings()
            rows = [(e(key), e(getattr(settings, key)), e(f"{lo}–{hi}")) for key, (lo, hi) in LIMITS.items()]
            aggregate = app.state.metrics.snapshot()
            installed = ROOT / "RELEASE_VERIFIED"
            evidence = installed.read_text().strip() if installed.is_file() else "Нет квитанции установщика"
            return PAGES[path], card("Проверка при установке", "<p>" + e(evidence) + "</p>") + card("Счётчики API с момента запуска",
                       table(("Показатель", "Значение"), [(e(k), e(v)) for k, v in aggregate.items()])) + card("Anti-Abuse", table(("Параметр", "Значение", "Диапазон"), rows) +
                                     "<p>Ограничения меняются в панели VPS.</p>")
        if path == "/diagnostics":
            status = repo.db.execute("PRAGMA quick_check(1)").fetchone()[0] if params.get("check") == "sqlite" else None
            return PAGES[path], card("Локальная проверка", "<p>API отвечает. " +
                                     ("SQLite: " + e(status) if status else "Проверка SQLite не запускалась.") + "</p>" +
                                     link(url(path, check="sqlite"), "Проверить SQLite") +
                                     "<p>" + e(last_check.get("label", "Telegram API: проверка не запускалась.")) + "</p>" +
                                     form("/web/telegram-check", hidden("check", "telegram"), "Проверить Telegram API", csrf=actor.csrf) +
                                     "<p>Журнал systemd и проверка GitHub доступны через sudo numberbot.</p>")
        raise InfraError(404, "not_found")

    def ui_page(request: Request):
        path = request.url.path
        try:
            page = min(10000, max(0, int(request.query_params.get("page", "0"))))
            query = request.query_params.get("q", "")[:64]
            state = request.query_params.get("status", "all")
            with service.repository() as repo:
                actor = identify(request, repo)
                title, body = page_content(path, request, repo, actor, page, query, state)
                return layout(title, body, identity=actor, current=path, notice=request.query_params.get("notice"),
                              owner=actor.uid == repo.owner)
        except InfraError as error:
            if error.status in {401, 403}:
                return redirect("/login")
            return HTMLResponse("<h1>Запись недоступна</h1>", status_code=error.status)
        except ValueError:
            return redirect(path, "form_error")

    for path in PAGES:
        app.add_api_route(path, ui_page, methods=["GET"], include_in_schema=False)

    @app.post("/web/telegram-check", include_in_schema=False)
    async def check_telegram(request: Request):
        data = await fields(request)
        with service.repository() as repo:
            current = identify(request, repo)
            if not compare_digest(data.get("csrf", ""), current.csrf):
                raise InfraError(403, "csrf")
        if monotonic() < last_check.get("next", 0):
            return redirect("/diagnostics")
        last_check["next"] = monotonic() + 15
        status = await telegram_check(current.id)
        label = {"ok": "Telegram подтвердил getMe", "failed": "Telegram не подтвердил getMe",
                 "busy": "Проверка уже выполняется", "unavailable": "Служба Telegram недоступна"}[status]
        last_check["label"] = label + " · " + clock(int(time()))
        return redirect("/diagnostics")

    class AdminStatus(BaseModel):
        role: str
        telegram: str
        users: int
        administrators: int
        active_codes: int
        active_devices: int

    @app.get("/api/v1/admin/status", response_model=AdminStatus)
    def admin_status(request: Request, authentication: Annotated[str | None, Security(browser_session)]):
        with service.repository() as repo:
            identity = service.identify(repo, request.cookies.get(session_cookie) if config.local else authentication, "web")
            return {
                "role": "owner" if identity.uid == repo.owner else "administrator",
                "telegram": service_info()[0],
                "users": repo.db.execute("SELECT count(*) FROM trusted_users WHERE active=1").fetchone()[0],
                "administrators": repo.db.execute("SELECT count(*) FROM admins").fetchone()[0],
                "active_codes": repo.db.execute("SELECT count(*) FROM barcodes WHERE active=1").fetchone()[0],
                "active_devices": repo.db.execute("SELECT count(*) FROM infra_devices WHERE revoked IS NULL").fetchone()[0],
            }

    @app.post("/web/{resource}", include_in_schema=False)
    async def mutate(resource: str, request: Request):
        data = await fields(request)
        paths = {"employee": "/employees", "admin": "/admins", "barcode": "/catalog",
                 "device": "/devices", "features": "/features"}
        if resource not in paths:
            raise InfraError(404, "action_not_found")
        target = paths[resource]
        try:
            with service.repository() as repo:
                actor = identify(request, repo)
                if not compare_digest(data.get("csrf", ""), actor.csrf):
                    raise InfraError(403, "csrf")
                action = data.get("action", "")
                high_risk = "role" if resource == "admin" else resource if resource == "features" else (
                    "devices" if resource == "device" and not data.get("id") else None)
                if high_risk:
                    if actor.uid != repo.owner:
                        raise InfraError(403, "owner_required")
                    mark = fingerprint([actor.id, {key: value for key, value in data.items() if key != "csrf"}])
                    if not consume(repo.db, actor.uid, high_risk, mark):
                        prompt = "<p>Подтверди указанное действие с VPS. Срок — три минуты.</p><p><code>" + e(
                            f"sudo numberbot approve {high_risk} {mark}") + "</code></p>"
                        repeat = form(request.url.path,
                                      "".join(hidden(key, value) for key, value in data.items() if key != "csrf"),
                                      "Выполнить подтверждённое действие", csrf=actor.csrf)
                        return layout("Подтверждение с VPS", card("Критическое действие", prompt + repeat),
                                      identity=actor, current=target, owner=True)
                if resource == "employee":
                    uid = integer(data.get("uid"))
                    if action not in {"give", "name", "note", "disable", "restore"}:
                        raise ValueError
                    target = url(target, id=uid) if action != "give" else target
                    repo.change_employee(actor.uid, uid, action, int(data.get("revision", "0")),
                                         name=data.get("name", data.get("value", "")), note=data.get("note", data.get("value", "")))
                elif resource == "admin":
                    if actor.uid != repo.owner:
                        raise InfraError(403, "owner_required")
                    repo.set_admin(actor.uid, integer(data.get("uid")), action != "remove")
                elif resource == "device":
                    uid = integer(data.get("uid"))
                    device = data.get("id")
                    if device and not ID.fullmatch(device):
                        raise ValueError
                    if data.get("back") == "employee":
                        target = url("/employees", id=uid)
                    service.revoke(actor.uid, uid, device, admin=True)
                elif resource == "barcode":
                    record = None
                    if data.get("id"):
                        rid = integer(data["id"])
                        record = repo.get(rid)
                        if not record:
                            raise InfraError(404, "barcode_not_found")
                        record = replace(record, revision=integer(data.get("revision")))
                    if record and action in {"delete", "toggle"}:
                        repo.change(actor.uid, record, delete=action == "delete")
                    else:
                        saved = repo.save(actor.uid, data.get("name", ""), data.get("value", ""), record)
                        target = url(target, id=saved.id)
                elif data.get("mode") == "common":
                    keys = features.current().options()
                    if actor.uid != repo.owner:
                        raise InfraError(403, "owner_required")
                    write_common(repo.db, {key: key in data for key in keys}, data.get("revision", ""),
                                 features.legacy.current,
                                 lambda: repo.log(actor.uid, "Изменены общие функции", "Переключатели"))
                elif data.get("mode") == "client":
                    service.set_policy(actor.uid, {key: key in data for key in CLIENT_FEATURES}, data.get("revision", ""))
                else:
                    raise ValueError
        except InfraError as error:
            if error.status in {401, 403}:
                raise
            return redirect(target, "conflict")
        except CatalogError:
            return redirect(target, "conflict")
        except ValueError:
            return redirect(target, "form_error")
        return redirect(target, "ok")
