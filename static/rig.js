/* Character bone rig: hierarchy editing, IK targets and key poses. */

const R = {
  state: null,           // GET /rig payload
  project: null,         // project snapshot at open time
  pose: {rotations: {}, root: [0, 0], targets: []},
  report: null,          // last solved preview report
  image: null,           // data URL of the preview
  selected: null,        // selected bone id
  stash: [],             // recorded poses for keyframe generation
  drag: null,
  token: 0,
  timer: null,
  zoom: 6,
  dirty: false,
};

function rBone(id) {
  return R.state?.bones.find(b => b.id === id) || null;
}

function rClone(value) {
  return JSON.parse(JSON.stringify(value));
}

function rNormalizeAngle(value) {
  let result = ((Number(value) + 180) % 360 + 360) % 360 - 180;
  return result;
}

function rPoseClone() {
  const copy = rClone(R.pose);
  for (const item of copy.targets) {
    if (item.pin && item.target) delete item.target;
  }
  return copy;
}

function rPosePayload() {
  const copy = rClone(R.pose);
  for (const item of copy.targets) {
    if (item.pin && item.target) delete item.target;
  }
  return copy;
}

function rResidualText(target) {
  if (!target) return '—';
  const value = Number(target.residual);
  if (!Number.isFinite(value)) return '—';
  if (value <= 0.05) return '已到目标';
  return '剩余误差 ' + value.toFixed(2) + ' px';
}

/* ---------------------------------------------------------------- dialog */

async function openRig() {
  project();
  R.project = rClone(project());
  R.state = await api(endpoint('rig'));
  R.pose = {rotations: {}, root: [0, 0], targets: []};
  const frame = currentFrame();
  if (R.state.rig && frame.pose) R.pose = rClone(frame.pose);
  R.selected = R.state.bones[0]?.id || null;
  R.stash = [];
  R.report = null;
  const body = dialog('骨骼绑定与角色姿态');
  body.replaceChildren();
  const layout = element('div', undefined, 'rig-layout');
  const left = element('div', undefined, 'rig-side');
  const right = element('div', undefined, 'rig-main');
  const panel = element('div', undefined, 'rig-side');
  layout.append(left, right, panel);
  body.append(layout);
  R.nodes = {left, right, panel};
  renderRigLeft();
  renderRigMain();
  await refreshRigPreview();
}

function rigField(labelText, value, options = {}) {
  const wrapper = element('label', labelText, 'rig-field');
  const input = element('input');
  input.type = options.type || 'number';
  input.value = value;
  if (options.min !== undefined) input.min = options.min;
  if (options.max !== undefined) input.max = options.max;
  input.step = options.type === 'number' ? 'any' : '';
  if (options.type === 'checkbox') {
    input.checked = Boolean(value);
    wrapper.prepend(input);
    wrapper.className = 'check';
  } else {
    wrapper.append(input);
  }
  if (options.onchange) input.onchange = options.onchange;
  wrapper.input = input;
  return wrapper;
}

