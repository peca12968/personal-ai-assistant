"""Bounded stored-text projection of DOCX packages, without Word or extraction."""
import io
import posixpath
import re
import zipfile
import zlib
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

ENCODING = 'docx-text-v1'
W = ('http://schemas.openxmlformats.org/wordprocessingml/2006/main',
     'http://purl.oclc.org/ooxml/wordprocessingml/main')
R = ('http://schemas.openxmlformats.org/officeDocument/2006/relationships',
     'http://purl.oclc.org/ooxml/officeDocument/relationships')
PKG = 'http://schemas.openxmlformats.org/package/2006/relationships'
MAX_PART = 2 * 1024 * 1024
MAX_XML = 8 * 1024 * 1024
LIMITATIONS = ('DOCX儲存文字的抽取行號，不是Word頁碼或原版面；表格按實體儲存格順序標示。'
               '含正文及相關頁首頁尾、註腳、章節附註、註解；附屬文字分區呈現，不重建每頁位置。'
               '註記#號為封裝ID，不保證等同Word顯示編號。'
               '不做OCR，不解析圖片、圖表、文字方塊、方程式或嵌入物件；不重建清單編號、合併格版面或樣式。'
               '可能包含隱藏文字；欄位僅取已儲存的顯示文字，不更新或執行欄位、巨集與外部連結。')


def metadata(document):
    if document.get('encoding') == ENCODING:
        return {'representation': ENCODING, 'citation_basis': 'extracted_text_lines',
                'fingerprint_basis': 'archive_and_projected_text', 'extraction_limits': LIMITATIONS}
    return {}


class LimitedTree(ET.TreeBuilder):
    def __init__(self, budget=None):
        super().__init__(); self.depth = self.nodes = 0
        self.budget = budget if budget is not None else [0]

    def doctype(self, *args):
        raise ValueError('DOCX不接受DTD或自訂實體。')

    def start(self, tag, attrs):
        self.depth += 1; self.nodes += 1; self.budget[0] += 1
        if self.depth > 128 or self.nodes > 50000 or self.budget[0] > 200000:
            raise ValueError('DOCX XML層數或節點超過上限。')
        return super().start(tag, attrs)

    def end(self, tag):
        self.depth -= 1
        return super().end(tag)


def target(base, value):
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError('DOCX必要內容不能指向外部或片段。')
    value = unquote(parsed.path, errors='strict')
    if '\\' in value or any(ord(c) < 32 for c in value):
        raise ValueError('DOCX部件路徑無效。')
    path = posixpath.normpath(value.lstrip('/') if value.startswith('/') else posixpath.join(posixpath.dirname(base), value))
    if path in ('', '.', '..') or path.startswith('../'):
        raise ValueError('DOCX部件路徑超出封裝。')
    return path


