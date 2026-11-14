import math
import re
from datetime import datetime, timezone


class Invalid(ValueError):
    pass


class Conflict(ValueError):
    pass


def object_value(value, name='参数'):
    if not isinstance(value, dict):
        raise Invalid(name + '必须是对象')
    return value


def array(value, name='列表', maximum=1000):
    if not isinstance(value, list):
        raise Invalid(name + '必须是列表')
    if len(value) > maximum:
        raise Invalid(f'{name}最多{maximum}项')
    return value


def real(value, name, low=None, high=None):
    if isinstance(value, bool):
        raise Invalid(name + '必须是数值')
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise Invalid(name + '必须是数值')
    if not math.isfinite(result):
        raise Invalid(name + '必须是有限数值')
    if low is not None and result < low:
        raise Invalid(f'{name}不能小于{low}')
    if high is not None and result > high:
        raise Invalid(f'{name}不能大于{high}')
    return result


def integer(value, name, low=None, high=None):
    result = real(value, name, low, high)
    if result != int(result):
        raise Invalid(name + '必须是整数')
    return int(result)


def boolean(value, name):
    if not isinstance(value, bool):
        raise Invalid(name + '必须为布尔值')
    return value


def text(value, name, maximum=200, empty=True):
    if not isinstance(value, str):
        raise Invalid(name + '必须为文本')
    value = value.strip()
    if not empty and not value:
        raise Invalid(name + '不能为空')
    if len(value) > maximum or '\x00' in value:
        raise Invalid(name + '长度超限或包含无效字符')
    return value


def choice(value, name, options):
    if not isinstance(value, (str, int)) or value not in options:
        raise Invalid(name + '取值不支持')
    return value


