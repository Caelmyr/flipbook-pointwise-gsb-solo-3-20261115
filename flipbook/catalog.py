
def number(name, label, default, low=None, high=None):
    return {'name': name, 'label': label, 'type': 'number', 'default': default, 'min': low, 'max': high}


def select(name, label, default, options):
    return {'name': name, 'label': label, 'type': 'select', 'default': default, 'options': [{'value': key, 'label': title} for key, title in options]}


def text(name, label, default='', kind='text'):
    return {'name': name, 'label': label, 'type': kind, 'default': default}


def boolean(name, label, default=False):
    return {'name': name, 'label': label, 'type': 'checkbox', 'default': default}


def array(name, label, default):
    return text(name, label, default, 'array')


def tool(key, title, group, path, fields=None, mutation=True, note='', output='project', method='POST'):
    return {
        'key': key,
        'title': title,
        'group': group,
        'path': path,
        'fields': fields or [],
        'mutation': mutation,
        'note': note,
        'output': output,
        'method': method,
    }


EFFECTS = [
    ('invert', '反相'), ('grayscale', '灰阶'), ('brightness', '亮度'),
    ('contrast', '对比度'), ('saturation', '饱和度'), ('gamma', 'Gamma'),
    ('hue', '色相旋转'), ('posterize', '减色阶'), ('pixelate', '像素化'),
    ('blur', '模糊'), ('sharpen', '锐化'), ('outline', '描边'),
    ('shadow', '投影'), ('alpha_threshold', '透明度二值化'),
    ('alpha_bleed', '透明区颜色外溢'), ('offset', '环绕偏移'),
    ('replace', '替换颜色'), ('flip_x', '左右翻转'), ('flip_y', '上下翻转'),
]
ANCHORS = [
    ('top_left', '左上'), ('top', '上'), ('top_right', '右上'),
    ('left', '左'), ('center', '中心'), ('right', '右'),
    ('bottom_left', '左下'), ('bottom', '下'), ('bottom_right', '右下'),
]
EASING = [
    ('linear', '线性'), ('ease_in', '加速'), ('ease_out', '减速'),
    ('ease_in_out', '加速减速'), ('smoothstep', '平滑'),
    ('bounce', '弹跳'), ('elastic', '弹性'), ('steps', '阶梯'),
]


