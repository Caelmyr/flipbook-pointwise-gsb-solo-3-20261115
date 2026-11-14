import base64
import html
from .analysis import animation_report, motion_contact_sheet
from .composite import png_bytes


def escaped(value):
    return html.escape(str(value), quote=True)


def table(headers, rows):
    head = ''.join('<th>' + escaped(value) + '</th>' for value in headers)
    result = '<table><thead><tr>' + head + '</tr></thead><tbody>'
    for row in rows:
        result += '<tr>' + ''.join('<td>' + escaped(value) + '</td>' for value in row) + '</tr>'
    return result + '</tbody></table>'


def image_uri(image):
    return 'data:image/png;base64,' + base64.b64encode(png_bytes(image)).decode('ascii')


def project_report(store, project):
    report = animation_report(store, project)
    sheet = motion_contact_sheet(store, project)
    frame_rows = []
    for index, frame in enumerate(project['frames']):
        metric = report['frames'][index]
        frame_rows.append([
            index + 1,
            frame['name'],
            frame['duration'],
            len(frame['layers']),
            metric['visible_pixels'],
            metric['visible_colors'],
            metric['coverage_percent'],
            ','.join(map(str, frame['pivot'])),
        ])
    clip_rows = []
    for clip in project['clips']:
        known = {frame['id']: frame for frame in project['frames']}
        duration = sum(known[key]['duration'] for key in clip['frame_ids'])
        clip_rows.append([clip['name'], len(clip['frame_ids']), duration, clip['direction'], clip['repeat']])
    notes = ''.join('<li>' + escaped(item) + '</li>' for item in report['warnings'])
    metadata = table(['项目', '值'], [
        ['名称', project['name']],
        ['画布', f'{project["width"]} × {project["height"]}'],
        ['当前修订', project['revision']],
        ['帧数', len(project['frames'])],
        ['单次总时长 ms', report['total_duration']],
        ['平均帧率', report['average_fps']],
        ['说明', project['notes']],
    ])
    swatches = ''.join('<span style="background:' + color[:7] + ';display:inline-block;width:34px;height:34px;border:1px solid #aaa" title="' + escaped(color) + '"></span>' for color in project['palette'])
    result = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>动画制作报告</title>'
    result += '<style>body{max-width:1150px;margin:30px auto;font:14px sans-serif;padding:20px;color:#203342}h1{color:#268b75}table{width:100%;border-collapse:collapse;font-size:12px}td,th{padding:8px;border:1px solid #ccd6dd;text-align:left}th{background:#edf4f2}img{max-width:100%}section{margin:24px 0}@media print{button{display:none}section{break-inside:avoid}}</style><body>'
    result += '<button onclick="window.print()">打印 / 另存PDF</button><h1>逐帧 · 动画制作报告</h1>'
    result += '<section>' + metadata + '</section>'
    result += '<section><h2>帧联系表</h2><img src="' + image_uri(sheet) + '"></section>'
    result += '<section><h2>调色板</h2>' + swatches + '</section>'
    result += '<section><h2>时间轴与图像指标</h2>' + table(['序号', '名称', '时长ms', '图层', '可见像素', '颜色数', '覆盖率%', '枢轴'], frame_rows) + '</section>'
    result += '<section><h2>动画片段</h2>' + table(['名称', '引用帧数', '正向基础时长', '方向', '循环'], clip_rows) + '</section>'
    result += '<section><h2>导出预检</h2><ul>' + notes + '</ul><p>APNG保留完整透明度；GIF限制为二值透明及255个可见颜色，帧时长会量化。图集旋转、裁边和枢轴需由目标引擎按元数据解释。</p></section>'
    result += '<footer>Flipbook Studio · 图像逐帧创作 · 修订 ' + str(project['revision']) + '</footer></body></html>'
    return result
