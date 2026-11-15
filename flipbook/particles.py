import bisect
import math
from PIL import Image, ImageDraw
from . import validate as v
from .model import new_frame, new_layer, new_id
from .composite import render_frame, blend_pixels

TIME_EPSILON = 1e-6
SLIDE_VELOCITY = .02
MAX_EVENTS = 24
MAX_PARTICLES = 400
MAX_WALLS = 32
MAX_WINDOWS = 8
MAX_EMITTERS = 20
MAX_DURATION = 60000
MASK64 = (1 << 64) - 1


def _stream(seed):
    state = seed & MASK64
    while True:
        state = (state + 0x9E3779B97F4A7C15) & MASK64
        z = state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        yield (z ^ (z >> 31)) & MASK64


def _uniform(stream):
    return (next(stream) >> 11) * (1.0 / 9007199254740992.0)


def _windows(value):
    result = []
    total = 0
    for item in v.array(value, '发射时段', MAX_WINDOWS):
        item = v.object_value(item, '发射时段')
        start = v.real(item.get('start'), '时段开始', 0, MAX_DURATION)
        end = v.real(item.get('end'), '时段结束', 0, MAX_DURATION)
        if end < start:
            raise v.Invalid('时段结束不能早于开始')
        count = v.integer(item.get('count'), '时段粒子数', 1, MAX_PARTICLES)
        total += count
        result.append({'start': start, 'end': end, 'count': count})
    if not result:
        raise v.Invalid('至少需要一个发射时段')
    if total > MAX_PARTICLES:
        raise v.Invalid(f'粒子总数最多{MAX_PARTICLES}个')
    return result


def _walls(value):
    result = []
    for item in v.array(value, '碰撞线段', MAX_WALLS):
        coords = v.array(item, '碰撞线段', 4)
        if len(coords) != 4:
            raise v.Invalid('碰撞线段需要x1,y1,x2,y2四个坐标')
        wall = [v.real(component, '线段坐标', -4096, 4096) for component in coords]
        if math.hypot(wall[2] - wall[0], wall[3] - wall[1]) < .5:
            raise v.Invalid('碰撞线段过短')
        result.append(wall)
    return result


def validate_emitter(body, width, height):
    body = v.object_value(body, '粒子发射器')
    config = {
        'name': v.text(body.get('name', '粒子发射器'), '发射器名称', 100, False),
        'seed': v.integer(body.get('seed', 1), '随机种子', 0, 2147483647),
        'origin': _origin(body.get('origin', [width / 2, 8])),
        'direction': v.real(body.get('direction', 90), '发射方向', -360, 360),
        'spread': v.real(body.get('spread', 40), '扩散角度', 0, 360),
        'speed_min': v.real(body.get('speed_min', 60), '最小初速', 0, 20000),
        'speed_max': v.real(body.get('speed_max', 120), '最大初速', 0, 20000),
        'gravity_x': v.real(body.get('gravity_x', 0), '水平重力', -4000, 4000),
        'gravity_y': v.real(body.get('gravity_y', 300), '垂直重力', -4000, 4000),
        'life_min': v.real(body.get('life_min', 400), '最短寿命', 20, MAX_DURATION),
        'life_max': v.real(body.get('life_max', 900), '最长寿命', 20, MAX_DURATION),
        'size': v.integer(body.get('size', 2), '粒子尺寸', 1, 8),
        'color_start': v.hex_color(body.get('color_start', '#ffe9a3ff')),
        'color_end': v.hex_color(body.get('color_end', '#ff5f3c00')),
        'collision': v.choice(body.get('collision', 'bounce'), '碰撞处理', ['bounce', 'vanish', 'none']),
        'restitution': v.real(body.get('restitution', .55), '弹性系数', 0, 1),
        'friction': v.real(body.get('friction', .1), '切向阻尼', 0, 1),
        'windows': _windows(body.get('windows')),
        'walls': _walls(body.get('walls', [])),
    }
    if config['speed_min'] > config['speed_max']:
        raise v.Invalid('最小初速不能大于最大初速')
    if config['life_min'] > config['life_max']:
        raise v.Invalid('最短寿命不能大于最长寿命')
    return config


def _origin(value):
    coords = v.array(value, '发射位置', 2)
    if len(coords) != 2:
        raise v.Invalid('发射位置需要x和y')
    return [v.real(coords[0], '发射位置X', -4096, 4096), v.real(coords[1], '发射位置Y', -4096, 4096)]