function renderRigLeft() {
  const {left} = R.nodes;
  left.replaceChildren();
  left.append(element('h3', '骨骼结构'));
  if (!R.state.rig) {
    left.append(element('p', '当前项目还没有骨骼绑定。可从当前帧的可见图层自动建立，再调整关节位置、父子关系、活动范围与图层绑定。', 'hint'));
    const form = element('form', undefined, 'form');
    form.append(rigField('根骨骼名称', '根', {type: 'text'}));
    const names = currentFrame().layers.filter(l => l.kind !== 'group' && l.visible).map(l => l.name);
    const checks = [];
    for (const name of names) {
      const label = element('label', undefined, 'check');
      const input = element('input');
      input.type = 'checkbox';
      input.checked = true;
      label.append(input, document.createTextNode(name));
      form.append(label);
      checks.push({input, name});
    }
    const submit = element('button', '从可见图层建立骨骼', 'primary');
    submit.type = 'submit';
    form.append(submit, element('p', '', 'form-error'));
    form.onsubmit = guard(async event => {
      event.preventDefault();
      const layers = checks.filter(item => item.input.checked).map(item => item.name);
      const rootName = form.querySelector('input[type=text]').value || '根';
      const result = await work('建立骨骼绑定…', () => post(endpoint('rig/auto'), {
        revision: project().revision, frame_id: currentFrame().id, root_name: rootName, layers,
      }));
      await acceptProject(result);
      R.project = rClone(result);
      await openRig();
      status('骨骼与图层绑定已建立，可拖动手脚目标调整姿态');
    });
    left.append(form);
    return;
  }
  const bones = rClone(R.state.rig.bones);
  const names = bones.map(bone => bone.name);
  const bindings = rClone(R.state.rig.bindings);
  const boundByBone = {};
  for (const binding of bindings) boundByBone[binding.bone] = binding;
  const layerNames = [...new Set(currentFrame().layers.filter(l => l.kind !== 'group').map(l => l.name))];
  left.append(element('p', `共 ${bones.length} 根骨骼、${bindings.length} 个图层绑定。拖动画布上的关节可转动骨骼，拖动圆点可移动 IK 目标。`, 'muted'));
  if (!bindings.length) {
    left.append(element('p', '还没有图层绑定：选择每根骨骼要驱动的图层后“保存骨骼结构”，未绑定的图层不会随骨骼移动。', 'hint'));
  }
  const drafts = [];
  bones.forEach((bone, index) => {
    const details = element('details', undefined, 'rig-bone');
    details.open = bone.id === R.selected;
    const summary = element('summary', `${index + 1}. ${bone.name}${bone.parent ? '' : ' · 根'}`);
    details.append(summary);
    const nameInput = rigField('名称', bone.name, {type: 'text'});
    details.append(nameInput);
    const parentSelect = element('select');
    parentSelect.append(new Option('（无父骨骼）', ''));
    for (const other of bones) {
      if (other.id !== bone.id) parentSelect.append(new Option(other.name, other.id));
    }
    parentSelect.value = bone.parent || '';
    const parentLabel = element('label', '父骨骼', 'rig-field');
    parentLabel.append(parentSelect);
    details.append(parentLabel);
    const pair = element('div', 'rig-pair');
    const originX = rigField('关节X', bone.origin[0]);
    const originY = rigField('关节Y', bone.origin[1]);
    pair.append(originX, originY);
    details.append(pair);
    const pair2 = element('div', 'rig-pair');
    pair2.append(rigField('静止角度°', bone.angle), rigField('骨骼长度', bone.length, {min: 0.5}));
    details.append(pair2);
    const pair3 = element('div', 'rig-pair');
    pair3.append(rigField('活动下限°', bone.limits.min, {min: -360, max: 360}), rigField('活动上限°', bone.limits.max, {min: -360, max: 360}));
    details.append(pair3);
    const targetLabel = element('label', undefined, 'check');
    const targetInput = element('input');
    targetInput.type = 'checkbox';
    targetInput.checked = Boolean(bone.target);
    targetLabel.append(targetInput, document.createTextNode('可作为 IK 目标（手/脚）'));
    details.append(targetLabel);
    const bindSelect = element('select');
    bindSelect.append(new Option('（不绑定图层）', ''));
    for (const name of layerNames) bindSelect.append(new Option(name, name));
    bindSelect.value = boundByBone[bone.id]?.layer || '';
    const bindLabel = element('label', '绑定图层', 'rig-field');
    bindLabel.append(bindSelect);
    details.append(bindLabel);
    const buttons = element('div', undefined, 'rig-inline');
    const focus = element('button', '选中骨骼');
    focus.type = 'button';
    focus.onclick = event => {
      event.preventDefault();
      R.selected = bone.id;
      renderRigLeft();
      renderRigPanel();
      drawRigCanvas();
      refreshRigPreview();
    };
    const remove = element('button', '删除骨骼');
    remove.type = 'button';
    remove.onclick = event => {
      event.preventDefault();
      if (bones.length <= 1) { status('至少保留一根骨骼', true); return; }
      bone._delete = true;
      renderRigLeft();
    };
    buttons.append(focus, remove);
    details.append(buttons);
    left.append(details);
    drafts.push({bone, nameInput, parentSelect, originX, originY, angleField: pair2.children[0], lengthField: pair2.children[1], lowField: pair3.children[0], highField: pair3.children[1], targetInput, bindSelect, details});
  });
  const add = element('button', '＋ 添加骨骼');
  add.type = 'button';
  add.onclick = () => {
    const id = crypto.randomUUID().replaceAll('-', '');
    bones.push({id, name: '骨骼 ' + (bones.length + 1), parent: bones[0]?.id || null, origin: [project().width / 2, project().height / 2], angle: -90, length: 8, limits: {min: -180, max: 180}, target: true});
    R.state.rig.bones = bones;
    R.selected = id;
    renderRigLeft();
    drawRigCanvas();
  };
  const save = element('button', '保存骨骼结构', 'primary');
  save.type = 'button';
  save.onclick = guard(async () => {
    const nextBones = [];
    const nextBindings = [];
    for (const draft of drafts) {
      const bone = draft.bone;
      if (bone._delete) continue;
      const name = draft.nameInput.input.value.trim();
      if (!name) throw Error('骨骼名称不能为空');
      const payload = {
        id: bone.id,
        name,
        parent: draft.parentSelect.value || null,
        origin: [Number(draft.originX.input.value), Number(draft.originY.input.value)],
        angle: Number(draft.angleField.input.value),
        length: Number(draft.lengthField.input.value),
        limit_min: Number(draft.lowField.input.value),
        limit_max: Number(draft.highField.input.value),
        target: draft.targetInput.checked,
      };
      if (payload.parent === payload.id) throw Error('骨骼不能以自身为父骨骼');
      nextBones.push(payload);
      const layer = draft.bindSelect.value;
      if (layer) {
        const existing = boundByBone[bone.id];
        nextBindings.push({bone: bone.id, layer, ...(existing && existing.layer === layer ? {bind: existing.bind} : {})});
      }
    }
    if (!nextBones.length) throw Error('至少保留一根骨骼');
    const rootId = nextBones.some(item => item.id === R.state.rig.root) ? R.state.rig.root : nextBones[0].id;
    const result = await work('保存骨骼结构…', () => post(endpoint('rig'), {
      revision: project().revision,
      rig: {bones: nextBones, bindings: nextBindings, root: rootId, created_frame: R.state.rig.created_frame, notes: R.state.rig.notes},
    }, 'PUT'));
    await acceptProject(result);
    R.project = rClone(result);
    await openRig();
    status('骨骼结构与图层绑定已保存');
  });
  const auto = element('button', '按当前帧重新自动建立');
  auto.type = 'button';
  auto.onclick = guard(async () => {
    if (!confirm('重新建立会以当前帧图层变换作为新的绑定并替换现有骨骼，继续？')) return;
    const result = await work('重建骨骼…', () => post(endpoint('rig/auto'), {revision: project().revision, frame_id: currentFrame().id, root_name: '根'}));
    await acceptProject(result);
    R.project = rClone(result);
    await openRig();
  });
  const remove = element('button', '移除骨骼绑定');
  remove.type = 'button';
  remove.onclick = guard(async () => {
    if (!confirm('移除骨骼绑定不会还原已保存姿态的图层变换，继续？')) return;
    const result = await work('移除骨骼绑定…', () => post(endpoint('rig'), {}, 'DELETE').catch(error => { throw error; }));
    await acceptProject(result);
    R.project = rClone(result);
    await openRig();
  });
  const actions = element('div', undefined, 'rig-inline');
  actions.append(save, add);
  left.append(actions, element('p'), (() => {
    const row = element('div', undefined, 'rig-inline');
    row.append(auto, remove);
    return row;
  })());
  left.append(element('p', '提示：带子骨骼的骨骼应先把子骨骼关节位置定好再绑定图层；每次保存只写骨骼结构，不改已保存的姿态帧。', 'hint'));
}

