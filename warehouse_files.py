"""Локальные адаптеры простых офисных документов."""
from collections import Counter
from base64 import b64decode, b64encode
from copy import copy, deepcopy
from io import BytesIO
import re
from zipfile import ZipFile
from lxml import etree
from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string
from docx import Document
from warehouse import alignment, blocks, compare, order


def inspect_zip(raw):
    with ZipFile(BytesIO(raw)) as z:
        files = z.infolist()
        if len(files)>300 or sum(f.file_size for f in files)>16*1024**2 or len(set(z.namelist()))!=len(files):
            raise ValueError("Слишком большой или неоднозначный архив.")
        for f in files:
            name = f.filename.lower()
            if name.endswith((".xml",".rels")):
                root = etree.fromstring(z.read(f), etree.XMLParser(resolve_entities=False, no_network=True))
                if root.getroottree().docinfo.doctype or any(e.get("TargetMode")=="External" for e in root.iter()):
                    raise ValueError("Документ содержит внешние ссылки или DTD.")
            elif name.endswith(".bin"):
                raise ValueError("Встроенные программы не поддерживаются.")


def identifier(cell):
    v = cell.value
    if v is None:
        return None
    if isinstance(v,str) and cell.data_type not in {"f","e"}:
        return v
    if type(v) is int and len(str(abs(v)))<=15:
        if cell.number_format in ("General","0"):
            return str(v)
        if re.fullmatch("0+",cell.number_format) and v>=0:
            return str(v).zfill(len(cell.number_format))
    raise ValueError("Используй текстовые идентификаторы или целые числа с простым форматом.")