def project(raw):
    """All returned lines belong to this deterministic projection, not Word pages."""
    if len(raw) > MAX_PART: raise ValueError('DOCX原檔最多2MiB。')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > 512 or sum(e.file_size for e in entries) > 32 * 1024 * 1024:
                raise ValueError('DOCX封裝超過512部件或32MiB解壓宣告上限。')
            names = [e.filename for e in entries]
            if len(names) != len(set(names)) or any(e.flag_bits & 1 for e in entries):
                raise ValueError('DOCX有重複部件或加密內容。')
            if any(n.startswith('/') or '\\' in n or '..' in n.split('/') or
                   any(ord(c) < 32 for c in n) for n in names):
                raise ValueError('DOCX封裝部件名稱無效。')
            cache, consumed, node_budget = {}, 0, [0]

            def xml(name):
                nonlocal consumed
                if name in cache: return cache[name]
                info = archive.getinfo(name)
                if info.file_size > MAX_PART or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise ValueError('DOCX XML部件超過2MiB或壓縮方式不支援。')
                with archive.open(info) as source: data = source.read(MAX_PART + 1)
                consumed += len(data)
                if len(data) > MAX_PART or consumed > MAX_XML:
                    raise ValueError('DOCX解壓XML超過單部件2MiB或合計8MiB上限。')
                root = ET.fromstring(data, parser=ET.XMLParser(target=LimitedTree(node_budget)))
                cache[name] = root
                return root

            def relations(part):
                name = posixpath.join(posixpath.dirname(part), '_rels', posixpath.basename(part) + '.rels') if part else '_rels/.rels'
                if name not in names: return {}
                root = xml(name)
                if root.tag != '{'+PKG+'}Relationships': raise ValueError('DOCX關聯格式無效。')
                result = {}
                for row in root:
                    key = row.get('Id')
                    if row.tag != '{'+PKG+'}Relationship' or not key or key in result:
                        raise ValueError('DOCX關聯編號無效或重複。')
                    result[key] = dict(row.attrib)
                return result

            def related(part, relation):
                if relation.get('TargetMode', 'Internal') != 'Internal':
                    raise ValueError('DOCX必要文字部件為外部內容，未讀取。')
                return target(part, relation['Target'])

            roots = [r for r in relations('').values() if r.get('Type') in [ns+'/officeDocument' for ns in R]]
            if len(roots) != 1: raise ValueError('DOCX需有唯一主要文件關聯。')
            main = related('', roots[0])
            types = xml('[Content_Types].xml')
            ct = 'http://schemas.openxmlformats.org/package/2006/content-types'
            declared = [e.get('ContentType') for e in types if e.tag == '{'+ct+'}Override' and target('',e.get('PartName','')) == main]
            if types.tag != '{'+ct+'}Types' or declared != ['application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml']:
                raise ValueError('只支援一般DOCX，不支援巨集文件或其他Office格式。')
            document = xml(main)
            namespace = next((ns for ns in W if document.tag == '{'+ns+'}document'), None)
            if namespace is None: raise ValueError('DOCX主要文件格式不支援。')
            q = lambda name: '{'+namespace+'}'+name
            body = document.find(q('body'))
            if body is None or len(document.findall(q('body'))) != 1: raise ValueError('DOCX需有唯一正文。')
            links = relations(main)
            lines, byte_count, text_found, table_count = [], 0, False, 0
            notes = {name:set() for name in ('footnote','endnote','comment')}

            def emit(text):
                nonlocal byte_count
                byte_count += len(text.encode('utf-8')) + 1
                if byte_count > MAX_PART: raise ValueError('DOCX抽取文字超過2MiB。')
                lines.append(text)

            def inspect(root):
                for node in root.iter():
                    if node.tag.startswith('{'+namespace+'}'):
                        name = node.tag.split('}',1)[1]
                        if name in ('ins','del','delText','delInstrText','moveFrom','moveTo','cellIns','cellDel','cellMerge') or name.endswith('Change') or name.startswith(('moveFromRange','moveToRange','customXmlInsRange','customXmlDelRange','customXmlMove')):
                            raise ValueError('DOCX含尚未處理的修訂；請先在Word接受或拒絕修訂後另存。')
                        if name in ('altChunk','subDoc'):
                            raise ValueError('DOCX含替代或連結子文件，請先轉為一般正文。')
                    if node.tag.endswith(('}conflictIns','}conflictDel')):
                        raise ValueError('DOCX含衝突修訂，請先在Word處理後另存。')

            def note_id(node):
                value = node.get(q('id'), '')
                if not re.fullmatch(r'[0-9]{1,10}',value): raise ValueError('DOCX註記編號無效。')
                return str(int(value))

            def inline(node):
                nonlocal text_found
                if node.tag == q('t'):
                    value = node.text or ''; text_found = text_found or bool(value.strip())
                    return value
                if node.tag == q('tab'): return '\t'
                if node.tag in (q('br'),q('cr')): return '\n'
                if node.tag in (q('drawing'),q('pict'),q('object')) or node.tag.endswith(('}oMath','}oMathPara','}AlternateContent')):
                    return '[圖形／物件文字未解析]'
                if node.tag == q('sym'): return '[字型符號未轉換]'
                for kind,label in (('footnote','註腳'),('endnote','章節附註'),('comment','註解')):
                    if node.tag in (q(kind+'Reference'),q(kind+'RangeStart')):
                        key=note_id(node); notes[kind].add(key)
                        return f'[{label}#{key}]'
                if node.tag in (q('pPr'),q('rPr'),q('instrText')): return ''
                return ''.join(inline(child) for child in node)

            def containers(root, tag):
                for child in root:
                    if child.tag == q(tag): yield child
                    elif child.tag != q('tbl'): yield from containers(child, tag)

            def blocks(root):
                nonlocal table_count
                for node in root:
                    if node.tag == q('p'):
                        if node.find(q('pPr')+'/'+q('numPr')) is not None: emit('[清單項目；自動編號未展開]')
                        emit(inline(node))
                    elif node.tag == q('tbl'):
                        table_count += 1; number = table_count
                        emit(f'【表格{number}；依實體儲存格順序，非合併格版面】')
                        for ri,row in enumerate(containers(node,'tr'),1):
                            for ci,cell in enumerate(containers(row,'tc'),1):
                                emit(f'【表格{number}／列{ri}／儲存格{ci}】'); blocks(cell)
                    elif node.tag in (q('drawing'),q('pict'),q('object')) or node.tag.endswith('}AlternateContent'):
                        emit('[圖形／物件文字未解析]')
                    else: blocks(node)

            emit('【DOCX儲存文字抽取副本 v1】'); emit(LIMITATIONS)
            inspect(document); emit('【正文】'); blocks(body)
            seen = set()
            for node in document.iter():
                if node.tag not in (q('headerReference'),q('footerReference')): continue
                rid = next((node.get('{'+ns+'}id') for ns in R if node.get('{'+ns+'}id')), None)
                relation = links.get(rid)
                kind = 'header' if node.tag == q('headerReference') else 'footer'
                if not relation or relation.get('Type') not in [ns+'/'+kind for ns in R]:
                    raise ValueError('DOCX頁首頁尾關聯缺失。')
                part = related(main,relation)
                root=xml(part)
                if root.tag != q('hdr' if kind=='header' else 'ftr'): raise ValueError('DOCX頁首頁尾部件格式無效。')
                if part in seen: continue
                seen.add(part); inspect(root)
                emit('【'+('頁首' if kind=='header' else '頁尾')+'部件：'+part+'；未對應Word頁碼】'); blocks(root)
            # Referenced notes/comments only; ignore unreferenced historical parts.
            done = {kind:set() for kind in notes}
            for _ in range(4):
                for kind,label in (('footnote','註腳'),('endnote','章節附註'),('comment','註解')):
                    pending = notes[kind]-done[kind]
                    if not pending: continue
                    matches=[r for r in links.values() if r.get('Type') in [ns+'/'+kind+'s' for ns in R]]
                    if len(matches)!=1: raise ValueError('DOCX必要註記關聯缺失或重複。')
                    root=xml(related(main,matches[0]));inspect(root)
                    if root.tag!=q(kind+'s'): raise ValueError('DOCX註記部件格式無效。')
                    items={}
                    for item in root.findall(q(kind)):
                        if item.get(q('type')) in ('separator','continuationSeparator','continuationNotice'): continue
                        key=note_id(item)
                        if key in items: raise ValueError('DOCX註記編號重複。')
                        items[key]=item
                    for key in sorted(pending,key=int):
                        if key not in items: raise ValueError('DOCX引用的註記內容缺失。')
                        done[kind].add(key);emit(f'【{label}#{key}】');blocks(items[key])
            if any(notes[kind]-done[kind] for kind in notes): raise ValueError('DOCX註記鏈過深。')
            if not text_found: raise ValueError('DOCX沒有可分析的文字；圖片不會經OCR轉文字。')
            return '\n'.join(lines)
    except (zipfile.BadZipFile, KeyError, ET.ParseError, UnicodeError, RuntimeError, NotImplementedError, zlib.error, EOFError) as exc:
        raise ValueError('DOCX封裝損壞、缺少必要部件或格式不支援。') from exc