/* ------------------------------------------------------------ pose canvas */

function renderRigMain() {
  const {right} = R.nodes;
  right.replaceChildren();
  const header = element('div', undefined, 'rig-canvas-head');
  header.append(element('b', '姿态预览'), element('span', '拖动关节旋转 · 拖动圆点移动 IK 目标 · 拖动根骨骼方块移动身体', 'muted'));
  const zoom = element('select');
  for (const value of [2, 3, 4, 5, 6, 8, 10, 12]) zoom.append(new Option(value + '×', String(value)));
  R.zoom = Math.max(2, Math.min(8, Math.floor(360 / Math.max(project().width, project().height)) || 2));
  zoom.value = String(R.zoom);
  zoom.onchange = () => { R.zoom = Number(zoom.value); drawRigCanvas(); };
  header.append(zoom);
  const stage = element('div', undefined, 'rig-stage');
  const canvas = element('canvas');
  canvas.id = 'rigCanvas';
  stage.append(canvas);
  right.append(header, stage);
  R.canvas = canvas;
  canvas.width = project().width;
  canvas.height = project().height;
  canvas.style.width = project().width * R.zoom + 'px';
  canvas.style.height = project().height * R.zoom + 'px';
  canvas.onpointerdown = rigPointerDown;
  canvas.onpointermove = rigPointerMove;
  canvas.onpointerup = rigPointerUp;
  canvas.onpointercancel = () => { R.drag = null; drawRigCanvas(); };
  const legend = element('div', undefined, 'rig-legend');
  legend.append(
    element('span', '● 关节'),
    element('span', '◆ 根骨骼'),
    element('span', '◎ IK 目标'),
    element('span', '× 够不到时的实际末端'),
  );
  right.append(legend);
  drawRigCanvas();
}

