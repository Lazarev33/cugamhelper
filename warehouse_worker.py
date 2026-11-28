"""Вычислительный процесс обработки документов."""

import json
import resource
import sys

from private_process import disable_core_dumps


def process_bytes(data, validator=None):
    from warehouse_files import process

    try:
        request = json.loads(data)
        if validator:
            request = validator(request)
        result = json.dumps(process(request), ensure_ascii=False).encode()
        if len(result) > 8 * 1024**2:
            raise ValueError("Слишком большой результат.")
        return result
    except UnicodeDecodeError:
        result = "Нужен UTF-8. Для Windows-1251 отправь «кодировка 1251», затем файл."
    except ValueError as error:
        result = str(error)[:300]
    except Exception:
        result = "Повреждённый документ или неподдерживаемая структура."
    return json.dumps({"error": result}).encode()


if __name__ == "__main__":
    disable_core_dumps()
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    sys.stdout.buffer.write(process_bytes(sys.stdin.buffer.read(12 * 1024**2 + 1)))
