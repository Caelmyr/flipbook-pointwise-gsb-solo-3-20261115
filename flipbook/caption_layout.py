"""整句排版与按文字簇显露。

旧实现把每次增长的前缀重新交给光栅器排版，导致连写（阿拉伯 positional
forms、拉丁连字）随前缀变化而变形，组合标记被拆开，双向文本的位置也会跳动。

这里先对**完整句子**做一次排版：

1. 用 UAX #29 字形簇（grapheme cluster）切分逻辑字符串，显露以文字簇为单位；
2. 用 python-bidi 计算双向嵌入级别并得到视觉顺序与镜像字符；
3. 按方向级别拆成排版 run，交给 HarfBuzz 整句塑形，连字与组合标记永不拆开；
4. 用 FreeType 光栅化整句得到一张主覆盖蒙版，记录每个字形覆盖的像素区域；
5. 每一帧只按阅读进度保留已显露文字簇对应字形的区域，其余像素清零，
   因此已经出现的像素与最终整句逐像素一致，位置和形态都不会再变化。

模块只依赖 requirements 中固定的 uharfbuzz、freetype-py、regex、python-bidi。
"""
import io
import unicodedata

import freetype
import numpy as np
import regex
import uharfbuzz as hb
from bidi import algorithm as bidi_alg

from . import validate as v

PIXEL_FIXED = 64