function rigPoint(event) {
  const rect = R.canvas.getBoundingClientRect();
  return [
    (event.clientX - rect.left) / rect.width * project().width,
    (event.clientY - rect.top) / rect.height * project().height,
  ];
}

function rigHit(point) {
  const scale = project().width / R.canvas.getBoundingClientRect().width;
  const radius = 9 * scale;
  const targets = R.pose.targets || [];
  for (let index = targets.length - 1; index >= 0; index -= 1) {
    const item = targets[index];
    const solved = R.report?.targets?.[index];
    const position = solved?.target || item.target;
    if (Math.hypot(position[0] - point[0], position[1] - point[1]) <= radius) {
      return {type: 'target', index};
    }
  }
  const bones = R.report?.bones || [];
  const rootBone = bones.find(item => item.id === R.state.rig?.root);
  const rootHit = rootBone && Math.hypot(rootBone.joint[0] - point[0], rootBone.joint[1] - point[1]) <= radius;
  for (let index = bones.length - 1; index >= 0; index -= 1) {
    const bone = bones[index];
    if (bone.id === rootBone?.id) continue;
    if (Math.hypot(bone.joint[0] - point[0], bone.joint[1] - point[1]) <= radius) return {type: 'joint', id: bone.id, joint: bone.joint, angle: bone.angle};
  }
  if (rootHit) return {type: 'root', start: R.pose.root.slice(), point};
  return null;
}

function rigPointerDown(event) {
  if (!R.report) return;
  const point = rigPoint(event);
  const hit = rigHit(point);
  if (!hit) return;
  R.canvas.setPointerCapture(event.pointerId);
  if (hit.type === 'joint') {
    R.selected = hit.id;
    R.drag = {type: 'joint', id: hit.id, joint: hit.joint, startAngle: hit.angle, base: R.pose.rotations[hit.id] || 0};
  } else if (hit.type === 'root') {
    R.drag = {type: 'root', start: hit.start, origin: hit.point};
  } else {
    R.drag = {type: 'target', index: hit.index};
  }
  renderRigPanel();
  drawRigCanvas();
}

function rigPointerMove(event) {
  if (!R.drag) return;
  const point = rigPoint(event);
  if (R.drag.type === 'joint') {
    const joint = R.drag.joint;
    const desired = Math.atan2(point[1] - joint[1], point[0] - joint[0]) * 180 / Math.PI;
    R.pose.rotations[R.drag.id] = rNormalizeAngle(R.drag.base + rNormalizeAngle(desired - R.drag.startAngle));
  } else if (R.drag.type === 'root') {
    R.pose.root = [
      Math.round(R.drag.start[0] + point[0] - R.drag.origin[0]),
      Math.round(R.drag.start[1] + point[1] - R.drag.origin[1]),
    ];
  } else {
    const item = R.pose.targets[R.drag.index];
    item.target = [Math.round(point[0] * 100) / 100, Math.round(point[1] * 100) / 100];
  }
  scheduleRigPreview();
  drawRigCanvas();
}

function rigPointerUp() {
  if (!R.drag) return;
  R.drag = null;
  scheduleRigPreview(true);
  drawRigCanvas();
}

function scheduleRigPreview(immediate = false) {
  if (R.timer) clearTimeout(R.timer);
  R.timer = setTimeout(() => { R.timer = null; refreshRigPreview(); }, immediate ? 0 : 40);
}