def key(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{32}', value):
        raise Invalid('编号无效')
    return value


def digest(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise Invalid('图像引用无效')
    return value


def rgba(value):
    if isinstance(value, str):
        if not re.fullmatch('#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?', value):
            raise Invalid('颜色应为#RRGGBB或#RRGGBBAA')
        parts = [int(value[i:i + 2], 16) for i in range(1, len(value), 2)]
        if len(parts) == 3:
            parts.append(255)
        return tuple(parts)
    values = array(value, '颜色', 4)
    if len(values) not in (3, 4):
        raise Invalid('颜色应有3或4个通道')
    result = [integer(item, '颜色通道', 0, 255) for item in values]
    if len(result) == 3:
        result.append(255)
    return tuple(result)


def hex_color(value):
    return '#%02x%02x%02x%02x' % rgba(value)


def point(value, width, height, clamp=False):
    values = array(value, '坐标', 2)
    if len(values) != 2:
        raise Invalid('坐标需要x和y')
    x = real(values[0], '坐标X')
    y = real(values[1], '坐标Y')
    if clamp:
        return [max(0, min(width - 1, x)), max(0, min(height - 1, y))]
    if not 0 <= x < width or not 0 <= y < height:
        raise Invalid('坐标超出画布')
    return [x, y]


def points(value, width, height, minimum=1, maximum=3000):
    result = [point(p, width, height) for p in array(value, '路径点', maximum)]
    if len(result) < minimum:
        raise Invalid(f'至少需要{minimum}个路径点')
    return result


def rect(body, width, height):
    body = object_value(body, '矩形')
    x = integer(body.get('x', 0), '起点X', 0, width - 1)
    y = integer(body.get('y', 0), '起点Y', 0, height - 1)
    w = integer(body.get('width', width - x), '区域宽度', 1, width - x)
    h = integer(body.get('height', height - y), '区域高度', 1, height - y)
    return {'x': x, 'y': y, 'width': w, 'height': h}


def revision(body):
    return integer(body.get('revision'), '修订号', 0)


def dimensions(body):
    width = integer(body.get('width', 64), '画布宽度', 8, 2048)
    height = integer(body.get('height', 64), '画布高度', 8, 2048)
    return width, height


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def duration(value):
    return integer(value, '帧时长毫秒', 20, 10000)


def blend(value):
    return choice(value, '混合模式', ['normal', 'multiply', 'screen', 'add', 'difference', 'overlay', 'darken', 'lighten'])


def transform(body):
    body = object_value(body, '变换')
    return {
        'x': integer(body.get('x', 0), '偏移X', -4096, 4096),
        'y': integer(body.get('y', 0), '偏移Y', -4096, 4096),
        'scale_x': real(body.get('scale_x', 1), '横向比例', 0.01, 8),
        'scale_y': real(body.get('scale_y', 1), '纵向比例', 0.01, 8),
        'rotation': real(body.get('rotation', 0), '旋转角度', -360, 360),
        'flip_x': boolean(body.get('flip_x', False), '水平翻转'),
        'flip_y': boolean(body.get('flip_y', False), '垂直翻转'),
    }


def layer_properties(body):
    result = {}
    if 'name' in body:
        result['name'] = text(body['name'], '图层名称', 100, False)
    if 'visible' in body:
        result['visible'] = boolean(body['visible'], '显示图层')
    if 'locked' in body:
        result['locked'] = boolean(body['locked'], '锁定图层')
    if 'opacity' in body:
        result['opacity'] = real(body['opacity'], '不透明度', 0, 1)
    if 'blend' in body:
        result['blend'] = blend(body['blend'])
    if 'transform' in body:
        result['transform'] = transform(body['transform'])
    return result


def draw_command(body, width, height):
    body = object_value(body)
    mode = choice(body.get('tool', 'pencil'), '绘制工具', ['pencil', 'erase', 'line', 'rectangle', 'ellipse', 'polygon', 'fill', 'gradient', 'text'])
    needed = 3 if mode == 'polygon' else 2 if mode in ('line', 'rectangle', 'ellipse', 'gradient') else 1
    result = {
        'tool': mode,
        'color': rgba(body.get('color', '#65e2b9')),
        'secondary': rgba(body.get('secondary', '#6078ff')),
        'size': integer(body.get('size', 1), '笔刷尺寸', 1, 128),
        'points': points(body.get('points', []), width, height, needed),
        'filled': boolean(body.get('filled', True), '填充形状'),
        'tolerance': integer(body.get('tolerance', 0), '填充容差', 0, 255),
        'contiguous': boolean(body.get('contiguous', True), '连续填充'),
        'text': text(body.get('text', ''), '绘制文字', 500),
        'font_size': integer(body.get('font_size', 12), '文字大小', 6, 256),
        'mirror_x': boolean(body.get('mirror_x', False), '水平镜像绘制'),
        'mirror_y': boolean(body.get('mirror_y', False), '垂直镜像绘制'),
    }
    if mode == 'text' and not result['text']:
        raise Invalid('请输入绘制文字')
    return result


def easing(value):
    return choice(value, '缓动方式', ['linear', 'ease_in', 'ease_out', 'ease_in_out', 'smoothstep', 'bounce', 'elastic', 'steps'])


def frame_ids(value, project, minimum=1):
    ids = [key(item) for item in array(value, '帧编号', 300)]
    if len(ids) < minimum:
        raise Invalid('请选择动画帧')
    if len(ids) != len(set(ids)):
        raise Invalid('帧编号不能重复')
    known = {frame['id'] for frame in project['frames']}
    if any(item not in known for item in ids):
        raise Invalid('包含不存在的帧')
    return ids


def pixel_budget(width, height, count, maximum=40000000):
    if width * height * count > maximum:
        raise Invalid('操作超过4000万画布像素预算，请减少帧数或画布尺寸')


def palette(value):
    colors = [hex_color(item) for item in array(value, '调色板', 256)]
    if not colors:
        raise Invalid('调色板至少需要一种颜色')
    return list(dict.fromkeys(colors))


def frame_duration(value, project):
    if project.get('animation_timing') == 'exact_composition':
        return integer(value, '精确镜头曝光毫秒', 1, 10000)
    return duration(value)