class Source:
    def __init__(self, item, opts, mode, limit):
        self.kind = item["kind"]
        raw = b64decode(item["data"])
        self.table = False
        self.opts = opts
        self.groups = []
        self.positions = []
        if self.kind in {"xlsx","docx"}:
            inspect_zip(raw)
        if self.kind=="xlsx":
            self.book = load_workbook(BytesIO(raw))
            if len(self.book.worksheets)>16 or any(s.max_column*s.max_row>50000 for s in self.book.worksheets):
                raise ValueError("Превышен размер таблицы.")
            if len(self.book.worksheets)>1 and "sheet" not in opts:
                raise ValueError("Выбери лист командой настройки. Листы: "+", ".join(self.book.sheetnames))
            self.sheet = self.book.worksheets[int(opts.get("sheet",1))-1]
            if any(s.merged_cells or s.tables or s._charts or s._images or any(c.data_type=="f" for c in s._cells.values()) for s in self.book.worksheets):
                raise ValueError("Сохрани простую копию без формул, объединений и объектов.")
            if self.sheet.conditional_formatting or self.sheet.auto_filter.filterColumn or self.sheet.data_validations and self.sheet.data_validations.count:
                raise ValueError("Сохрани простую копию таблицы без фильтров и правил оформления.")
            self.rows = list(self.sheet.rows)
            self.table = True
        elif self.kind=="docx":
            self.book = Document(BytesIO(raw))
            if sum(1 for _ in self.book.element.iter())>60000:
                raise ValueError("Слишком большой документ.")
            if self.book.tables and opts.get("sheet")!="0":
                if len(self.book.tables)>1 and "sheet" not in opts:
                    raise ValueError("Укажи номер нужной таблицы.")
                self.wordtable = self.book.tables[int(opts.get("sheet",1))-1]
                self.rows = [r.cells for r in self.wordtable.rows]
                if any(len(row)!=len(self.wordtable.columns) for row in self.rows) or self.wordtable._tbl.xpath('.//w:gridSpan|.//w:vMerge|.//w:tbl'):
                    raise ValueError("Нужна простая прямоугольная таблица Word.")
                self.table = True
            else:
                self.paras=self.book.paragraphs
                lo, hi = map(int,opts.get("area",f"1-{len(self.paras)}").split("-"))
                if not 1<=lo<=hi<=len(self.paras): raise ValueError("Проверь диапазон абзацев.")
                self.paras=self.paras[lo-1:hi]
                if "area" not in opts and any(len(p.text.split())>3 for p in self.paras):
                    raise ValueError("Выбери область абзацев: например «область 2-15».")
                lines=[p.text for p in self.paras]
        else:
            self.encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else opts.get("encoding","utf-8")
            lines=re.split(r"\r\n|\r|\n",raw.decode(self.encoding))
            if any("\x00" in s for s in lines): raise ValueError("Ожидается текстовый файл.")
        if self.table:
            self.start=int(opts.get("header",0))
            width=len(self.rows[0])
            self.a=column_index_from_string(opts.get("a","A"))-1
            self.b=column_index_from_string(opts.get("b","B"))-1
            self.link=[column_index_from_string(x)-1 for x in opts.get("link","").split(",") if x]
            if (width>2 if mode=="align" else width>1) and "a" not in opts:
                raise ValueError("Выбери столбцы через настройку «столбцы».")
            cols=[self.a]+([self.b,*self.link] if mode=="align" else [])
            if max(cols)>=width or len(set(cols))!=len(cols): raise ValueError("Проверь выбранные столбцы.")
            for col in cols[:2]:
                group=[]; positions=[]
                for i,row in enumerate(self.rows[self.start:],self.start):
                    val=identifier(row[col]) if self.kind=="xlsx" else row[col].text
                    if val not in (None,""):
                        group.append(val);positions.append(i)
                    elif mode!="align" and any((c.value if self.kind=="xlsx" else c.text) not in (None,"") for c in row):
                        raise ValueError("В заполненной строке отсутствует ключ сортировки.")
                    elif mode=="align" and col==self.b and any((row[j].value if self.kind=="xlsx" else row[j].text) not in (None,"") for j in self.link):
                        raise ValueError("У связанных данных отсутствует идентификатор.")
                self.groups.append(group);self.positions.append(positions)
        else:
            if len(lines)>limit*2+2: raise ValueError("Слишком много строк в источнике.")
            self.groups=blocks(lines)
            self.positions=[[i for i,line in enumerate(lines) if line.strip()]]
        if not self.groups or not any(self.groups) or sum(map(len,self.groups))>limit:
            raise ValueError("Пустой список или превышен предел строк.")

    def output(self, orders, align=False):
        lists=[[group[i] for i in ix] for group,ix in zip(self.groups,orders)]
        if not self.table:
            if self.kind=="text": return lists
            if self.kind=="txt":
                return "\n\n".join("\n".join(g) for g in lists).encode(self.encoding)
            targets=[self.paras[i]._p for i in self.positions[0]]
            offset=0
            for group,ix in zip(self.groups,orders):
                originals=[deepcopy(x) for x in targets[offset:offset+len(group)]]
                for idx,node in enumerate(targets[offset:offset+len(group)]):
                    if idx<len(ix): node.addprevious(originals[ix[idx]])
                    node.getparent().remove(node)
                offset+=len(group)
        else:
            width=len(self.rows[0])
            if align:
                matched,extra=alignment(*self.groups)
                cols=[self.b,*self.link]
                moves={dest:self.positions[1][idx] for dest,idx in zip(self.positions[0],matched) if idx is not None}
                for j,idx in enumerate(extra,len(self.rows)): moves[j]=self.positions[1][idx]
                check=[self.groups[1][i] for i in matched if i is not None]+[self.groups[1][i] for i in extra]
                if Counter(check)!=Counter(self.groups[1]):raise ValueError("Ошибка сопоставления.")
            else:
                cols=range(width)
                moves={dest:self.positions[0][i] for dest,i in zip(self.positions[0],orders[0])}
            if self.kind=="xlsx":
                saved={(i,c):copy(self.rows[i][c]) for i in set(moves.values()) for c in cols}
                for i in range(self.start,max([len(self.rows)-1,*moves])+1):
                    for c in cols:
                        cell=self.sheet.cell(i+1,c+1)
                        if i in moves:
                            old=saved[moves[i],c];cell.value=old.value;cell.data_type=old.data_type;cell._style=copy(old._style)
                        else: cell.value=None
            else:
                saved={(i,c):deepcopy(self.rows[i][c]._tc) for i in set(moves.values()) for c in cols}
                for i in range(self.start,max([len(self.rows)-1,*moves])+1):
                    row=self.rows[i] if i<len(self.rows) else self.wordtable.add_row().cells
                    for c in cols:
                        if i in moves:
                            node=row[c]._tc;node.getparent().replace(node,deepcopy(saved[moves[i],c]))
                        else: row[c].text=""
        stream=BytesIO()
        self.book.save(stream)
        if self.table:
            if self.kind=="xlsx":
                reopened=load_workbook(BytesIO(stream.getvalue()))[self.sheet.title]
                rows=list(reopened.rows)
                read=identifier
            else:
                reopened=Document(BytesIO(stream.getvalue()))
                rows=[r.cells for r in reopened.tables[int(self.opts.get("sheet",1))-1].rows]
                read=lambda cell:cell.text
            values=[read(r[self.a]) for r in rows[self.start:]]
            if [v for v in values if v not in (None,"")] != (self.groups[0] if align else lists[0]):
                raise ValueError("Контрольное чтение результата обнаружило изменение строк.")
            if align:
                matched,extra=alignment(*self.groups)
                actual=[read(r[self.b]) for r in rows[self.start:]]
                target=[None]*(len(rows)-self.start)
                for dest,idx in zip(self.positions[0],matched):
                    if idx is not None:target[dest-self.start]=self.groups[1][idx]
                for dest,idx in enumerate(extra,len(self.rows)-self.start):target[dest]=self.groups[1][idx]
                if [v if v!="" else None for v in actual]!=target:
                    raise ValueError("Контрольное чтение обнаружило расхождение в проверяемом столбце.")
        return stream.getvalue()


def process(request):
    mode=request["mode"]
    sources=[Source(it,it.get("opts",{}),mode,request["limit"]) for it in request["items"]]
    groups=[g for src in sources for g in src.groups]
    if mode in {"quick","ref","align"}:
        if len(groups)!=2 or mode=="align" and not sources[0].table or mode=="ref" and len(sources)!=2:
            raise ValueError("Для сверки нужны ровно два списка. Раздели блоки пустой строкой либо выбери сверку с эталоном.")
        report=compare(*groups)
    else:
        report=[]
    outputs=[]
    for src in sources:
        orders=[]
        for g in src.groups:
            ix,info=order(g,mode,request.get("expected"));orders.append(ix)
            if mode not in {"quick","ref","align"}:report.extend(info)
        out=src.output(orders,mode=="align")
        outputs.append({"kind":src.kind,"data":out if isinstance(out,list) else b64encode(out).decode()})
    return {"report":"\n".join([*report,"Потерь: 0"]), "outputs":outputs}