async function refreshRigPreview() {
  if (!R.state?.rig) { drawRigCanvas(); return; }
  const token = ++R.token;
  let data;
  try {
    data = await post(endpoint('rig/preview'), {frame_id: currentFrame().id, pose: rPosePayload()});
  } catch (error) {
    status(error.message, true);
    return;
  }
  if (token !== R.token) return;
  R.image = data.image;
  R.report = data.report;
  R.pose = rClone(data.report.pose);
  for (const item of R.pose.targets) {
    if (item.pin && item.target) delete item.target;
  }
  drawRigCanvas();
  renderRigPanel();
}

function drawRigCanvas() {
  const canvas = R.canvas;
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.imageSmoothingEnabled = false;
  ctx.fillStyle = '#111e28';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  if (R.image) {
    const image = new Image();
    image.onload = () => {
      if (!R.canvas) return;
      const live = R.canvas.getContext('2d');
      live.clearRect(0, 0, canvas.width, canvas.height);
      live.fillStyle = '#111e28';
      live.fillRect(0, 0, canvas.width, canvas.height);
      live.imageSmoothingEnabled = false;
      live.drawImage(image, 0, 0, canvas.width, canvas.height);
      drawRigOverlay();
    };
    image.src = R.image;
    drawRigOverlay();
  } else {
    drawRigOverlay();
  }
}

