from pathlib import Path
import unicodedata
import freetype
from PIL import ImageFont, features
from . import validate as v

FONTS = {'cjk': '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
         'sans': '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'}


def caption_font(body, message, size):
    key = v.choice(body.get('typeface', 'cjk'), '字幕字体', FONTS)
    path = FONTS[key]
    if not Path(path).is_file() or not features.check('raqm'):
        raise v.Invalid('字幕字体或完整排版运行库不可用，请按项目依赖启动')
    face = freetype.Face(path)
    missing = sorted({ch for ch in message if not ch.isspace() and unicodedata.category(ch) != 'Cf' and not face.get_char_index(ord(ch))})
    if missing:
        raise v.Invalid('所选字体缺少字形：' + ''.join(missing))
    return path, ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.RAQM)
