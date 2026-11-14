from pathlib import Path

from PIL import features

from . import validate as v
from .caption_layout import Typeface

FONTS = {'cjk': '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
         'sans': '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'}


def caption_font(body, message, size):
    """校验字体与运行库，返回 (字体路径, 整句排版引擎)。"""
    key = v.choice(body.get('typeface', 'cjk'), '字幕字体', FONTS)
    path = FONTS[key]
    if not Path(path).is_file() or not features.check('raqm'):
        raise v.Invalid('字幕字体或完整排版运行库不可用，请按项目依赖启动')
    typeface = Typeface(path, size)
    missing = sorted(typeface.covers(message))
    if missing:
        raise v.Invalid('所选字体缺少字形：' + ''.join(missing))
    return path, typeface
