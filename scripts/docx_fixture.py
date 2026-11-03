"""Deterministic synthetic DOCX fixtures, no user documents."""
import io
import zipfile
from xml.sax.saxutils import escape

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
P='http://schemas.openxmlformats.org/package/2006/relationships'
C='http://schemas.openxmlformats.org/package/2006/content-types'


def paragraph(text):return '<w:p><w:r><w:t xml:space="preserve">'+escape(text)+'</w:t></w:r></w:p>'


def package(body, *, parts=None, links=None, extra=None):
    parts=parts or {};links=links or []
    overrides=''.join(f'<Override PartName="/{name}" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.{kind}+xml"/>'
                      for name in parts for kind in ('header','footer','footnotes','endnotes','comments')
                      if name.rsplit('/',1)[-1].startswith(kind))
    values={'[Content_Types].xml':f'<Types xmlns="{C}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>{overrides}</Types>',
            '_rels/.rels':f'<Relationships xmlns="{P}"><Relationship Id="main" Type="{R}/officeDocument" Target="word/document.xml"/></Relationships>',
            'word/document.xml':f'<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body>{body}</w:body></w:document>',
            'word/_rels/document.xml.rels':f'<Relationships xmlns="{P}">'+''.join(f'<Relationship Id="{rid}" Type="{R}/{kind}" Target="{path}"/>' for rid,kind,path in links)+'</Relationships>'}
    values.update(parts);values.update(extra or {})
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,value in sorted(values.items()):
            info=zipfile.ZipInfo(name,(2020,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(info,value.encode('utf-8') if isinstance(value,str) else value)
    return output.getvalue()


def example():
    table='<w:tbl>'+''.join('<w:tr>'+''.join('<w:tc>'+paragraph(v)+'</w:tc>' for v in row)+'</w:tr>'
                          for row in [('項目','數量','單價'),('青鳥','3','120'),('白鷺','2','80')])+'</w:tbl>'
    body=paragraph('採購方案：只納入表格兩項，金額未含運費。')+table
    body+='<w:p><w:r><w:t>運費規則</w:t><w:footnoteReference w:id="1"/></w:r></w:p>'
    body+='<w:p><w:r><w:t>交期限制</w:t><w:endnoteReference w:id="2"/></w:r></w:p>'
    body+='<w:p><w:commentRangeStart w:id="3"/><w:r><w:t>備選折扣尚未確認。</w:t></w:r><w:commentRangeEnd w:id="3"/><w:r><w:commentReference w:id="3"/></w:r></w:p>'
    body+='<w:sectPr><w:headerReference w:type="default" r:id="h"/><w:footerReference w:type="default" r:id="f"/></w:sectPr>'
    parts={
        'word/header1.xml':f'<w:hdr xmlns:w="{W}">'+paragraph('專案代號：ORBIT-72')+'</w:hdr>',
        'word/footer1.xml':f'<w:ftr xmlns:w="{W}">'+paragraph('版本：R7，內部草案')+'</w:ftr>',
        'word/footnotes.xml':f'<w:footnotes xmlns:w="{W}"><w:footnote w:type="separator" w:id="-1"><w:p/></w:footnote><w:footnote w:id="1">'+paragraph('運費固定40元；兩項合併出貨只計一次。')+'</w:footnote></w:footnotes>',
        'word/endnotes.xml':f'<w:endnotes xmlns:w="{W}"><w:endnote w:id="2">'+paragraph('交期為確認訂單後5個日曆天。')+'</w:endnote></w:endnotes>',
        'word/comments.xml':f'<w:comments xmlns:w="{W}"><w:comment w:id="3">'+paragraph('審閱建議：若改買10組再另議折扣，尚非決議。')+'</w:comment><w:comment w:id="99">'+paragraph('UNREFERENCED_OLD_NOTE')+'</w:comment></w:comments>'}
    links=[('h','header','header1.xml'),('f','footer','footer1.xml'),('fn','footnotes','footnotes.xml'),
           ('en','endnotes','endnotes.xml'),('c','comments','comments.xml')]
    return package(body,parts=parts,links=links)
