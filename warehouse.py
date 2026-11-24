"""Точное сравнение складских записей. Индексы позволяют перемещать связанные строки."""
from collections import Counter, defaultdict, deque
import re


def natural(value):
    parts = re.split(r"([0-9]+)", value)
    return tuple((1, len(p.lstrip("0")), p.lstrip("0")) if i % 2 else (0, p)
                 for i, p in enumerate(parts)), value


def blocks(lines):
    result = []
    group = []
    for line in lines:
        if line.strip():
            group.append(line)
        elif group:
            result.append(group)
            group = []
    if group:
        result.append(group)
    return result


def details(title, counts):
    return [title, *(f"{key} — {counts[key]}" for key in sorted(counts, key=natural))] if counts else []


def compare(first, second):
    a, b = Counter(first), Counter(second)
    missing, extra = a-b, b-a
    return ["Обнаружены расхождения." if missing or extra else "Сверка завершена.",
            f"Сошлось: {(a & b).total()}/{len(first)}", f"Первый список: {len(first)}",
            f"Второй список: {len(second)}", f"Не хватает: {missing.total()}", f"Лишних: {extra.total()}",
            *details("Отсутствует во втором списке:", missing), *details("Лишние значения:", extra)]


def alignment(first, second):
    buckets = defaultdict(deque)
    for i, value in enumerate(second):
        buckets[value].append(i)
    matched = [buckets[v].popleft() if buckets[v] else None for v in first]
    extras = sorted(i for bucket in buckets.values() for i in bucket)
    if Counter(i for i in [*matched,*extras] if i is not None) != Counter(range(len(second))):
        raise ValueError("Не удалось сохранить экземпляры при сопоставлении.")
    return matched, extras


def order(values, mode, expected=None):
    counts = Counter(values)
    indices = sorted(range(len(values)), key=lambda i: natural(values[i]), reverse=mode=="desc")
    report = []
    if mode == "pairs":
        singles = {v:n for v,n in counts.items() if n==1}
        over = {v:n for v,n in counts.items() if n>2}
        good = sum(n==2 for n in counts.values())
        bad = singles or over or expected is not None and expected != len(counts)
        report = ["Обнаружены расхождения." if bad else "Сверка завершена.",
                  f"Сошлось: {good}/{expected if expected is not None else len(counts)}",
                  f"Полных пар: {good}", f"Не хватает повторений: {len(singles)}",
                  f"Лишних повторений: {sum(n-2 for n in over.values())}",
                  *details("Не хватает пары для:", singles), *details("Более двух экземпляров:", over)]
        if expected is not None and expected != len(counts):
            report.append(f"Ожидалось уникальных: {expected}. Получено: {len(counts)}. Полностью отсутствующий код можно назвать только по эталону.")
    elif mode in {"dups","unique"}:
        repeated = {v:n for v,n in counts.items() if n>1}
        report = [f"Повторяющихся идентификаторов: {len(repeated)}",
                  f"Лишних экземпляров: {len(values)-len(counts)}", *details("Дубликаты:", repeated)]
        if mode=="unique":
            indices = [i for pos,i in enumerate(indices) if not pos or values[i]!=values[indices[pos-1]]]
            report.append(f"Удалено экземпляров: {len(values)-len(indices)}")
    else:
        report.append("Сортировка завершена.")
    actual = Counter(values[i] for i in indices)
    if actual != (Counter(counts.keys()) if mode=="unique" else counts):
        raise ValueError("Проверка сохранности не пройдена.")
    return indices, report+[f"Получено строк: {len(values)}",f"Возвращено строк: {len(indices)}",f"Уникальных: {len(counts)}"]