def validate_stored_emitters(project):
    seen = set()
    for emitter in v.array(project.get('particle_emitters', []), '粒子发射器', MAX_EMITTERS):
        emitter_id = v.key(emitter.get('id'))
        if emitter_id in seen:
            raise v.Invalid('粒子发射器编号重复')
        seen.add(emitter_id)
        validate_emitter(emitter, project['width'], project['height'])


def total_particles(config):
    return sum(window['count'] for window in config['windows'])


def emitter_duration(config):
    return math.ceil(max(window['end'] for window in config['windows']) + config['life_max'])


def _particle(config, index):
    stream = _stream(config['seed'] * 0x9E3779B97F4A7C15 + index * 0xDA942042E4DD58B5 + 0x165667B19E3779F9)
    jitter = _uniform(stream)
    remaining = index
    for window in config['windows']:
        if remaining < window['count']:
            span = window['end'] - window['start']
            birth = window['start'] + span * (remaining + jitter) / window['count']
            break
        remaining -= window['count']
    lifetime = config['life_min'] + _uniform(stream) * (config['life_max'] - config['life_min'])
    speed = config['speed_min'] + _uniform(stream) * (config['speed_max'] - config['speed_min'])
    angle = math.radians(config['direction'] + (_uniform(stream) - .5) * config['spread'])
    return {
        'birth': birth,
        'death': birth + lifetime,
        'x': config['origin'][0],
        'y': config['origin'][1],
        'vx': math.cos(angle) * speed / 1000,
        'vy': math.sin(angle) * speed / 1000,
    }


def _roots(a, b, c):
    if abs(a) < 1e-18:
        if abs(b) < 1e-18:
            return []
        return [-c / b]
    discriminant = b * b - 4 * a * c
    if discriminant < 0:
        return []
    q = -0.5 * (b + math.copysign(math.sqrt(discriminant), b))
    if q == 0:
        return [0.0]
    return sorted((q / a, c / q))


def _next_hit(walls, gravity, start, px, py, vx, vy, limit, exclude=-1):
    best = None
    latest = limit - start
    for index, (x1, y1, x2, y2) in enumerate(walls):
        if index == exclude:
            continue
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        nx, ny = -dy / length, dx / length
        a = .5 * (nx * gravity[0] + ny * gravity[1])
        b = nx * vx + ny * vy
        c = nx * (px - x1) + ny * (py - y1)
        for dt in _roots(a, b, c):
            if dt <= TIME_EPSILON or dt >= latest:
                continue
            hx = px + vx * dt + .5 * gravity[0] * dt * dt
            hy = py + vy * dt + .5 * gravity[1] * dt * dt
            along = ((hx - x1) * dx + (hy - y1) * dy) / (length * length)
            if not -1e-9 <= along <= 1 + 1e-9:
                continue
            if best is None or dt < best[0]:
                best = (dt, nx, ny, index)
    if best is None:
        return None
    return (start + best[0], best[1], best[2], best[3])


def _exit_slide(wall, gravity, px, py, vx, vy):
    x1, y1, x2, y2 = wall
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length
    a = .5 * (ux * gravity[0] + uy * gravity[1])
    b = ux * vx + uy * vy
    c = ux * (px - x1) + uy * (py - y1)
    best = None
    for target in (0.0, length):
        for dt in _roots(a, b, c - target):
            if dt > TIME_EPSILON and (best is None or dt < best):
                best = dt
    return best


def _gravity(config):
    return (config['gravity_x'] / 1000000, config['gravity_y'] / 1000000)


