"""Visible linked contents with bookmark page fields, independent of stale TOC caches."""
from common import *

def set_ptext(p,value):
    ts=p.xpath('.//w:t',namespaces=NS)
    if ts:
        ts[0].text=value
        for t in ts[1:]:t.text=''
    else:E.SubElement(E.SubElement(p,W+'r'),W+'t').text=value

def _run(parent,value=None,bold=False,size=21):
    r=E.SubElement(parent,W+'r');pr=E.SubElement(r,W+'rPr')
    f=E.SubElement(pr,W+'rFonts')
    for k,v in [('ascii','Arial'),('hAnsi','Arial'),('eastAsia','宋体')]:f.set(W+k,v)
    E.SubElement(pr,W+'sz').set(W+'val',str(size))
    E.SubElement(pr,W+'color').set(W+'val','000000')
    if bold:E.SubElement(pr,W+'b')
    if value is not None:E.SubElement(r,W+'t').text=value
    return r

def entries_for(root):
    body=root.find(W+'body');entries=[]
    def paragraph(label):
        ps=[p for p in body.findall(W+'p') if norm(text(p))==label]
        if len(ps)!=1:raise ValueError('目录目标不唯一：'+label)
        return ps[0]
    entries.append(('basic','一、电站基本信息',0,paragraph('电站基本信息')))
    entries.append(('overview','二、报告概况',0,paragraph('报告概况')))
    overview=body.findall(W+'tbl')[1]
    for key,prefix,label in [('workload','一、定检工作量','定检工作量及完成情况统计'),('defects','二、定检缺陷','定检缺陷隐患及处理'),('plan','三、遗留问题','遗留问题整改计划'),('conclusion','四、结论','结论')]:
        row=next(r for r in overview.findall(W+'tr') if norm(text(r)).startswith(prefix))
        entries.append(('overview.'+key,label,1,row.find('./'+W+'tc/'+W+'p')))
    entries.append(('inspection','三、定检报告',0,paragraph('定检报告')))
    modules=root.xpath('./w:body/w:sdt[w:sdtPr/w:tag[starts-with(@w:val,"equipment:")]]',namespaces=NS)
    for i,x in enumerate(modules,1):
        key=x.find('./'+W+'sdtPr/'+W+'tag').get(W+'val').split(':')[1]
        content=x.find(W+'sdtContent');heading=content.find(W+'p')
        label=f'{i}、{MODULE_NAMES[key]}';set_ptext(heading,label)
        for n in heading.xpath('./w:pPr/w:numPr',namespaces=NS):n.getparent().remove(n)
        entries.append(('equipment.'+key,label,1,heading))
        clauses=content.xpath('./w:sdt[w:sdtPr/w:tag[starts-with(@w:val,"clause:")]]',namespaces=NS)
        if clauses:
            cc=clauses[0].find(W+'sdtContent');target=cc.find('.//'+W+'p')
            entries.append(('clause.'+key,'定检要求' if key=='other' else '检查标准表',2,target))
        tables=[t for t in content.findall(W+'tbl') if '结果汇总表' in text(t.find(W+'tr'))]
        if len(tables)!=1:raise ValueError('结果表目标不唯一：'+key)
        entries.append(('result.'+key,'定检结果表',2,tables[0].find('./'+W+'tr/'+W+'tc/'+W+'p')))
    tickets=root.xpath('./w:body/w:sdt[w:sdtPr/w:tag[@w:val="tickets"]]',namespaces=NS)
    if tickets:
        p=tickets[0].find('./'+W+'sdtContent/'+W+'p');set_ptext(p,'四、两票执行表')
        for n in p.xpath('./w:pPr/w:numPr',namespaces=NS):n.getparent().remove(n)
        entries.append(('tickets','四、两票执行表',0,p))
    return entries