def catalog():
    tools = []
    tools.append(tool(
        'retime', '动画重定时', '时间轴', 'retime', [
            select('mode', '时长模式', 'fps', [('fps', '统一帧率'), ('duration', '统一帧时长'), ('scale', '时长倍数'), ('total', '指定总时长')]),
            number('fps', '帧率', 12, .1, 50),
            number('duration', '每帧毫秒', 100, 20, 10000),
            number('factor', '时长倍数', 1, .05, 20),
            number('total', '目标总时长毫秒', 1000, 20),
        ], note='作用于勾选帧；未勾选时作用于全部帧。总时长按原时长比例分配。',
    ))
    tools.append(tool('reverse', '反转帧序', '时间轴', 'reverse', note='仅反转勾选帧所在的位置，其他帧不移动。'))
    tools.append(tool('pingpong', '生成往返循环', '时间轴', 'pingpong', note='至少三帧，倒序复制中间帧，端点不重复。'))
    tools.append(tool('deduplicate', '合并重复画面', '时间轴', 'deduplicate', [boolean('adjacent_only', '仅合并相邻重复', True)], note='画面、碰撞框与枢轴一致的重复帧合并并累加时长；各动作片段保留自己的曝光时序与往返端点，地图瓦片槽位与翻转含义不变。整理前会说明可合并与保留原因，整理后可比较、可撤销。'))
    tools.append(tool('flatten', '合并可见图层', '图层', 'flatten', note='合成为一个普通混合图层，有锁定图层时拒绝。'))
    tools.append(tool(
        'effect', '像素效果', '像素', 'effect', [
            select('operation', '效果', 'outline', EFFECTS),
            number('amount', '强度 / Gamma', 1, .01, 10),
            number('radius', '描边/滤波半径', 1, 1, 12),
            text('color', '目标/描边颜色', '#152437ff'),
            text('source', '待替换颜色', '#ffffffff'),
            number('tolerance', '替换颜色容差', 0, 0, 255),
            number('threshold', '透明度阈值', 128, 0, 255),
            number('levels', '色阶数量', 4, 2, 32),
            number('block', '像素块尺寸', 2, 1, 64),
            number('degrees', '色相角度', 30, -360, 360),
            number('dx', '水平偏移', 2, -2048, 2048),
            number('dy', '垂直偏移', 2, -2048, 2048),
            boolean('inside', '内描边'),
        ], note='单帧处理当前图层；多帧按唯一同名图层匹配，遇到锁定图层则全部不提交。',
    ))
    tools.append(tool(
        'pipeline', '批量效果链', '像素', 'pipeline', [
            array('steps', '处理步骤JSON', [{'operation': 'posterize', 'levels': 4}, {'operation': 'outline', 'radius': 1, 'color': '#152437ff'}]),
        ], note='每帧所有图层按顺序处理；整个项目一次提交，可撤销。',
    ))
    tools.append(tool(
        'motion-trail', '生成运动残影', '动画', 'motion-trail', [
            number('depth', '前方残影帧数', 3, 1, 12),
            number('opacity', '首个残影透明度', .35, .01, .9),
            number('dx', '每级残影水平偏移', 0, -256, 256),
            number('dy', '每级残影垂直偏移', 0, -256, 256),
        ], note='使用操作开始时的各帧快照，生成独立底部残影层。',
    ))
    tools.append(tool(
        'temporal-average', '帧间透明平均', '动画', 'temporal-average', [number('radius', '前后窗口半径', 1, 1, 5)],
        note='使用预乘Alpha平均，结果每帧合并为一个图层；可撤销。',
    ))
    tools.append(tool(
        'tween-loop', '程序化循环', '动画', 'tween/loop', [
            select('kind', '循环动作', 'bob', [('bob', '上下浮动'), ('pulse', '缩放脉动'), ('spin', '旋转'), ('shake', '抖动'), ('fade', '明灭')]),
            number('count', '新增帧数', 12, 4, 60),
            number('amplitude', '位置振幅px / 缩放百分比', 6, 0, 100),
            number('duration', '帧时长毫秒', 80, 20, 10000),
        ], note='在当前帧之后新增独立帧，保留可编辑图层变换。',
    ))
    tools.append(tool(
        'tween-transform', '关键帧变换补间', '动画', 'tween/transform', [
            array('target', '目标变换JSON', {'x': 20, 'y': 0, 'scale_x': 1, 'scale_y': 1, 'rotation': 0, 'flip_x': False, 'flip_y': False}),
            select('easing', '缓动', 'smoothstep', EASING),
            number('count', '新增补间帧数', 8, 1, 60),
            number('duration', '帧时长毫秒', 80, 20, 10000),
            number('opacity', '末端透明度', 1, 0, 1),
        ], note='当前帧图层到目标变换；旋转走最短角路径，透明度同时插值。',
    ))
    tools.append(tool(
        'crossfade', '两帧透明过渡', '动画', 'tween/crossfade', [
            {'name': 'first', 'label': '起始帧', 'type': 'frame', 'default': 'current'},
            {'name': 'second', 'label': '结束帧', 'type': 'frame', 'default': 'next'},
            number('count', '插入过渡帧数量', 6, 1, 60),
            number('duration', '过渡帧毫秒', 80, 20, 10000),
            select('easing', '缓动', 'linear', EASING),
        ], note='按预乘Alpha交叉插值，避免透明边缘出现错误色边，合成的新帧可独立编辑。',
    ))
    tools.append(tool(
        'tween-path', '贝塞尔路径动画', '动画', 'tween/path', [
            array('points', '路径控制点 [[x,y],…]', [[12, 32], [32, 12], [52, 32]]),
            select('easing', '缓动', 'linear', EASING),
            number('count', '新增路径帧', 12, 2, 60),
            number('duration', '帧时长毫秒', 80, 20, 10000),
        ], note='支持2至8个控制点，偏移以第一个控制点为相对原点。',
    ))
    tools.append(tool(
        'typewriter', '打字字幕动画', '动画', 'typewriter', [
            text('text', '字幕内容', 'HELLO'),
            select('typeface', '字幕字体', 'cjk', [('cjk', 'Noto 中英文字体'), ('sans', 'DejaVu 连写与双向文字')]),
            number('font_size', '字号', 12, 6, 128),
            text('color', '文字颜色', '#ffffffff'),
            number('x', '文字X', 4, -100, 2048),
            number('y', '文字Y', 4, -100, 2048),
            number('duration', '显露一步毫秒', 100, 20, 10000),
        ], note='字幕使用所选字体完成排版并逐步显露；希望已显露文字保持整句字形和位置。组合标记及连字不能拆开。字体不支持的内容会先提示，源帧与背景图层保留。',
    ))
    tools.append(tool(
        'burst', '放射爆发特效', '动画', 'burst', [
            array('center', '爆发中心 [x,y]', [32, 32]),
            number('rays', '射线数量', 8, 3, 32),
            number('radius', '最大扩散半径', 24, 1, 1024),
            number('count', '新增特效帧数', 12, 4, 60),
            number('duration', '帧时长毫秒', 60, 20, 10000),
            text('color', '射线颜色', '#f8cc65ff'),
        ], note='独立特效层，射线随时间扩散与淡出。',
    ))
    tools.append(tool(
        'resize', '画布与图像尺寸', '画布', 'resize', [
            number('width', '目标宽度', 96, 8, 2048),
            number('height', '目标高度', 96, 8, 2048),
            select('mode', '调整方式', 'canvas', [('canvas', '扩展/裁切画布'), ('scale', '缩放像素')]),
            select('anchor', '画布锚点', 'center', ANCHORS),
            select('interpolation', '缩放插值', 'nearest', [('nearest', '最近邻（像素画）'), ('lanczos', 'Lanczos')]),
        ], note='自动烘焙图层变换并更新枢轴与碰撞框，移除旧地图。',
    ))
    tools.append(tool('trim', '统一透明裁边', '画布', 'trim', [number('padding', '内容外留白', 2, 0, 64)], note='按全部帧的可见内容并集裁切，保留动画位置关系。'))
    tools.append(tool('crop', '统一矩形裁切', '画布', 'crop', [number('x', '左上X', 8, 0), number('y', '左上Y', 8, 0), number('width', '裁切宽度', 48, 8), number('height', '裁切高度', 48, 8)], note='裁切所有图层与动画帧，枢轴和碰撞框同步调整。'))
    tools.append(tool('align', '内容位置对齐', '画布', 'align', [select('anchor', '对齐位置', 'center', ANCHORS)], note='以每帧可见内容包围框为依据，统一移动该帧全部图层。'))
    tools.append(tool('rotate', '旋转整个动画', '画布', 'rotate', [number('turns', '顺时针90度次数', 1, 1, 3)], note='整体旋转，更新画布与枢轴；碰撞框及地图清空后需重新建立。'))
    tools.append(tool(
        'ninepatch', '九宫格保边缩放', '画布', 'ninepatch', [
            number('left', '左边框', 8, 0), number('right', '右边框', 8, 0),
            number('top', '上边框', 8, 0), number('bottom', '下边框', 8, 0),
            number('width', '新宽度', 128, 8, 2048), number('height', '新高度', 96, 8, 2048),
            boolean('tiled', '中心与边带使用平铺'),
        ], mutation=False, output='new_project', note='保留当前帧的四角，生成独立项目，不改变源动画。',
    ))
    tools.append(tool('seamless', '瓦片边缘融合', '瓦片', 'seamless', [number('overlap', '融合带像素', 4, 1, 64), select('axes', '方向', 'both', [('both', '两个方向'), ('horizontal', '左右边'), ('vertical', '上下边')])], note='在选中帧的像素层上对称混合边缘，不保证内部纹理结构自然。'))
    tools.append(tool(
        'matte', '色键与透明遮罩', '像素', 'matte', [
            select('operation', '透明度处理', 'chroma', [('chroma', '去背景色'), ('channel', '从颜色通道建立Alpha'), ('frame', '从另一帧建立遮罩'), ('expand', '扩展Alpha'), ('contract', '收缩Alpha'), ('feather', '羽化'), ('threshold', 'Alpha二值化'), ('invert', '反转Alpha')]),
            {'name': 'mask_frame', 'label': '来源遮罩帧', 'type': 'frame', 'default': 'next'},
            text('color', '背景色', '#00ff00ff'),
            number('tolerance', '背景色容差', 20, 0, 255),
            number('softness', '过渡宽度', 10, 0, 100),
            number('despill', '去溢色', 0, 0, 1),
            select('channel', '来源通道', 'luminance', [('luminance', '亮度'), ('alpha', '另一帧Alpha'), ('red', '红'), ('green', '绿'), ('blue', '蓝'), ('maximum', '最大通道')]),
            number('radius', '透明度细修半径', 1, 1, 12),
            number('threshold', 'Alpha阈值', 128, 0, 255),
            boolean('invert', '反转来源通道'),
        ], note='处理当前帧当前图层；变换后的图层请先烘焙。',
    ))
    tools.append(tool('collision-boxes', '生成Alpha碰撞框', '精灵', 'collision-boxes', [select('mode', '框选方式', 'union', [('union', '整个可见内容'), ('components', '各连通组件')]), number('threshold', 'Alpha阈值', 128, 1, 255), number('minimum', '最小组件像素', 4, 1), number('padding', '框外留白', 0, 0, 64)], note='覆盖选中帧的碰撞框；最多30框，组件分析限100万像素。'))
    tools.append(tool('palette-inventory', '可见颜色清单', '调色板', 'palette/inventory', mutation=False, output='json', note='统计选中帧合成结果的颜色与用量。'))
    tools.append(tool('analysis', '动画质量与重复帧', '诊断', 'analysis', mutation=False, output='json', method='GET', note='透明覆盖、颜色、重复画面、质心运动及导出限制提示。'))
    tools.append(tool('integrity', '素材完整性检查', '诊断', 'integrity', mutation=False, output='json', method='GET', note='检查当前项目引用的不可变像素文件存在且尺寸正确。'))
    tools.append(tool('loop-seam', '首尾帧循环差异', '诊断', 'loop-seam', mutation=False, output='json', method='GET', note='首尾帧差异不直接等于循环错误，需结合运动方向判断。'))
    tools.append(tool('contact-sheet', '动画帧联系表', '诊断', 'contact-sheet', mutation=False, output='image', method='GET', note='每帧缩略图及其时长，可下载PNG。'))
    tools.append(tool(
        'resample', '按时间重新采样', '时间轴', 'resample', [
            number('fps', '目标帧率', 12, .1, 50),
            select('mode', '采样方式', 'hold', [('hold', '保持源帧'), ('crossfade', '透明交叉插值')]),
            boolean('wrap', '末帧向首帧插值'),
        ], mutation=False, output='sampled_project', note='另建项目，保留总时长；使用各输出帧时间中点采样，源项目保持可编辑。',
    ))
    tools.append(tool('exposure-chart', '摄影表时间分布图', '时间轴', 'exposure-chart', mutation=False, output='image', method='GET', note='按实际帧时长绘制时间位置，可下载PNG。'))
    tools.append(tool(
        'collision', '像素碰撞叠加预览', '精灵', 'collision', [
            {'name': 'first', 'label': '基础帧', 'type': 'frame', 'default': 'current'},
            {'name': 'second', 'label': '碰撞对象帧', 'type': 'frame', 'default': 'next'},
            number('dx', '对象偏移X', 0, -2048, 2048),
            number('dy', '对象偏移Y', 0, -2048, 2048),
            number('threshold', '碰撞Alpha阈值', 1, 1, 255),
        ], mutation=False, output='image', note='红色标出两帧的可见像素重叠，碰撞对象先应用指定偏移。',
    ))
    tools.append(tool(
        'difference', '两帧像素差异', '诊断', 'difference', [
            {'name': 'first', 'label': '第一帧', 'type': 'frame', 'default': 'current'},
            {'name': 'second', 'label': '第二帧', 'type': 'frame', 'default': 'next'},
        ], mutation=False, output='json', note='比较合成后的RGBA像素，输出变化面积与平均差异。',
    ))
    tools.append(tool('frame-seam', '当前瓦片接缝检查', '瓦片', 'frames/{frame}/seam', mutation=False, output='json', method='GET', note='比较当前帧左右、上下边缘是否逐像素一致。'))
    tools.append(tool('repeat', '瓦片九格拼接预览', '瓦片', 'frames/{frame}/repeat', mutation=False, output='image', method='GET', note='以3×3排列查看当前帧的纹理接缝。'))
    tools.append(tool('alpha', '当前帧透明度检查', '诊断', 'frames/{frame}/alpha', mutation=False, output='json', method='GET', note='透明、半透明、不透明像素和隐藏RGB检查。'))
    return {'tools': tools, 'effects': EFFECTS, 'easing': EASING, 'anchors': ANCHORS}