def simulate(config, index):
    particle = _particle(config, index)
    gravity = _gravity(config)
    death = particle['death']
    spans = [(particle['birth'], particle['x'], particle['y'], particle['vx'], particle['vy'], gravity[0], gravity[1], -1)]
    hits = 0
    if config['collision'] != 'none' and config['walls']:
        while hits < MAX_EVENTS:
            start, px, py, vx, vy, gx, gy, slide = spans[-1]
            hit = _next_hit(config['walls'], (gx, gy), start, px, py, vx, vy, death, slide)
            leave = _exit_slide(config['walls'][slide], (gx, gy), px, py, vx, vy) if slide >= 0 else None
            if leave is not None and (hit is None or leave < hit[0]):
                if start + leave >= death:
                    break
                spans.append((start + leave, px + vx * leave + .5 * gx * leave * leave,
                              py + vy * leave + .5 * gy * leave * leave,
                              vx + gx * leave, vy + gy * leave, gravity[0], gravity[1], -1))
                continue
            if hit is None:
                break
            t, nx, ny, wall_index = hit
            if config['collision'] == 'vanish':
                death = t
                break
            hits += 1
            dt = t - start
            hx = px + vx * dt + .5 * gx * dt * dt
            hy = py + vy * dt + .5 * gy * dt * dt
            rvx = vx + gx * dt
            rvy = vy + gy * dt
            if rvx * nx + rvy * ny < 0:
                onx, ony = nx, ny
            else:
                onx, ony = -nx, -ny
            normal = rvx * onx + rvy * ony
            tangent_x, tangent_y = -ony, onx
            tangent = rvx * tangent_x + rvy * tangent_y
            normal *= -config['restitution']
            tangent *= 1 - config['friction']
            rvx = tangent_x * tangent + onx * normal
            rvy = tangent_y * tangent + ony * normal
            press = onx * gravity[0] + ony * gravity[1]
            if abs(normal) < SLIDE_VELOCITY and press < 0:
                svx = rvx - (rvx * onx + rvy * ony) * onx
                svy = rvy - (rvy * onx + rvy * ony) * ony
                spans.append((t, hx, hy, svx, svy,
                              gravity[0] - press * onx, gravity[1] - press * ony, wall_index))
            else:
                spans.append((t, hx, hy, rvx, rvy, gravity[0], gravity[1], -1))
    return {
        'spans': spans,
        'starts': [span[0] for span in spans],
        'birth': particle['birth'],
        'death': death,
        'natural_death': particle['death'],
        'hits': hits,
    }


def build_timelines(config):
    return [simulate(config, index) for index in range(total_particles(config))]


def position(timeline, time_ms):
    spans = timeline['spans']
    index = bisect.bisect_right(timeline['starts'], time_ms) - 1
    index = max(0, min(index, len(spans) - 1))
    start, px, py, vx, vy, gx, gy, _ = spans[index]
    dt = time_ms - start
    return (px + vx * dt + .5 * gx * dt * dt,
            py + vy * dt + .5 * gy * dt * dt)


def live_particles(config, time_ms, timelines=None):
    for index in range(total_particles(config)):
        if timelines is None:
            particle = _particle(config, index)
            if not particle['birth'] <= time_ms < particle['death']:
                continue
            timeline = simulate(config, index)
        else:
            timeline = timelines[index]
        if timeline['birth'] <= time_ms < timeline['death']:
            yield index, timeline


def state_response(config, time_ms):
    rows = []
    for index, timeline in live_particles(config, time_ms):
        x, y = position(timeline, time_ms)
        rows.append({'index': index, 'x': round(x, 6), 'y': round(y, 6)})
    return {'time_ms': time_ms, 'count': len(rows), 'particles': rows}


def summary(config, timelines=None):
    timelines = timelines if timelines is not None else build_timelines(config)
    return {
        'particles': len(timelines),
        'duration_ms': emitter_duration(config),
        'bounces': sum(timeline['hits'] for timeline in timelines),
        'vanished': sum(1 for timeline in timelines if timeline['death'] < timeline['natural_death']),
        'max_events': max((timeline['hits'] for timeline in timelines), default=0),
        'windows': len(config['windows']),
        'walls': len(config['walls']),
    }


def render_particles(config, size, time_ms, timelines=None):
    image = Image.new('RGBA', size)
    painter = ImageDraw.Draw(image)
    first = v.rgba(config['color_start'])
    last = v.rgba(config['color_end'])
    for _, timeline in live_particles(config, time_ms, timelines):
        x, y = position(timeline, time_ms)
        span = timeline['death'] - timeline['birth']
        age = (time_ms - timeline['birth']) / span if span > 0 else 1
        color = tuple(round(a + (b - a) * age) for a, b in zip(first, last))
        left = math.floor(x + .5 - config['size'] / 2)
        top = math.floor(y + .5 - config['size'] / 2)
        painter.rectangle((left, top, left + config['size'] - 1, top + config['size'] - 1), fill=color)
    return image


def source_frame_at(project, time_ms):
    total = sum(frame['duration'] for frame in project['frames'])
    at = time_ms % total
    elapsed = 0
    for frame in project['frames']:
        if at < elapsed + frame['duration']:
            return frame
        elapsed += frame['duration']
    return project['frames'][-1]