class Typeface:
    """一次加载、可重复使用的整句排版引擎（HarfBuzz + FreeType 共享字体数据）。"""

    def __init__(self, path, pixel_size, ttc_index=0):
        with open(path, 'rb') as stream:
            self.data = stream.read()
        blob = hb.Blob(self.data)
        self.hb_face = hb.Face(blob, ttc_index)
        self.hb_font = hb.Font(self.hb_face)
        self.hb_font.scale = (pixel_size * PIXEL_FIXED, pixel_size * PIXEL_FIXED)
        # FreeType 直接读字节流，避免再复制一份字体文件
        self.ft_face = freetype.Face(io.BytesIO(self.data), ttc_index)
        self.ft_face.set_pixel_sizes(0, pixel_size)
        self.ascender = self.ft_face.size.ascender
        self.line_step = self.ft_face.size.height
        self._bitmap_cache = {}

    def covers(self, text):
        """返回字体里缺少字形的非空白、非格式字符集合。"""
        missing = set()
        for ch in text:
            if ch.isspace() or unicodedata.category(ch) == 'Cf':
                continue
            if not self.ft_face.get_char_index(ord(ch)):
                missing.add(ch)
        return missing

    # -- 双向与塑形 -------------------------------------------------------

    def _visual_chars(self, line):
        """返回视觉顺序的字符记录（含逻辑下标、级别、镜像后的码点）。"""
        storage = bidi_alg.get_empty_storage()
        base_level = bidi_alg.get_base_level(line)
        storage['base_level'] = base_level
        storage['base_dir'] = ('L', 'R')[base_level]
        bidi_alg.get_embedding_levels(line, storage, False, False)
        bidi_alg.explicit_embed_and_overrides(storage, False)
        bidi_alg.resolve_weak_types(storage, False)
        bidi_alg.resolve_neutral_types(storage, False)
        bidi_alg.resolve_implicit_levels(storage, False)
        for index, record in enumerate(storage['chars']):
            record['logical'] = index
        bidi_alg.reorder_resolved_levels(storage, False)
        # Unicode Bidi 规则 L4：奇数级别上的镜像字符需要换成镜像码点，
        # HarfBuzz 本身不做双向镜像，必须在上层完成。
        for record in storage['chars']:
            if record['level'] % 2 and unicodedata.mirrored(record['ch']):
                mirrored = unicodedata.mirrored(record['ch'])
                if isinstance(mirrored, str) and len(mirrored) == 1:
                    record['ch'] = mirrored
        return storage['chars'], base_level

    def _shape_line(self, visual_chars):
        """按方向级别奇偶拆 run 塑形，返回字形记录与整行宽度（26.6 定点数）。

        双向排序得到的 run 是视觉顺序，但 HarfBuzz 必须接收**逻辑顺序**的码点
        才能正确做阿拉伯连写等上下文替换，因此每个 run 先还原成逻辑顺序再塑形。
        HarfBuzz 输出已是视觉顺序、x 从 run 原点 0 沿正向递增；这里把每个 run
        平移到它在视觉行中的笔位，RTL run 的笔位从行右端向左安排。
        """
        # 1. 视觉顺序切成同方向 run
        runs = []
        index = 0
        total = len(visual_chars)
        while index < total:
            level = visual_chars[index]['level']
            end = index + 1
            while end < total and visual_chars[end]['level'] % 2 == level % 2:
                end += 1
            runs.append((level, visual_chars[index:end]))
            index = end
        # 2. 先塑形每个 run（逻辑顺序输入），拿到 run 宽度
        shaped = []
        for level, run in runs:
            logical_run = sorted(run, key=lambda record: record['logical'])
            buffer = hb.Buffer()
            buffer.add_codepoints([ord(record['ch']) for record in logical_run])
            buffer.direction = 'rtl' if level % 2 else 'ltr'
            # 让 HarfBuzz 自行推断脚本/语言，阿拉伯 GPOS 草书连接定位等特性
            # 只有在脚本属性正确时才会生效，否则字距会退化成孤立字形宽度。
            buffer.guess_segment_properties()
            hb.shape(self.hb_font, buffer)
            run_width = sum(pos.x_advance for pos in buffer.glyph_positions)
            shaped.append((level, logical_run, buffer, run_width))
        line_width = sum(item[3] for item in shaped)
        # 3. run 已按视觉顺序排列，用一支笔顺序摆放即可：LTR run 从笔位向右，
        # RTL run 的 HarfBuzz 输出同样沿 x 正向增长（视觉首字形在 run 左端，
        # 对应视觉上的 run 右缘），因此不需要反向笔位。
        glyphs = []
        pen = 0
        for level, logical_run, buffer, run_width in shaped:
            logical_indices = [record['logical'] for record in logical_run]
            cursor = 0
            for info, pos in zip(buffer.glyph_infos, buffer.glyph_positions):
                glyphs.append({
                    'gid': info.codepoint,
                    'logical': logical_indices[info.cluster],
                    'x': pen + cursor + pos.x_offset,
                    'y_offset': pos.y_offset,
                })
                cursor += pos.x_advance
            pen += run_width
        return glyphs, line_width

    def _line_width(self, line):
        if not line:
            return 0
        visual_chars, _ = self._visual_chars(line)
        return self._shape_line(visual_chars)[1]

    # -- 换行 -------------------------------------------------------------

    def _wrap(self, text, max_width_px):
        """在 UAX #29 文字簇边界贪心换行，优先在空格处断行。"""
        max_fixed = max_width_px * PIXEL_FIXED
        clusters = [(match.group(), match.start()) for match in regex.finditer(r'\X', text)]
        if not clusters:
            return []
        lines = []
        current = []
        current_start = clusters[0][1]
        for cluster, global_start in clusters:
            trial = ''.join(piece for piece, _ in current) + cluster
            if current and self._line_width(trial) > max_fixed:
                joined = ''.join(piece for piece, _ in current)
                space = joined.rfind(' ')
                if space > 0:
                    head, tail = joined[:space], joined[space + 1:]
                    lines.append((head, current_start))
                    current_start += len(head) + 1
                    current = [(tail, None)] if tail else []
                    current.append((cluster, global_start))
                else:
                    lines.append((joined, current_start))
                    current = [(cluster, global_start)]
                    current_start = global_start
            else:
                current.append((cluster, global_start))
        if current:
            lines.append((''.join(piece for piece, _ in current), current_start))
        return lines

    # -- 光栅化 -----------------------------------------------------------

    def _rasterize(self, gid):
        cached = self._bitmap_cache.get(gid)
        if cached is not None:
            return cached
        self.ft_face.load_glyph(gid, freetype.FT_LOAD_RENDER)
        bitmap = self.ft_face.glyph.bitmap
        try:
            raw = np.array(bitmap.buffer, dtype=np.uint8)
        except ValueError:
            raw = np.empty(0, dtype=np.uint8)
        if raw.size == 0 or bitmap.rows == 0 or bitmap.width == 0:
            array = np.zeros((0, 0), dtype=np.uint8)
        elif bitmap.pixel_mode == freetype.FT_PIXEL_MODE_GRAY:
            array = raw[:bitmap.rows * bitmap.pitch].reshape(
                bitmap.rows, bitmap.pitch)[:, :bitmap.width].copy()
        elif bitmap.pixel_mode == freetype.FT_PIXEL_MODE_MONO:
            packed = raw[:bitmap.rows * bitmap.pitch].reshape(bitmap.rows, bitmap.pitch)
            array = (np.unpackbits(packed, axis=1)[:, :bitmap.width] * 255).astype(np.uint8)
        else:
            raise v.Invalid('字幕字体包含不支持的位图字形格式')
        result = (array, self.ft_face.glyph.bitmap_left, self.ft_face.glyph.bitmap_top)
        self._bitmap_cache[gid] = result
        return result

    # -- 对外接口 ---------------------------------------------------------

    def layout(self, text, max_width_px):
        """整句一次排版。

        返回 (records, line_count)：records 每项为
        (gid, 全局逻辑下标, 像素x, 基线像素y)，坐标已包含换行与方向对齐。
        """
        lines = self._wrap(text, max_width_px)
        records = []
        for line_index, (line, global_start) in enumerate(lines):
            if not line:
                continue
            visual_chars, base_level = self._visual_chars(line)
            glyphs, line_width = self._shape_line(visual_chars)
            # 塑形坐标里 LTR 行占据 [0, 行宽]、贴左；RTL 行已经把视觉右端放在
            # 行宽处，整行同样落在 [0, 行宽]，贴右时平移 (换行框宽 - 行宽)。
            # 行宽整句一次固定，显露过程不再回流。
            origin_x = (max_width_px * PIXEL_FIXED - line_width) if base_level % 2 else 0
            baseline_y = self.ascender + line_index * self.line_step
            for glyph in glyphs:
                records.append((
                    glyph['gid'],
                    global_start + glyph['logical'],
                    round((origin_x + glyph['x']) / PIXEL_FIXED),
                    round((baseline_y - glyph['y_offset']) / PIXEL_FIXED),
                ))
        return records, len(lines)

    def master_mask(self, text, max_width_px, width, height):
        """把整句光栅化到覆盖蒙版，并记录每个像素归属的逻辑字符下标。

        重叠像素保留最先写入字形的覆盖值（与 RAQM/PIL 的覆盖语义一致）。
        蒙版与归属表只算一次，后续各帧只按文字簇清零，保证逐帧像素稳定。
        """
        records, line_count = self.layout(text, max_width_px)
        coverage = np.zeros((height, width), dtype=np.uint8)
        # -1 表示该像素尚无字形归属；多行之间像素本就不重叠
        ownership = np.full((height, width), -1, dtype=np.int32)
        for gid, logical, pen_x, baseline_y in records:
            array, left, top = self._rasterize(gid)
            if array.size == 0:
                continue
            origin_x = pen_x + left
            origin_y = baseline_y - top
            glyph_h, glyph_w = array.shape
            x0, y0 = max(0, origin_x), max(0, origin_y)
            x1, y1 = min(width, origin_x + glyph_w), min(height, origin_y + glyph_h)
            if x1 <= x0 or y1 <= y0:
                continue
            sub = array[y0 - origin_y:y1 - origin_y, x0 - origin_x:x1 - origin_x]
            slot = coverage[y0:y1, x0:x1]
            owner = ownership[y0:y1, x0:x1]
            unowned = slot == 0
            slot[unowned] = sub[unowned]
            owner[unowned] = logical
        return coverage, ownership, line_count

    def reveal_frames(self, text, max_width_px, width, height, color):
        """按文字簇生成逐帧 RGBA 图层，已显露像素与整句最终形态完全一致。"""
        spans = [match.span() for match in regex.finditer(r'\X', text)]
        coverage, ownership, _ = self.master_mask(text, max_width_px, width, height)
        color_rgb = np.array(color[:3], dtype=np.uint8)
        alpha_scale = color[3]
        frames = []
        for step in range(len(spans) + 1):
            limit = spans[step][0] if step < len(spans) else len(text) + 1
            # 归属到未显露逻辑字符的像素整体清零；同一张蒙版裁剪而来，
            # 保留下来的像素与整句最终结果逐像素相同
            mask = coverage.copy()
            mask[ownership >= limit] = 0
            image = np.zeros((height, width, 4), dtype=np.uint8)
            image[:, :, 0] = color_rgb[0]
            image[:, :, 1] = color_rgb[1]
            image[:, :, 2] = color_rgb[2]
            image[:, :, 3] = (mask.astype(np.uint16) * alpha_scale // 255).astype(np.uint8)
            frames.append(image)
        return frames, spans