def write_toc(root,pages=None):
    pages=pages or {}
    controls=root.xpath('.//w:sdt[w:sdtPr/w:tag[@w:val="report_toc"]]',namespaces=NS)
    if not controls:controls=root.xpath('.//w:sdt[.//w:instrText[contains(text(),"TOC")]]',namespaces=NS)
    if len(controls)!=1:raise ValueError('目录控件缺失或重复')
    control=controls[0];pr=control.find(W+'sdtPr')
    if pr is None:pr=E.SubElement(control,W+'sdtPr')
    tag=pr.find(W+'tag')
    if tag is None:tag=E.SubElement(pr,W+'tag')
    tag.set(W+'val','report_toc')
    old_ids=set()
    for el in root.xpath('.//w:bookmarkStart[starts-with(@w:name,"DJTOC_")]',namespaces=NS):old_ids.add(el.get(W+'id'));el.getparent().remove(el)
    for el in root.xpath('.//w:bookmarkEnd',namespaces=NS):
        if el.get(W+'id') in old_ids:el.getparent().remove(el)
    next_id=max([int(e.get(W+'id','0')) for e in root.xpath('.//w:bookmarkStart',namespaces=NS)]+[0])+1
    content=control.find(W+'sdtContent')
    for child in list(content):content.remove(child)
    heading=E.SubElement(content,W+'p');pp=E.SubElement(heading,W+'pPr');E.SubElement(pp,W+'jc').set(W+'val','center')
    spacing=E.SubElement(pp,W+'spacing');spacing.set(W+'after','260');_run(heading,'目  录',True,32)
    result=[]
    for n,(key,label,level,target) in enumerate(entries_for(root),1):
        bookmark=f'DJTOC_{n:03}';bid=str(next_id+n)
        bs=E.Element(W+'bookmarkStart');bs.set(W+'id',bid);bs.set(W+'name',bookmark)
        target.insert(1 if target.find(W+'pPr') is not None else 0,bs)
        be=E.SubElement(target,W+'bookmarkEnd');be.set(W+'id',bid)
        p=E.SubElement(content,W+'p');pr=E.SubElement(p,W+'pPr')
        E.SubElement(pr,W+'ind').set(W+'left',str(level*280))
        sp=E.SubElement(pr,W+'spacing');sp.set(W+'after','40');sp.set(W+'line','280');sp.set(W+'lineRule','auto')
        tabs=E.SubElement(pr,W+'tabs');tab=E.SubElement(tabs,W+'tab');tab.set(W+'val','right');tab.set(W+'leader','dot');tab.set(W+'pos','9150')
        link=E.SubElement(p,W+'hyperlink');link.set(W+'anchor',bookmark);_run(link,label,level==0)
        E.SubElement(_run(p),W+'tab')
        E.SubElement(_run(p),W+'fldChar').set(W+'fldCharType','begin')
        E.SubElement(_run(p),W+'instrText').text=f' PAGEREF {bookmark} '
        E.SubElement(_run(p),W+'fldChar').set(W+'fldCharType','separate')
        _run(p,str(pages.get(bookmark,'—')))
        E.SubElement(_run(p),W+'fldChar').set(W+'fldCharType','end')
        result.append({'key':key,'label':label,'level':level,'bookmark':bookmark,'page':pages.get(bookmark)})
    return result

def update_cached_pages(root,pages):
    content=find_sdt(root,'report_toc').find(W+'sdtContent')
    for p in content.findall(W+'p'):
        instruction=p.find('.//'+W+'instrText')
        if instruction is None:continue
        name=instruction.text.strip().split()[1]
        if name not in pages:raise ValueError('未找到目录目标页码：'+name)
        result=False
        for r in p.findall(W+'r'):
            fld=r.find(W+'fldChar')
            if fld is not None:
                if fld.get(W+'fldCharType')=='separate':result=True
                elif fld.get(W+'fldCharType')=='end':result=False
            t=r.find(W+'t')
            if result and t is not None:t.text=str(pages[name])

def sync_pages_from_pdf(docx_path,pdf_path):
    """Copy renderer-resolved bookmark destinations into the saved DOCX field cache."""
    from pypdf import PdfReader
    docx_path=Path(docx_path);pdf=PdfReader(pdf_path)
    with zipfile.ZipFile(docx_path) as z:root=E.fromstring(z.read('word/document.xml'))
    toc=find_sdt(root,'report_toc');links=toc.xpath('.//w:hyperlink/@w:anchor',namespaces=NS)
    page_ids={p.indirect_reference.idnum:i+1 for i,p in enumerate(pdf.pages)}
    targets=[]
    for i,page in enumerate(pdf.pages):
        t=norm(page.extract_text() or '')
        if '目录' not in t or '电站基本信息' not in t:continue
        annotations=[]
        for ref in page.get('/Annots',[]):
            a=ref.get_object();dest=a.get('/Dest')
            if not dest:continue
            # LibreOffice emits a second link on the right-aligned page field.
            # Count the left-side label link once for each visible entry.
            if float(a['/Rect'][0])>float(page.mediabox.width)*0.75:continue
            if isinstance(dest,list) and getattr(dest[0],'idnum',None) in page_ids:
                annotations.append((float(a['/Rect'][3]),float(a['/Rect'][0]),page_ids[dest[0].idnum]))
        annotations.sort(key=lambda x:(-x[0],x[1]));targets.extend(x[2] for x in annotations)
    if len(targets)!=len(links):raise ValueError(f'目录链接目标数量不符：{len(targets)} != {len(links)}')
    pages=dict(zip(links,targets));update_cached_pages(root,pages)
    clean_package(docx_path,docx_path,E.tostring(root))
    return pages