def preview_image(store, project, body):
    config = validate_emitter(body.get('emitter'), project['width'], project['height'])
    time_ms = v.real(body.get('time_ms', 0), '预览时间', 0, 3600000)
    size = (project['width'], project['height'])
    if v.boolean(body.get('show_source', True), '叠加原动画'):
        base = render_frame(store, project, source_frame_at(project, time_ms)['id'])
    else:
        base = Image.new('RGBA', size)
    base = blend_pixels(base, render_particles(config, size, time_ms))
    if v.boolean(body.get('show_walls', True), '显示碰撞线段'):
        painter = ImageDraw.Draw(base)
        for x1, y1, x2, y2 in config['walls']:
            painter.line((x1, y1, x2, y2), fill=(255, 95, 110, 255))
            painter.rectangle((x1 - 1, y1 - 1, x1 + 1, y1 + 1), fill=(255, 220, 120, 255))
            painter.rectangle((x2 - 1, y2 - 1, x2 + 1, y2 + 1), fill=(255, 220, 120, 255))
    return base, config


def save_emitter(store, project_id, revision, body):
    with store.edit(project_id, revision, '保存粒子发射器') as project:
        config = validate_emitter(body.get('emitter'), project['width'], project['height'])
        emitters = project.setdefault('particle_emitters', [])
        emitter_id = body.get('emitter_id')
        if emitter_id is not None:
            v.key(emitter_id)
            found = next((item for item in emitters if item['id'] == emitter_id), None)
            if found is None:
                raise v.Invalid('粒子发射器不存在')
            config['id'] = emitter_id
            emitters[emitters.index(found)] = config
        else:
            if len(emitters) >= MAX_EMITTERS:
                raise v.Invalid(f'粒子发射器最多{MAX_EMITTERS}个')
            config['id'] = new_id()
            emitters.append(config)
    return store.get(project_id)


def remove_emitter(store, project_id, revision, emitter_id):
    v.key(emitter_id)
    with store.edit(project_id, revision, '移除粒子发射器') as project:
        emitters = project.get('particle_emitters', [])
        found = next((item for item in emitters if item['id'] == emitter_id), None)
        if found is None:
            raise v.Invalid('粒子发射器不存在')
        emitters.remove(found)
    return store.get(project_id)


def generate(store, project_id, body):
    with store.lock:
        original = store.get(project_id)
        config = validate_emitter(body.get('emitter'), original['width'], original['height'])
        fps = v.real(body.get('fps', 12), '输出帧率', 1, 50)
        mode = v.choice(body.get('mode', 'overlay'), '生成模式', ['overlay', 'particles_only'])
        duration = v.integer(body.get('duration_ms') or emitter_duration(config), '生成总时长', 20, MAX_DURATION)
        frame_ms = max(20, int(round(1000 / fps)))
        count = -(-duration // frame_ms)
        if count > 300:
            raise v.Invalid('生成帧数超过300，请降低帧率或缩短总时长')
        v.pixel_budget(original['width'], original['height'], count * 2)
        created = store.create(original['name'][:90] + ' · 粒子', original['width'], original['height'])
        timelines = build_timelines(config)
        size = (original['width'], original['height'])
        blank = Image.new('RGBA', size)
        with store.edit(created['id'], 0, '生成粒子动画') as project:
            project['frames'] = []
            project['palette'] = original['palette'][:]
            project['background'] = original['background']
            project['loop'] = original['loop']
            stored = dict(config)
            stored['id'] = new_id()
            project['particle_emitters'] = [stored]
            for index in range(count):
                at = index * frame_ms
                particle_asset = store.put_image(created['id'], render_particles(config, size, at, timelines))
                if mode == 'overlay':
                    base_asset = store.put_image(created['id'], render_frame(store, original, source_frame_at(original, at)['id']))
                    frame = new_frame(base_asset, f'粒子 {index + 1}', frame_ms)
                    frame['layers'][0]['name'] = '来源画面'
                else:
                    frame = new_frame(store.put_image(created['id'], blank), f'粒子 {index + 1}', frame_ms)
                frame['layers'].append(new_layer('粒子火花', particle_asset))
                project['frames'].append(frame)
            project['clips'] = [{
                'id': new_id(),
                'name': '粒子循环',
                'frame_ids': [frame['id'] for frame in project['frames']],
                'direction': 'forward',
                'repeat': original['loop'],
            }]
            project['notes'] = f'来源项目{project_id}，修订{original["revision"]}；种子{config["seed"]}，模式{mode}，每帧{frame_ms}ms。'
        return {'project': store.get(created['id']), 'particles': summary(config, timelines)}
