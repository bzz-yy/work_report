"""Embed original photographs into the retained Word photo table, without cropping."""
from copy import deepcopy
import hashlib
from pathlib import Path

from lxml import etree as E
from PIL import Image

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
WP='http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'
A='http://schemas.openxmlformats.org/drawingml/2006/main'
PIC='http://schemas.openxmlformats.org/drawingml/2006/picture'
R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
REL='http://schemas.openxmlformats.org/package/2006/relationships'
CT='http://schemas.openxmlformats.org/package/2006/content-types'
NS={'w':W,'wp':WP}


def node(parent, namespace, local_name, **attrs):
    return E.SubElement(parent,'{'+namespace+'}'+local_name,{key:str(value) for key,value in attrs.items()})


def image_info(path):
    with Image.open(path) as picture:
        picture.verify()
    with Image.open(path) as picture:
        picture.load()
        if picture.format not in {'JPEG','PNG'}:raise ValueError('报告图片只支持已验证的JPEG或PNG')
        width,height=picture.size
        if width<=0 or height<=0:raise ValueError('图片尺寸无效')
        return {'width':width,'height':height,'format':picture.format,
                'extension':'jpg' if picture.format=='JPEG' else 'png',
                'mime':'image/jpeg' if picture.format=='JPEG' else 'image/png'}


def picture_paragraph(rid,identifier,filename,cx,cy,caption):
    paragraph=E.Element('{'+W+'}p')
    props=node(paragraph,W,'pPr');node(props,W,'jc',**{'{'+W+'}val':'center'})
    node(props,W,'spacing',**{'{'+W+'}before':'0','{'+W+'}after':'0','{'+W+'}line':'240','{'+W+'}lineRule':'auto'})
    run=node(paragraph,W,'r');drawing=node(run,W,'drawing')
    inline=node(drawing,WP,'inline',distT=0,distB=0,distL=0,distR=0)
    node(inline,WP,'extent',cx=cx,cy=cy)
    node(inline,WP,'effectExtent',l=0,t=0,r=0,b=0)
    node(inline,WP,'docPr',id=identifier,name=filename,descr=caption)
    locks=node(inline,WP,'cNvGraphicFramePr');node(locks,A,'graphicFrameLocks',noChangeAspect=1)
    graphic=node(inline,A,'graphic');data=node(graphic,A,'graphicData',uri=PIC)
    pic=node(data,PIC,'pic');nonvisual=node(pic,PIC,'nvPicPr')
    node(nonvisual,PIC,'cNvPr',id=0,name=filename);node(nonvisual,PIC,'cNvPicPr')
    fill=node(pic,PIC,'blipFill');node(fill,A,'blip',**{'{'+R+'}embed':rid})
    stretch=node(fill,A,'stretch');node(stretch,A,'fillRect')
    shape=node(pic,PIC,'spPr');transform=node(shape,A,'xfrm')
    node(transform,A,'off',x=0,y=0);node(transform,A,'ext',cx=cx,cy=cy)
    geometry=node(shape,A,'prstGeom',prst='rect');node(geometry,A,'avLst')
    return paragraph


def embed_photos(root,mapping,photos,template_zip,used):
    items=photos.get('items',[]) if photos else []
    if not items:return {},{'embedded':0,'total':0}
    config=mapping['photo_region']
    if config.get('overflow')!='append_rows' or config.get('fit')!='contain':
        raise ValueError('图片区域未启用保留比例和扩行规则')
    table=root.xpath('/w:document/w:body/w:tbl',namespaces=NS)[config['table_index']]
    prototype=deepcopy(table.find('{'+W+'}tr'))
    cells=table.xpath('./w:tr/w:tc',namespaces=NS)
    while len(cells)<len(items):
        table.append(deepcopy(prototype));cells=table.xpath('./w:tr/w:tc',namespaces=NS)
    rels=E.fromstring(template_zip.read('word/_rels/document.xml.rels'))
    content_types=E.fromstring(template_zip.read('[Content_Types].xml'))
    known_extensions={item.get('Extension') for item in content_types}
    ids={item.get('Id') for item in rels}
    next_id=max([int(v) for v in root.xpath('.//wp:docPr/@id',namespaces=NS)]+[0])+1
    patches={};embedded=[]
    photo_marks=[item for item in used if item['field_id']=='F090']
    for index,(item,cell) in enumerate(zip(items,cells)):
        path=Path(item['file']);payload=path.read_bytes();digest=hashlib.sha256(payload).hexdigest()
        if digest!=item['sha256']:raise ValueError('图片内容摘要发生变化')
        info=image_info(path)
        filename='photo_'+digest[:24]+'.'+info['extension'];part='word/media/'+filename
        if part in template_zip.namelist() and template_zip.read(part)!=payload:
            raise ValueError('图片部件命名冲突')
        patches[part]=payload
        rid='rIdPhoto'+str(next_id+index)
        while rid in ids:rid+='x'
        ids.add(rid)
        node(rels,REL,'Relationship',Id=rid,Type=R+'/image',Target='media/'+filename)
        if info['extension'] not in known_extensions:
            node(content_types,CT,'Default',Extension=info['extension'],ContentType=info['mime'])
            known_extensions.add(info['extension'])
        tcwidth=cell.find('{'+W+'}tcPr/{'+W+'}tcW')
        maximum_width=min(int(float(config['max_width_cm'])*360000),
                          (int(tcwidth.get('{'+W+'}w'))-160)*635) if tcwidth is not None else int(float(config['max_width_cm'])*360000)
        maximum_height=int(float(config['max_height_cm'])*360000)
        scale=min(maximum_width/info['width'],maximum_height/info['height'])
        cx,cy=int(info['width']*scale),int(info['height']*scale)
        for paragraph in cell.findall('{'+W+'}p'):cell.remove(paragraph)
        caption=item.get('caption') or '现场运维照片'
        cell.append(picture_paragraph(rid,next_id+index,filename,cx,cy,caption))
        label=E.Element('{'+W+'}p');props=node(label,W,'pPr')
        node(props,W,'jc',**{'{'+W+'}val':'center'})
        node(props,W,'spacing',**{'{'+W+'}before':'0','{'+W+'}after':'0'})
        run=node(label,W,'r');rprops=node(run,W,'rPr')
        node(rprops,W,'rFonts',**{'{'+W+'}eastAsia':'宋体','{'+W+'}ascii':'宋体'})
        node(rprops,W,'sz',**{'{'+W+'}val':'16'})
        node(run,W,'t').text=caption
        cell.append(label)
        mark={'field_id':'F090','status':'real','filled':True}
        if index<len(photo_marks):photo_marks[index].update(mark)
        else:used.append(mark)
        embedded.append({'file':str(path),'sha256':digest,'part':part,'caption':caption,'fit':'contain'})
    patches['word/_rels/document.xml.rels']=E.tostring(rels,xml_declaration=True,encoding='UTF-8',standalone=True)
    patches['[Content_Types].xml']=E.tostring(content_types,xml_declaration=True,encoding='UTF-8',standalone=True)
    return patches,{'embedded':len(embedded),'total':len(items),'slots':len(cells),'images':embedded,'bitmap_bytes_unchanged':True}
