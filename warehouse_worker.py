"""Вычислительный процесс без доступа к токену через окружение."""
import json
import resource
import sys

from private_process import disable_core_dumps

if __name__=="__main__":
    disable_core_dumps()
    resource.setrlimit(resource.RLIMIT_AS,(512*1024**2,512*1024**2))
    from warehouse_files import process
    try:
        result=json.dumps(process(json.load(sys.stdin)),ensure_ascii=False).encode()
        if len(result)>8*1024**2: raise ValueError("Слишком большой результат.")
    except UnicodeDecodeError:
        result=json.dumps({"error":"Нужен UTF-8. Для Windows-1251 отправь «кодировка 1251», затем файл."}).encode()
    except ValueError as error:
        result=json.dumps({"error":str(error)[:300]}).encode()
    except Exception:
        result=json.dumps({"error":"Повреждённый документ или неподдерживаемая структура."}).encode()
    sys.stdout.buffer.write(result)