function drawRigOverlay() {
  const canvas = R.canvas;
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const pixel = canvas.width / Math.max(1, canvas.getBoundingClientRect().width);
  ctx.save();
  ctx.lineWidth = Math.max(1, 1.4 * pixel);
  const bones = R.report?.bones || [];
  for (const bone of bones) {
    const active = bone.id === R.selected;
    ctx.strokeStyle = active ? '#ffd166' : '#7ae2c2';
    ctx.beginPath();
    ctx.moveTo(bone.joint[0], bone.joint[1]);
    ctx.lineTo(bone.tip[0], bone.tip[1]);
    ctx.stroke();
    ctx.fillStyle = active ? '#ffd166' : '#a3f3d8';
    ctx.beginPath();
    ctx.arc(bone.joint[0], bone.joint[1], 2.2 * pixel, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = '#0e1a22';
    ctx.font = `${Math.max(3, 4 * pixel)}px sans-serif`;
    ctx.fillText(bone.name, bone.joint[0] + 2.4 * pixel, bone.joint[1] - 1.6 * pixel);
  }
  const rootBone = bones.find(item => item.id === R.state?.rig?.root);
  if (rootBone) {
    ctx.fillStyle = '#8c9bff';
    const size = 2.6 * pixel;
    ctx.fillRect(rootBone.joint[0] - size, rootBone.joint[1] - size, size * 2, size * 2);
  }
  const targets = R.report?.targets || [];
  targets.forEach((target, index) => {
    const local = R.pose.targets[index];
    const position = local?.target || target.target;
    const reached = target.reached || target.target;
    const missed = Number(target.residual) > 0.05;
    ctx.strokeStyle = missed ? '#ff7b8a' : '#66e2bd';
    ctx.lineWidth = Math.max(1, pixel);
    ctx.beginPath();
    ctx.arc(position[0], position[1], 3.4 * pixel, 0, Math.PI * 2);
    ctx.stroke();
    if (Number(target.pin)) {
      ctx.setLineDash([1.5 * pixel, 1.5 * pixel]);
      ctx.beginPath();
      ctx.moveTo(position[0] - 3.4 * pixel, position[1]);
      ctx.lineTo(position[0] + 3.4 * pixel, position[1]);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    if (missed) {
      ctx.strokeStyle = '#ff7b8a';
      ctx.beginPath();
      ctx.moveTo(reached[0] - 1.6 * pixel, reached[1] - 1.6 * pixel);
      ctx.lineTo(reached[0] + 1.6 * pixel, reached[1] + 1.6 * pixel);
      ctx.moveTo(reached[0] + 1.6 * pixel, reached[1] - 1.6 * pixel);
      ctx.lineTo(reached[0] - 1.6 * pixel, reached[1] + 1.6 * pixel);
      ctx.stroke();
      ctx.fillStyle = '#ffbcc3';
      ctx.fillText(Number(target.residual).toFixed(1) + 'px', reached[0] + 2.2 * pixel, reached[1] + 3.4 * pixel);
    }
  });
  ctx.restore();
}

/* ---------------------------------------------------------- pose controls */

function renderRigPanel() {
  const {panel} = R.nodes;
  if (!panel) return;
  panel.replaceChildren();
  const rig = R.state.rig;
  panel.append(element('h3', '姿态与动作帧'));
  if (!rig) {
    panel.append(element('p', '先建立骨骼绑定，才能调整姿态。', 'muted'));
    return;
  }
  const targetRow = element('div', undefined, 'rig-targets');
  targetRow.append(element('b', 'IK 目标与剩余误差'));
  R.pose.targets.forEach((target, index) => {
    const solved = R.report?.targets?.[index];
    const row = element('div', undefined, 'rig-target');
    const bone = rBone(target.bone);
    row.append(element('span', bone?.name || target.bone.slice(0, 6)));
    const pair = element('div', 'rig-pair');
    const current = solved?.target || target.target || (R.report?.bones?.find(item => item.id === target.bone)?.tip) || [0, 0];
    const x = rigField('目标X', current[0]);
    const y = rigField('目标Y', current[1]);
    const update = () => {
      target.target = [Number(x.input.value), Number(y.input.value)];
      scheduleRigPreview();
    };
    x.input.onchange = update;
    y.input.onchange = update;
    pair.append(x, y);
    if (target.pin) pair.append(element('small', '已固定：目标取绑定位置，未填写坐标', 'muted'));
    row.append(pair);
    const pinLabel = element('label', undefined, 'check');
    const pin = element('input');
    pin.type = 'checkbox';
    pin.checked = Boolean(target.pin);
    pin.onchange = () => {
      target.pin = pin.checked;
      if (pin.checked) delete target.target;
      scheduleRigPreview(true);
    };
    pinLabel.append(pin, document.createTextNode('固定脚掌/原位'));
    row.append(pinLabel);
    row.append(element('small', rResidualText(solved), solved && Number(solved.residual) > 0.05 ? 'rig-warn' : 'muted'));
    const remove = element('button', '移除');
    remove.type = 'button';
    remove.onclick = () => {
      R.pose.targets.splice(index, 1);
      renderRigPanel();
      scheduleRigPreview(true);
    };
    row.append(remove);
    targetRow.append(row);
  });
  const addTarget = element('button', '＋ 添加 IK 目标');
  addTarget.type = 'button';
  addTarget.onclick = () => {
    const candidates = R.state.bones.filter(bone => bone.target);
    const bone = candidates[0] || R.state.bones[R.state.bones.length - 1];
    if (!bone) return;
    const tip = R.report?.bones?.find(item => item.id === bone.id)?.tip || bone.rest_tip;
    R.pose.targets.push({bone: bone.id, target: [tip[0], tip[1]]});
    renderRigPanel();
    scheduleRigPreview(true);
  };
  panel.append(targetRow, addTarget);

  const rotation = element('div', undefined, 'rig-targets');
  rotation.append(element('b', '选中关节'));
  const bone = rBone(R.selected) || R.state.bones[0];
  if (bone) {
    const solved = R.report?.bones?.find(item => item.id === bone.id);
    rotation.append(element('p', `${bone.name} · 当前 ${solved ? solved.delta.toFixed(1) : '0'}° · 活动范围 ${bone.limit_min}° 到 ${bone.limit_max}°`, 'muted'));
    const slider = element('input');
    slider.type = 'range';
    slider.min = String(bone.limit_min);
    slider.max = String(bone.limit_max);
    slider.step = '1';
    slider.value = String(R.pose.rotations[bone.id] || 0);
    slider.oninput = () => {
      R.pose.rotations[bone.id] = Number(slider.value);
      scheduleRigPreview();
    };
    slider.style.width = '100%';
    rotation.append(slider);
    const row = element('div', undefined, 'rig-inline');
    const reset = element('button', '该关节归零');
    reset.type = 'button';
    reset.onclick = () => {
      delete R.pose.rotations[bone.id];
      renderRigPanel();
      scheduleRigPreview(true);
    };
    const pin = element('button', '记录为 IK 目标');
    pin.type = 'button';
    pin.onclick = () => {
      const tip = solved?.tip || bone.rest_tip;
      R.pose.targets.push({bone: bone.id, target: [tip[0], tip[1]]});
      renderRigPanel();
      scheduleRigPreview(true);
    };
    row.append(reset, pin);
    rotation.append(row);
    if (R.report?.clamped?.includes(bone.id)) {
      rotation.append(element('small', '已按活动范围限制旋转，超出部分未应用', 'rig-warn'));
    }
  }
  panel.append(rotation);

  const rootRow = element('div', undefined, 'rig-targets');
  rootRow.append(element('b', '身体位移'));
  const pair = element('div', 'rig-pair');
  const rx = rigField('位移X', R.pose.root[0]);
  const ry = rigField('位移Y', R.pose.root[1]);
  const apply = () => {
    R.pose.root = [Number(rx.input.value), Number(ry.input.value)];
    scheduleRigPreview();
  };
  rx.input.onchange = apply;
  ry.input.onchange = apply;
  pair.append(rx, ry);
  const zero = element('button', '归零');
  zero.type = 'button';
  zero.onclick = () => { R.pose.root = [0, 0]; renderRigPanel(); scheduleRigPreview(true); };
  rootRow.append(pair, zero);
  panel.append(rootRow);

  const actions = element('div', undefined, 'rig-inline');
  const save = element('button', '保存到当前帧', 'primary');
  save.type = 'button';
  save.onclick = guard(async () => {
    const result = await work('保存角色姿态…', () => post(endpoint('rig/pose'), {revision: project().revision, frame_id: currentFrame().id, pose: rPosePayload()}));
    await acceptProject(result.project);
    R.project = rClone(result.project);
    R.report = result.report;
    renderRigPanel();
    drawRigCanvas();
    status('姿态已保存到当前帧，其他姿态帧不受影响');
  });
  const clear = element('button', '清除本帧姿态');
  clear.type = 'button';
  clear.onclick = guard(async () => {
    const result = await work('清除角色姿态…', () => post(endpoint('rig/clear'), {revision: project().revision, frame_id: currentFrame().id}));
    await acceptProject(result);
    R.project = rClone(result);
    R.pose = {rotations: {}, root: [0, 0], targets: []};
    R.state = await api(endpoint('rig'));
    await refreshRigPreview();
    status('已恢复本帧的绑定姿态');
  });
  actions.append(save, clear);
  panel.append(actions);

  const stashRow = element('div', undefined, 'rig-targets');
  const record = element('button', '记录当前姿态');
  record.type = 'button';
  record.onclick = () => {
    R.stash.push({name: `姿态 ${R.stash.length + 1}`, pose: rPoseClone()});
    renderRigPanel();
    status('已记录姿态，可在生成动作帧时作为关键姿态');
  };
  stashRow.append(element('b', '关键姿态列表'), record);
  R.stash.forEach((item, index) => {
    const row = element('div', undefined, 'rig-target');
    row.append(element('span', item.name));
    const load = element('button', '载入');
    load.type = 'button';
    load.onclick = () => {
      R.pose = rPoseClone(item.pose);
      renderRigPanel();
      scheduleRigPreview(true);
    };
    const drop = element('button', '删除');
    drop.type = 'button';
    drop.onclick = () => { R.stash.splice(index, 1); renderRigPanel(); };
    row.append(load, drop);
    stashRow.append(row);
  });
  const form = element('form', undefined, 'form rig-keyframes');
  form.append(element('p', '按列表顺序为每个姿态生成独立帧，可在姿态之间插入线性过渡；每帧保存可编辑图层与姿态数据。', 'hint'));
  form.append(rigField('帧名称前缀', '姿态', {type: 'text'}));
  form.append(rigField('姿态间过渡帧', 0, {min: 0, max: 30}));
  form.append(rigField('每帧时长 ms', currentFrame().duration, {min: 20, max: 10000}));
  const build = element('button', '生成动作帧', 'primary');
  build.type = 'submit';
  form.append(build, element('p', '', 'form-error'));
  form.onsubmit = guard(async event => {
    event.preventDefault();
    if (!R.stash.length) throw Error('请先记录至少一个关键姿态');
    const inputs = form.querySelectorAll('input');
    const result = await work('生成关键姿态帧…', () => post(endpoint('rig/keyframes'), {
      revision: project().revision,
      frame_id: currentFrame().id,
      poses: R.stash.map(item => item.pose),
      between: Number(inputs[1].value),
      duration: Number(inputs[2].value),
      name: inputs[0].value || '姿态',
    }));
    await acceptProject(result);
    R.project = rClone(result);
    R.stash = [];
    await openRig();
    status('已生成可编辑的动作帧');
  });
  stashRow.append(form);
  panel.append(stashRow);
}
