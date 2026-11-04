"""영상 위의 짧은 제목과 엔딩 문구를 만든다. 사진이나 정지 장면을 삽입하지 않는다."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ai_service.services.video.errors import VideoError


def render_titles(folder, storyboard):
    captions=[('opening-title.png',storyboard.title),('closing-title.png',storyboard.closing_line)]
    if not any(text for _,text in captions):return []
    paths=[Path('/System/Library/Fonts/AppleSDGothicNeo.ttc'),
           Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
           Path('/usr/share/fonts/truetype/noto/NotoSansKR-Regular.ttf')]
    if all(ord(char)<128 for _,text in captions if text for char in text):
        paths.append(Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'))
    font_path=next((p for p in paths if p.is_file()),None)
    if font_path is None:raise VideoError('memory_title_font_missing')
    results=[]
    for name,text in captions:
        if not text:continue
        image=Image.new('RGBA',(1280,720),(0,0,0,0));draw=ImageDraw.Draw(image)
        # A small lower-third treatment leaves the generated scene visible.
        for y in range(450,720):
            draw.line((0,y,1279,y),fill=(0,0,0,round(105*(y-450)/270)))
        font=ImageFont.truetype(str(font_path),40 if name.startswith('opening') else 30)
        while draw.textbbox((0,0),text,font=font)[2]>1080:
            font=ImageFont.truetype(str(font_path),font.size-1)
        draw.line((84,548,122,548),fill=(255,240,212,230),width=2)
        draw.text((84,570),text,font=font,fill=(255,249,239,255),stroke_width=1,stroke_fill=(0,0,0,70))
        path=folder/name;image.save(path);results.append((name,path))
    return results
