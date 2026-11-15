const $=id=>document.getElementById(id);
const S={project:null,frame:null,layer:null,projects:[],selected:new Set(),catalog:null,busy:false,points:[],drawing:false,play:false,playIndex:0,playStart:0,playSchedule:[],image:null,requestToken:0};
let jobTimer=null,previewURL=null,playRAF=null,mapTimer=null;
const names={frame_count:'帧数',total_duration:'总时长 ms',average_fps:'平均帧率',visible_pixels:'可见像素',coverage_percent:'覆盖率 %',partial_alpha_pixels:'半透明像素',visible_colors:'可见颜色数',touches_edge:'内容触及画布边界',empty:'完全透明',duration:'时长 ms',name:'名称',index:'序号',warnings:'预检提示',duplicate_groups:'重复帧组',frames:'动画帧',motion:'质心运动',alpha_centroid:'透明度质心',png_bytes:'PNG字节数',bounds:'可见包围框',mean_rgb:'平均RGB',source_duration:'源时长',target_duration:'目标时长',target_frames:'目标帧数',actual_fps:'实际帧率',colors:'颜色清单',percent:'使用比例 %',unique_colors:'独立颜色数',transparent_pixels:'全透明像素',opaque_pixels:'不透明像素',partial_pixels:'半透明像素',changed_pixels:'变化像素',changed_percent:'变化比例 %',mean_absolute_rgba_difference:'平均RGBA差异',occupancy_percent:'图集利用率 %',source_revision:'来源修订',valid:'检查通过',checked:'检查素材数',missing:'缺失素材',wrong_size:'尺寸错误',warning:'说明',note:'说明',collision:'发生碰撞',overlap_pixels:'重叠像素',overlap_bounds:'重叠包围框',page_count:'图集页数',sprite_count:'精灵数量',stored_assets:'保存的素材数',active_assets:'当前素材数',history_entries:'历史记录数'};
function label(key){return names[key]||key.replaceAll('_',' ')}
function element(tag,text,cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e}
function fmt(value){if(value===null||value===undefined)return '—';if(typeof value==='boolean')return value?'是':'否';if(typeof value==='number')return Number.isInteger(value)?String(value):Number(value.toPrecision(6)).toString();return String(value)}
function status(message,error=false){$('status').textContent=message;$('status').classList.toggle('error',error)}
async function api(path,options={}){const r=await fetch(path,options);const json=(r.headers.get('content-type')||'').includes('json');const data=json?await r.json():await r.blob();if(!r.ok)throw Error(data.error||`操作失败 (${r.status})`);return data}
function post(path,data={},method='POST'){return api(path,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})}
function project(){if(!S.project)throw Error('请先选择动画项目');return S.project}
function currentFrame(){const p=project();const frame=p.frames.find(f=>f.id===S.frame);if(!frame)throw Error('请先选择动画帧');return frame}
function flatLayerNodes(layers){return layers.flatMap(l=>[l,...(l.kind==='group'?flatLayerNodes(l.children):[])])}
function visibleLayerNodes(layers){S.collapsedGroups??=new Set();return layers.flatMap(l=>[l,...(l.kind==='group'&&!S.collapsedGroups.has(l.id)?visibleLayerNodes(l.children):[])])}
function currentLayer(){const frame=currentFrame();const layer=flatLayerNodes(frame.layers).find(l=>l.id===S.layer);if(!layer)throw Error('请先选择图层');return layer}
function endpoint(path=''){return `/api/projects/${project().id}`+(path?'/'+path:'')}
function framePath(path=''){return endpoint(`frames/${currentFrame().id}`)+(path?'/'+path:'')}
function layerPath(path=''){return framePath(`layers/${currentLayer().id}`)+(path?'/'+path:'')}
function ids(){const p=project();return S.selected.size?[...S.selected].filter(id=>p.frames.some(f=>f.id===id)):p.frames.map(f=>f.id)}
function guard(fn){return async(...args)=>{try{return await fn(...args)}catch(error){status(error.message,true);const box=$('dialogBody').querySelector('.form-error');if(box)box.textContent=error.message}}}
function stopPlayback(){S.play=false;if(playRAF)cancelAnimationFrame(playRAF);playRAF=null;$('play').textContent='▶ 播放';document.querySelectorAll('#layerInspector input,#layerInspector select,#layerInspector button').forEach(e=>e.disabled=false)}
async function work(message,fn){if(S.busy)throw Error('当前操作进行中，请稍候');stopPlayback();S.busy=true;document.body.classList.add('busy');status(message);try{return await fn()}finally{S.busy=false;document.body.classList.remove('busy')}}
async function mutate(message,path,data={},method='POST'){return work(message,async()=>{const result=await post(path,{...data,revision:project().revision},method);await acceptProject(result);status('已保存 · 可撤销');return result})}
function dialog(title){if(mapTimer){clearInterval(mapTimer);mapTimer=null}if(jobTimer){clearInterval(jobTimer);jobTimer=null}if(previewURL){URL.revokeObjectURL(previewURL);previewURL=null}$('dialogTitle').textContent=title;$('dialogBody').replaceChildren();if(!$('dialog').open)$('dialog').showModal();return $('dialogBody')}
$('closeDialog').onclick=()=>$('dialog').close();$('dialog').addEventListener('close',()=>{if(mapTimer){clearInterval(mapTimer);mapTimer=null}if(jobTimer)clearInterval(jobTimer);jobTimer=null});
function field(def,value){
  const wrapper=element('label',def.label);let input;
  if(def.type==='select'||def.type==='frame'){input=element('select');for(const o of (def.type==='frame'?frameChoices().map(c=>({value:c[0],label:c[1]})):def.options)){const option=element('option',o.label);option.value=o.value;input.append(option)}}
  else if(def.type==='textarea')input=element('textarea');
  else{input=element('input');input.type=['array','object'].includes(def.type)?'text':def.type||'text'}
  input.name=def.name;input.dataset.type=def.type;
  let initial=value===undefined?def.default:value;if(def.type==='frame'){const at=project().frames.findIndex(f=>f.id===S.frame);initial=def.default==='next'?project().frames[(at+1)%project().frames.length].id:currentFrame().id}
  if(def.type==='checkbox'){wrapper.className='check';input.checked=Boolean(initial);wrapper.prepend(input)}
  else{input.value=['array','object'].includes(def.type)?JSON.stringify(initial):initial??'';if(def.min!==null&&def.min!==undefined)input.min=def.min;if(def.max!==null&&def.max!==undefined)input.max=def.max;if(def.type==='number')input.step=def.step||'any';wrapper.append(input)}
  return wrapper;
}
function form(fields,title,callback){
  const f=element('form',undefined,'form');for(const def of fields)f.append(field(def));const submit=element('button',title,'primary');submit.type='submit';f.append(submit,element('p','','form-error'));
  f.onsubmit=guard(async e=>{e.preventDefault();const data={};const error=f.querySelector('.form-error');error.textContent='';for(const input of f.querySelectorAll('[name]')){const type=input.dataset.type;if(type==='checkbox')data[input.name]=input.checked;else if(type==='number'){if(input.value==='')throw Error('请输入 '+input.parentElement.textContent);data[input.name]=Number(input.value)}else if(type==='array'||type==='object'){try{data[input.name]=JSON.parse(input.value)}catch{throw Error(input.parentElement.textContent+'：JSON格式错误')}}else data[input.name]=input.value}submit.disabled=true;try{await callback(data,f)}catch(e){error.textContent=e.message;throw e}finally{submit.disabled=false}});return f;
}
function num(name,label,def,min,max){return {name,label,type:'number',default:def,min,max}}
function text(name,label,def='',type='text'){return {name,label,type,default:def}}
function arr(name,label,def){return text(name,label,def,'array')}
function select(name,label,def,choices){return {name,label,type:'select',default:def,options:choices.map(c=>({value:c[0],label:c[1]}))}}
function check(name,label,def=false){return {name,label,type:'checkbox',default:def}}
function frameChoices(){return project().frames.map((f,i)=>[f.id,`${i+1}. ${f.name}`])}
function renderData(data,target,depth=0){
  if(data===null||data===undefined){target.append(element('p','无结果','muted'));return}
  if(depth>5){target.append(element('p','深层明细请下载完整JSON','muted'));return}
  if(Array.isArray(data)){
    if(!data.length){target.append(element('p','无条目','muted'));return}
    if(data.every(v=>v&&typeof v==='object'&&!Array.isArray(v))){const keys=[...new Set(data.flatMap(Object.keys))].filter(k=>data.some(d=>d[k]===null||typeof d[k]!=='object')).slice(0,12);const wrap=element('div',undefined,'table-wrap'),table=element('table'),head=element('tr');keys.forEach(k=>head.append(element('th',label(k))));const thead=element('thead');thead.append(head);table.append(thead);const tbody=element('tbody');for(const row of data.slice(0,300)){const tr=element('tr');keys.forEach(k=>tr.append(element('td',fmt(row[k]))));tbody.append(tr)}table.append(tbody);wrap.append(table);target.append(wrap);return}
    if(data.every(v=>typeof v!=='object'||v===null)){target.append(element('p',data.length<80?data.map(fmt).join(', '):`数值序列 ${data.length} 项，可下载完整JSON`,'muted'));return}
    for(const item of data.slice(0,25)){const d=element('details');d.append(element('summary','条目'));renderData(item,d,depth+1);target.append(d)}return;
  }
  if(typeof data!=='object'){target.append(element('p',fmt(data)));return}
  const metrics=element('div',undefined,'metric-grid');const nested=[];
  for(const [key,value]of Object.entries(data)){if(value!==null&&typeof value==='object'){nested.push([key,value]);continue}if(['warning','note'].includes(key)){target.append(element('p',value,'hint'));continue}const card=element('div',undefined,'metric');card.append(element('small',label(key)),element('strong',fmt(value)));metrics.append(card)}if(metrics.childNodes.length)target.append(metrics);
  for(const [key,value]of nested){const d=element('details');d.open=['warnings','colors','pages'].includes(key);d.append(element('summary',label(key)));renderData(value,d,depth+1);target.append(d)}
}
function jsonDownload(data,name='result.json'){const link=element('a');const u=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));link.href=u;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(u),1000)}
function showData(title,data){const body=dialog(title);renderData(data,body);const b=element('button','下载完整JSON');b.onclick=()=>jsonDownload(data);body.append(b)}
function showBlob(blob,target,name='preview.png'){if(previewURL)URL.revokeObjectURL(previewURL);previewURL=URL.createObjectURL(blob);const image=element('img',undefined,'preview');image.src=previewURL;const a=element('a','↓ 下载图像');a.href=previewURL;a.download=name;target.append(image,element('p'),a)}
async function loadProjects(selectId=null){
  const query=new URLSearchParams({query:$('search').value,archived:$('archived').checked});S.projects=await api('/api/projects?'+query);$('projectCount').textContent=S.projects.length;$('projectList').replaceChildren();
  for(const p of S.projects){const card=element('div',undefined,'project-card');card.classList.toggle('active',S.project?.id===p.id);card.dataset.id=p.id;const img=element('img');img.src=`/api/projects/${p.id}/frames/${p.thumbnail_frame}/image?rev=${p.revision}`;img.alt=p.name;card.append(img,element('b',p.name),element('small',`${p.width}×${p.height} · ${p.frame_count} 帧 · ${p.duration}ms`));card.onclick=guard(()=>openProject(p.id));$('projectList').append(card)}
  if(selectId)await openProject(selectId);else if(!S.project&&S.projects.length)await openProject(S.projects[0].id);
}
async function openProject(id){if(S.busy)return;stopPlayback();const token=++S.requestToken;const data=await api('/api/projects/'+id);if(token!==S.requestToken)return;S.selected.clear();S.frame=null;S.layer=null;await acceptProject(data);status('已载入动画，直接在画布上绘制')}
async function acceptProject(p){
  S.project=p;const valid=new Set(p.frames.map(f=>f.id));S.selected=new Set([...S.selected].filter(id=>valid.has(id)));if(!p.frames.some(f=>f.id===S.frame))S.frame=p.frames[0].id;
  const frame=currentFrame();if(!flatLayerNodes(frame.layers).some(l=>l.id===S.layer))S.layer=frame.layers[frame.layers.length-1].id;
  S.points=[];S.drawing=false;$('projectName').textContent=p.name;$('projectInfo').textContent=`${p.width} × ${p.height} px · ${p.frames.length} 帧 · 修订 ${p.revision}`;
  $('timelineInfo').textContent=`${p.frames.length} 帧 / ${p.frames.reduce((a,f)=>a+f.duration,0)} ms · 已勾选 ${S.selected.size} 帧`;
  $('undo').disabled=!p.undo.length;$('redo').disabled=!p.redo.length;renderRoleChoices();renderPalette();renderFrames();renderLayers();loadCanvas();await refreshListOnly();
  const value=$('clipSelect').value;$('clipSelect').replaceChildren(new Option('完整时间轴',''));for(const clip of p.clips)$('clipSelect').append(new Option(clip.name,clip.id));if(p.clips.some(c=>c.id===value))$('clipSelect').value=value;
}
async function refreshListOnly(){
  const current=S.project;const query=new URLSearchParams({query:$('search').value,archived:$('archived').checked});S.projects=await api('/api/projects?'+query);$('projectCount').textContent=S.projects.length;$('projectList').replaceChildren();for(const p of S.projects){const card=element('div',undefined,'project-card');card.classList.toggle('active',current?.id===p.id);card.dataset.id=p.id;const img=element('img');img.src=`/api/projects/${p.id}/frames/${p.thumbnail_frame}/image?rev=${p.revision}`;img.alt=p.name;card.append(img,element('b',p.name),element('small',`${p.width}×${p.height} · ${p.frame_count}帧 · ${p.duration}ms`));card.onclick=guard(()=>openProject(p.id));$('projectList').append(card)}
}
function renderPalette(){$('swatches').replaceChildren();for(const color of project().palette){const b=element('button',undefined,'swatch');b.style.background=color.slice(0,7);b.title=color;b.setAttribute('aria-label','选择颜色 '+color);b.onclick=()=>$('color').value=color.slice(0,7);$('swatches').append(b)}}
function renderFrames(){
  $('frames').replaceChildren();project().frames.forEach((frame,index)=>{const card=element('div',undefined,'frame');card.classList.toggle('active',frame.id===S.frame);card.dataset.id=frame.id;const img=element('img');img.src=`/api/projects/${project().id}/frames/${frame.id}/image?rev=${project().revision}`;img.alt=frame.name;const check=element('input');check.type='checkbox';check.checked=S.selected.has(frame.id);check.setAttribute('aria-label','选择帧 '+(index+1));check.onclick=e=>e.stopPropagation();check.onchange=()=>{check.checked?S.selected.add(frame.id):S.selected.delete(frame.id);$('timelineInfo').textContent=`${project().frames.length} 帧 · 已勾选 ${S.selected.size} 帧`};const caption=element('small');caption.append(element('span',String(index+1).padStart(2,'0')),element('span',frame.duration+'ms'));card.append(img,check,caption);card.onclick=()=>{if(S.busy)return;stopPlayback();S.frame=frame.id;S.layer=frame.layers[frame.layers.length-1].id;S.points=[];renderFrames();renderLayers();loadCanvas()};$('frames').append(card)});
}
function renderLayers(){
  const frame=currentFrame();$('layerCount').textContent=frame.layers.length;$('layers').replaceChildren();for(const layer of visibleLayerNodes(frame.layers).slice().reverse()){const item=element('div',undefined,'layer');item.classList.toggle('active',layer.id===S.layer);const image=element('img');image.src=`/api/projects/${project().id}/frames/${frame.id}/layers/${layer.id}/image?rev=${project().revision}`;const name=element('span',(layer.kind==='group'?'▣ ':'')+layer.name);const mark=element('small',layer.locked?'锁定':Math.round(layer.opacity*100)+'%');const eye=element('button',layer.visible?'◉':'○');eye.title='切换图层显示';eye.onclick=guard(async e=>{e.stopPropagation();await mutate('保存图层显示…',`/api/projects/${project().id}/frames/${frame.id}/layers/${layer.id}`,{visible:!layer.visible},'PATCH')});item.append(image,name,mark,eye);if(layer.kind==='group'){const fold=element('button',S.collapsedGroups.has(layer.id)?'展开':'收起');fold.onclick=e=>{e.stopPropagation();S.collapsedGroups.has(layer.id)?S.collapsedGroups.delete(layer.id):S.collapsedGroups.add(layer.id);renderLayers()};const release=element('button','解组');release.onclick=guard(async e=>{e.stopPropagation();await mutate('解开图层组合…',`/api/projects/${project().id}/frames/${frame.id}/layers/${layer.id}/ungroup`)});item.append(fold,release)}item.onclick=()=>{if(S.busy)return;S.layer=layer.id;renderLayers()};$('layers').append(item)}
  const layer=currentLayer(),t=layer.transform;const target=$('layerInspector');target.replaceChildren();
  const fields=[text('name','图层名称',layer.name),num('opacity','不透明度 0–1',layer.opacity,0,1),select('blend','混合模式',layer.blend,[['normal','普通'],['multiply','正片叠底'],['screen','滤色'],['add','叠加亮度'],['difference','差值'],['overlay','叠加'],['darken','变暗'],['lighten','变亮']]),check('locked','锁定此图层',layer.locked),num('x','位置X',t.x,-4096,4096),num('y','位置Y',t.y,-4096,4096),num('scale_x','缩放X',t.scale_x,.01,8),num('scale_y','缩放Y',t.scale_y,.01,8),num('rotation','旋转°',t.rotation,-360,360),check('flip_x','左右翻转',t.flip_x),check('flip_y','上下翻转',t.flip_y)];
  target.append(form(fields,'保存图层属性',async data=>{const transform={};for(const k of ['x','y','scale_x','scale_y','rotation','flip_x','flip_y']){transform[k]=data[k];delete data[k]}await mutate('更新图层合成…',layerPath(),{...data,transform},'PATCH')}));
}
function loadCanvas(special=null){
  if(!S.project)return;const frame=currentFrame();$('frameName').textContent=`${frame.name} / ${frame.duration}ms`;$('canvas').hidden=false;$('empty').hidden=true;const image=new Image();const token=`${S.project.id}/${frame.id}/${S.project.revision}/${Boolean(special)}/${$('onion').checked}`;$('canvas').dataset.token=token;
  image.onload=()=>{if($('canvas').dataset.token!==token)return;S.image=image;$('canvas').width=project().width;$('canvas').height=project().height;drawCanvas()};image.onerror=()=>status('图像加载失败，请刷新项目',true);
  const onion=$('onion').checked?'&previous=1&following=1':'';image.src=special||`/api/projects/${project().id}/frames/${frame.id}/image?rev=${project().revision}${onion}`;
}
function drawCanvas(){
  if(!S.image)return;const canvas=$('canvas'),ctx=canvas.getContext('2d');ctx.clearRect(0,0,canvas.width,canvas.height);ctx.imageSmoothingEnabled=false;ctx.drawImage(S.image,0,0,canvas.width,canvas.height);const zoom=Number($('zoom').value);canvas.style.width=canvas.width*zoom+'px';canvas.style.height=canvas.height*zoom+'px';
  if($('grid').checked&&zoom>=4&&canvas.width<=256&&canvas.height<=256){ctx.strokeStyle='#b5d6ef30';ctx.lineWidth=1/zoom;for(let x=0;x<=canvas.width;x+=8){ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,canvas.height);ctx.stroke()}for(let y=0;y<=canvas.height;y+=8){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(canvas.width,y);ctx.stroke()}}
  if(S.points.length){ctx.strokeStyle=$('color').value;ctx.fillStyle=$('color').value;ctx.lineWidth=Math.max(1,Number($('brushSize').value));ctx.beginPath();S.points.forEach((p,i)=>i?ctx.lineTo(...p):ctx.moveTo(...p));ctx.stroke();for(const p of S.points)ctx.fillRect(Math.floor(p[0]),Math.floor(p[1]),1,1)}
}
for(const id of ['grid','zoom'])$(id).onchange=drawCanvas;$('onion').onchange=()=>loadCanvas();
function canvasPoint(e){const rect=$('canvas').getBoundingClientRect();return [Math.max(0,Math.min(project().width-1,Math.floor((e.clientX-rect.left)/rect.width*project().width))),Math.max(0,Math.min(project().height-1,Math.floor((e.clientY-rect.top)/rect.height*project().height)))]}
$('canvas').onpointerdown=guard(async e=>{
  if(S.busy||S.play)return;const layer=currentLayer();if(layer.locked)throw Error('当前图层已锁定');const mode=$('brush').value;const point=canvasPoint(e);
  if(mode==='eyedropper'){const result=await post(framePath('sample'),{point});$('color').value=result.hex.slice(0,7);status(`吸取颜色 ${result.hex}，最接近调色板 ${result.palette_match.nearest}`);return}
  if(mode==='polygon'){S.points.push(point);drawCanvas();return}
  S.points=[point];if(mode==='fill'||mode==='text'){await submitDrawing();return}S.drawing=true;$('canvas').setPointerCapture(e.pointerId);drawCanvas();
});
$('canvas').onpointermove=e=>{if(!S.project)return;const point=canvasPoint(e);$('coordinate').textContent=`${point[0]}, ${point[1]}`;if(!S.drawing||S.busy)return;const mode=$('brush').value;if(['pencil','erase'].includes(mode)){const last=S.points.at(-1);if(point[0]!==last[0]||point[1]!==last[1])S.points.push(point)}else S.points=[S.points[0],point];if(S.points.length>2500)S.points=S.points.filter((_,i)=>i%2===0);drawCanvas()};
$('canvas').onpointerup=guard(async e=>{if(!S.drawing)return;S.drawing=false;const point=canvasPoint(e);if(['line','rectangle','ellipse','gradient'].includes($('brush').value))S.points=[S.points[0],point];await submitDrawing()});
$('canvas').onpointercancel=()=>{S.drawing=false;S.points=[];drawCanvas()};
async function submitDrawing(){
  if(!S.points.length)return;const mode=$('brush').value;const data={tool:mode,color:$('color').value+Math.max(0,Math.min(255,Number($('celAlpha').value))).toString(16).padStart(2,'0'),secondary:$('secondary').value,size:Number($('brushSize').value),points:S.points.slice(),filled:$('filled').checked,mirror_x:$('mirrorX').checked,mirror_y:$('mirrorY').checked,tolerance:0,role_id:Number($('roleInk').value),ink_alpha:Number($('roleAlpha').value)};
  if(mode==='text'){const body=dialog('绘制文字');body.append(form([text('text','文字内容','HELLO'),num('font_size','字号',12,6,256)],'绘制到当前图层',async values=>{await mutate('绘制文字…',layerPath('draw'),{...data,...values});$('dialog').close()}));return}
  await mutate('保存像素笔画…',layerPath('draw'),data);
}
$('finishPolygon').onclick=guard(submitDrawing);$('brush').onchange=()=>{S.points=[];S.drawing=false;drawCanvas()};
$('newProject').onclick=()=>{const body=dialog('新建像素动画');body.append(form([text('name','动画名称','我的像素动画'),num('width','画布宽度',64,8,2048),num('height','画布高度',64,8,2048)],'创建画布',async data=>{const p=await work('创建动画…',()=>post('/api/projects',data));S.frame=S.layer=null;await acceptProject(p);$('dialog').close()}))};
$('demo').onclick=guard(async()=>{const p=await work('创建原创演示…',()=>post('/api/demo',{kind:$('demoKind').value}));S.frame=S.layer=null;S.selected.clear();await acceptProject(p);$('dialog').close();status('演示已就绪，点击播放或直接绘制像素')});
let searchTimer;$('search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(guard(()=>refreshListOnly()),250)};$('archived').onchange=guard(()=>refreshListOnly());
$('undo').onclick=guard(()=>mutate('撤销操作…',endpoint('undo')));$('redo').onclick=guard(()=>mutate('重做操作…',endpoint('redo')));
$('projectSettings').onclick=guard(()=>{const p=project();const body=dialog('项目设置');body.append(form([text('name','动画名称',p.name),text('tags','标签（逗号分隔）',p.tags.join(', ')),text('notes','项目说明',p.notes,'textarea'),text('background','背景颜色 #RRGGBBAA',p.background),num('loop','循环次数（0无限循环）',p.loop,0,65535),check('archived','归档项目',p.archived)],'保存项目设置',async data=>{data.tags=data.tags.split(/[,，]/).map(v=>v.trim()).filter(Boolean);await mutate('保存项目设置…',endpoint(),data,'PATCH');$('dialog').close()}));const b=element('button','复制为独立动画项目');b.onclick=guard(async()=>{const result=await work('复制项目…',()=>post(endpoint('duplicate')));S.frame=S.layer=null;await acceptProject(result);$('dialog').close()});body.append(b)});
$('addColor').onclick=guard(async()=>{const palette=[...new Set([...project().palette,$('color').value+'ff'])];await mutate('保存调色板颜色…',endpoint(),{palette},'PATCH')});
function goFrame(offset){if(S.busy||!S.project)return;stopPlayback();const index=project().frames.findIndex(f=>f.id===S.frame);const next=(index+offset+project().frames.length)%project().frames.length;S.frame=project().frames[next].id;S.layer=project().frames[next].layers.at(-1).id;renderFrames();renderLayers();loadCanvas()}
$('previous').onclick=()=>goFrame(-1);$('next').onclick=()=>goFrame(1);
$('play').onclick=guard(async()=>{
  if(S.play){stopPlayback();renderFrames();renderLayers();loadCanvas();return}project();if(S.busy)throw Error('请等待当前操作结束');const suffix=$('clipSelect').value?'?clip_id='+$('clipSelect').value:'';const schedule=await api(endpoint('schedule')+suffix);S.playSchedule=schedule.frames;S.playStart=performance.now();S.play=true;document.querySelectorAll('#layerInspector input,#layerInspector select,#layerInspector button').forEach(e=>e.disabled=true);$('play').textContent='Ⅱ 暂停';let shown=null;
  function tick(now){if(!S.play)return;if(schedule.loop>0&&now-S.playStart>=schedule.total_duration*(schedule.loop+1)){stopPlayback();renderFrames();renderLayers();loadCanvas();return}const time=(now-S.playStart)%schedule.total_duration;$('playTime').textContent=Math.floor(time)+' ms';const item=S.playSchedule.find(f=>time>=f.start&&time<f.end)||S.playSchedule[0];if(item.id!==shown){shown=item.id;S.frame=item.id;const frame=currentFrame();S.layer=frame.layers.at(-1).id;loadCanvas();document.querySelectorAll('.frame').forEach(el=>el.classList.toggle('active',el.dataset.id===shown))}playRAF=requestAnimationFrame(tick)}playRAF=requestAnimationFrame(tick);
});
$('clipSelect').onchange=()=>stopPlayback();
$('selectAll').onclick=guard(()=>{S.selected=new Set(project().frames.map(f=>f.id));renderFrames();$('timelineInfo').textContent=`已勾选 ${S.selected.size} 帧`});$('clearSelected').onclick=()=>{S.selected.clear();renderFrames();$('timelineInfo').textContent='未勾选，工具默认作用于全部帧'};
$('addFrame').onclick=guard(()=>mutate('新增空白帧…',endpoint('frames'),{after:currentFrame().id,count:1}));
$('duplicateFrame').onclick=guard(()=>mutate('复制当前帧…',endpoint('frames'),{after:currentFrame().id,source:currentFrame().id,count:1}));
$('removeFrames').onclick=guard(()=>{if(!S.selected.size)throw Error('请先勾选要移除的帧');return mutate('移除所选帧…',endpoint('frames/remove'),{frame_ids:[...S.selected]})});
for(const [id,offset]of [['frameLeft',-1],['frameRight',1]])$(id).onclick=guard(()=>{const list=project().frames.map(f=>f.id),index=list.indexOf(currentFrame().id),next=index+offset;if(next<0||next>=list.length)return;[list[index],list[next]]=[list[next],list[index]];return mutate('调整帧序…',endpoint('frames/reorder'),{frame_ids:list})});
$('frameProperties').onclick=guard(()=>{const frame=currentFrame();const body=dialog('帧时长、枢轴与碰撞框');body.append(form([text('name','帧名称',frame.name),num('duration','帧时长毫秒',frame.duration,project().animation_timing==='exact_composition'?1:20,10000),arr('pivot','归一化枢轴 [x,y]',frame.pivot),arr('boxes','碰撞框 [{name,x,y,width,height},…]',frame.boxes),text('notes','帧说明',frame.notes,'textarea')],'保存帧属性',async data=>{await mutate('保存帧属性…',framePath(),data,'PATCH');$('dialog').close()}))});
$('createClip').onclick=guard(()=>{const body=dialog('保存动画片段');body.append(element('p',`使用当前勾选的 ${ids().length} 帧，未勾选时使用全部帧。`,'hint'));body.append(form([text('name','片段名称','新的动画动作'),select('direction','播放方向','forward',[['forward','正向'],['reverse','反向'],['pingpong','往返']]),num('repeat','重复次数（0无限）',0,0,65535)],'保存动画片段',async data=>{await mutate('保存片段…',endpoint('clips'),{...data,frame_ids:ids()});$('dialog').close()}))});
$('addLayer').onclick=guard(()=>{const body=dialog('添加图层');body.append(form([text('name','图层名称','新绘制层'),text('color','初始填充颜色（00000000透明）','#00000000')],'添加到当前帧',async data=>{await mutate('添加图层…',framePath('layers'),data);S.layer=currentFrame().layers.at(-1).id;renderLayers();$('dialog').close()}))});
$('copyLayer').onclick=guard(()=>mutate('复制图层…',layerPath('duplicate')));$('removeLayer').onclick=guard(()=>mutate('移除图层…',layerPath(),{},'DELETE'));$('mergeLayer').onclick=guard(()=>mutate('合并到下方图层…',layerPath('merge')));$('bakeLayer').onclick=guard(()=>mutate('烘焙图层变换…',layerPath('bake')));$('clearLayer').onclick=guard(()=>mutate('清空当前图层…',layerPath('clear')));
for(const [id,delta]of [['layerUp',1],['layerDown',-1]])$(id).onclick=guard(()=>{const list=currentFrame().layers.map(l=>l.id),index=list.indexOf(currentLayer().id),next=index+delta;if(next<0||next>=list.length)return;[list[index],list[next]]=[list[next],list[index]];return mutate('调整图层顺序…',framePath('layers/reorder'),{layer_ids:list})});
$('selectionMove').onclick=guard(()=>{project();const body=dialog('矩形选区复制与移动');body.append(form([num('x','选区左上X',0,0),num('y','选区左上Y',0,0),num('width','选区宽度',Math.min(24,project().width),1),num('height','选区高度',Math.min(24,project().height),1),num('dx','偏移X',4,-2048,2048),num('dy','偏移Y',0,-2048,2048),check('copy','复制而非剪切')],'应用选区',async data=>{const {x,y,width,height,...rest}=data;await mutate('移动选区…',layerPath('selection'),{...rest,selection:{type:'rectangle',x,y,width,height}});$('dialog').close()}))});
$('propagate').onclick=guard(()=>{const body=dialog('跨帧复制当前图层');body.append(element('p','目标为勾选帧；未勾选时使用全部帧。源帧跳过。','hint'));body.append(form([select('mode','复制方式','append',[['append','追加图层'],['replace_name','替换唯一同名图层']])],'复制到目标帧',async data=>{await mutate('复制图层到其他帧…',layerPath('propagate'),{...data,frame_ids:ids()});$('dialog').close()}))});
$('stamp').onclick=guard(()=>{const body=dialog('使用动画帧作为图像印章');body.append(form([select('source_frame','来源帧',currentFrame().id,frameChoices()),num('x','印章左上X',0,-2048,2048),num('y','印章左上Y',0,-2048,2048),num('scale','印章倍率',.5,.05,4),num('rotation','旋转角度',0,-360,360),num('opacity','不透明度',1,0,1)],'绘制图像印章',async data=>{await mutate('应用帧印章…',layerPath('stamp'),data);$('dialog').close()}))});
$('collisionPreview').onclick=guard(()=>{loadCanvas(framePath('collision-preview')+'?rev='+project().revision);status('红框为碰撞框，黄色十字为枢轴；绘制或切帧后返回普通预览')});
function toolData(values){return {...values,revision:project().revision,frame_id:currentFrame().id,layer_id:currentLayer().id,frame_ids:ids()}}
$('toolbox').onclick=()=>openToolbox();
function openToolbox(group='全部'){
  const body=dialog('动画与图像工具箱'),nav=element('div',undefined,'group-tabs');for(const category of ['全部',...new Set(S.catalog.tools.map(t=>t.group))]){const b=element('button',category);if(category===group)b.className='primary';b.onclick=()=>openToolbox(category);nav.append(b)}const recipeButton=element('button','像素效果配方');recipeButton.onclick=guard(openRecipes);nav.append(recipeButton);body.append(nav);const grid=element('div',undefined,'tool-list');for(const tool of S.catalog.tools.filter(t=>group==='全部'||t.group===group)){const b=element('button',undefined,'tool-card');b.append(element('small',tool.group),element('b',tool.title),element('p',tool.note));b.onclick=guard(()=>openTool(tool.key));grid.append(b)}body.append(grid);
}
function openTool(key){
  project();const tool=S.catalog.tools.find(t=>t.key===key);const body=dialog(tool.title),layout=element('div',undefined,'dialog-grid'),controls=element('div'),output=element('div',undefined,'tool-output');controls.append(element('p',tool.note,'hint'));controls.append(element('p',`当前第${project().frames.findIndex(f=>f.id===S.frame)+1}帧；目标 ${ids().length} 帧。`,'muted'));
  controls.append(form(tool.fields,'运行 '+tool.title,async values=>{
    output.replaceChildren(element('p','正在处理…','muted'));const path=endpoint(tool.path.replace('{frame}',currentFrame().id)),data=toolData(values);if(tool.key==='collision')data.image=true;let result;
    await work('正在执行 '+tool.title+'…',async()=>{result=tool.method==='GET'?await api(path):await post(path,data);if(tool.output==='project')await acceptProject(result)});output.replaceChildren();
    if(tool.output==='project'){renderData({frame_count:result.frames.length,revision:result.revision},output);output.append(element('p','变更已保存，可关闭窗口查看或撤销。','hint'))}
    else if(tool.output==='new_project'||tool.output==='sampled_project'){const p=tool.output==='sampled_project'?result.project:result;renderData(tool.output==='sampled_project'?result.sampling:{name:p.name,width:p.width,height:p.height},output);const b=element('button','打开生成的新项目');b.onclick=guard(async()=>{S.frame=S.layer=null;await acceptProject(p);$('dialog').close()});output.append(b);await refreshListOnly()}
    else if(tool.output==='image')showBlob(result,output,tool.key+'.png');
    else{renderData(result,output);const b=element('button','下载结果JSON');b.onclick=()=>jsonDownload(result,tool.key+'.json');output.append(b)}status(tool.title+'完成');
  }));layout.append(controls,output);body.append(layout);if(tool.method==='GET')controls.querySelector('form').requestSubmit();
}
$('diagnostics').onclick=guard(()=>openTool('analysis'));
$('paletteTools').onclick=guard(async()=>{
  project();const body=dialog('共享调色板工坊'),layout=element('div',undefined,'dialog-grid'),controls=element('div'),output=element('div',undefined,'tool-output');let palette=project().palette.slice();
  function showPalette(){output.replaceChildren();const swatches=element('div',undefined,'palette-result');for(const color of palette){const b=element('button',undefined,'swatch');b.title=color;b.style.background=color.slice(0,7);b.onclick=()=>$('color').value=color.slice(0,7);swatches.append(b)}output.append(swatches);const editor=element('textarea');editor.value=JSON.stringify(palette);editor.style.width='100%';output.append(editor);const save=element('button','保存调色板（不改像素）');save.onclick=guard(async()=>{palette=JSON.parse(editor.value);await mutate('保存共享调色板…',endpoint(),{palette},'PATCH');showPalette()});const apply=element('button','将调色板映射到目标帧');apply.onclick=guard(async()=>{palette=JSON.parse(editor.value);await mutate('应用调色板减色…',endpoint('palette/apply'),{palette,dither:controls.querySelector('[name="dither"]').value,frame_ids:ids()});status('调色板已应用，像素已更新')});output.append(element('p'),save,element('p'),apply)}
  controls.append(form([num('count','提取颜色数',16,2,64),select('method','颜色提取算法','median',[['median','中位切分'],['coverage','最大覆盖']]),select('dither','应用时的抖动方式','none',[['none','不抖动'],['ordered','有序抖动'],['floyd','Floyd误差扩散']])],'从选中帧提取调色板',async data=>{const result=await work('提取共享颜色…',()=>post(endpoint('palette/extract'),{...data,frame_ids:ids()}));palette=result.palette;showPalette();renderData(result.inventory,output)}));
  controls.append(form([text('first','渐变起色','#152437ff'),text('second','渐变终色','#66e2bdff'),num('count','色阶数量',8,2,64),select('space','色彩空间','rgb',[['rgb','RGB'],['hsv','HSV最短色相']])],'生成渐变色阶',async data=>{palette=(await post('/api/palette/ramp',data)).palette;showPalette()}));const cycle=element('button','从当前帧生成颜色循环');cycle.onclick=guard(async()=>{await mutate('生成调色板循环…',endpoint('palette/cycle'),{frame_id:currentFrame().id,count:Math.min(project().palette.length,16)});$('dialog').close()});controls.append(cycle);layout.append(controls,output);body.append(layout);showPalette();
});
$('imports').onclick=guard(()=>{
  const body=dialog('图像、动画与精灵素材导入'),grid=element('div',undefined,'import-grid');
  function panel(title,note,fields,onFiles,multiple=false){const card=element('div',undefined,'import-panel');card.append(element('h3',title),element('p',note,'muted'));const file=element('input');file.type='file';file.multiple=multiple;card.append(file);const f=form(fields,'开始导入',async data=>{if(!file.files.length)throw Error('请先选择文件');await onFiles([...file.files],data)});card.append(f);grid.append(card)}
  panel('新建动画项目','PNG/APNG/GIF/WebP图像自动解码帧与时长。',[],async files=>{const f=new FormData();f.append('file',files[0]);const p=await work('解码动画…',()=>api('/api/import',{method:'POST',body:f}));S.frame=S.layer=null;await acceptProject(p);$('dialog').close()});
  panel('追加到当前项目','可选多个文件，支持追加为帧或添加为当前帧图层。',[select('mode','导入方式','append',[['append','追加动画帧'],['layers','添加为图层']]),select('fit','尺寸适配','center',[['center','原尺寸居中'],['contain','完整适应'],['stretch','拉伸到画布']])],async(files,data)=>{project();const f=new FormData();for(const file of files)f.append('files',file);for(const [k,val]of Object.entries({...data,revision:project().revision,frame_id:currentFrame().id}))f.append(k,val);const p=await work('导入到当前项目…',()=>api(endpoint('import'),{method:'POST',body:f}));await acceptProject(p);$('dialog').close()},true);
  panel('规则精灵表切片','按单元格尺寸、留白和间隔拆分到当前项目。',[num('cell_width','格宽',32,8,2048),num('cell_height','格高',32,8,2048),num('spacing','间隔',0,0,100),num('margin','边距',0,0,500),check('skip_empty','跳过完全透明单元格',true)],async(files,data)=>{project();const f=new FormData();f.append('file',files[0]);f.append('parameters',JSON.stringify({...data,revision:project().revision}));const p=await work('切割精灵表…',()=>api(endpoint('slice'),{method:'POST',body:f}));await acceptProject(p);$('dialog').close()});
  panel('透明图自动切片','按透明沟槽或连通组件识别精灵，另建统一画布项目。',[select('mode','切片依据','gutters',[['gutters','透明沟槽'],['components','连通组件']]),num('threshold','Alpha阈值',1,1,255),num('minimum','最小像素数',4,1),num('gap','容许内部透明间隔',0,0,32),num('padding','切片留白',0,0,32)],async(files,data)=>{const f=new FormData();f.append('file',files[0]);f.append('parameters',JSON.stringify(data));const result=await work('自动拆分精灵…',()=>api('/api/auto-slice',{method:'POST',body:f}));S.frame=S.layer=null;await acceptProject(result.project);$('dialog').close()});
  panel('恢复项目ZIP','保留可编辑图层、帧时长、片段、调色板及地图。',[],async files=>{const f=new FormData();f.append('file',files[0]);const p=await work('恢复项目归档…',()=>api('/api/import-bundle',{method:'POST',body:f}));S.frame=S.layer=null;await acceptProject(p);$('dialog').close()});
  const atlas=element('div',undefined,'import-panel');atlas.append(element('h3','恢复裁边精灵图集'),element('p','选择本工作台导出的atlas.png与atlas.json，恢复透明画布、旋转和锚点。','muted'));const image=element('input'),metadata=element('input');image.type=metadata.type='file';image.accept='image/png';metadata.accept='.json';atlas.append(element('label','图集PNG'),image,element('label','元数据JSON'),metadata);const button=element('button','恢复精灵图集');button.onclick=guard(async()=>{if(!image.files[0]||!metadata.files[0])throw Error('请选择图集图像与元数据');const f=new FormData();f.append('image',image.files[0]);f.append('metadata',metadata.files[0]);const p=await work('恢复图集…',()=>api('/api/import-atlas',{method:'POST',body:f}));S.frame=S.layer=null;await acceptProject(p);$('dialog').close()});atlas.append(button);grid.append(atlas);body.append(grid);
});
$('export').onclick=guard(openExport);
async function openExport(){
  project();const body=dialog('导出动画与游戏素材'),layout=element('div',undefined,'dialog-grid'),controls=element('div'),output=element('div',undefined,'tool-output');
  const fields=[select('format','导出格式','apng',[['apng','APNG透明动画'],['gif','GIF动画'],['webp','无损WebP动画'],['sequence','PNG序列与时间轴'],['atlas','紧凑精灵图集'],['multipage','多页精灵图集'],['sheet','规则序列图'],['project','可编辑项目归档'],['palette','调色板文件包']]),num('scale','动画/序列输出倍率',1,1,8),num('columns','规则序列图列数',6,1,64),num('padding','图集间隔',1,0,16),num('extrude','图集边缘外扩',1,0,8),num('atlas_width','单页图集宽（0为自动）',0,0,4096),num('atlas_height','单页图集高（0为自动）',0,0,4096),num('page_width','多页图集每页宽',256,32,4096),num('page_height','多页图集每页高',256,32,4096),check('trim','裁去透明边缘',true),check('rotate','允许图集旋转'),check('background','使用项目背景颜色')];
  controls.append(element('p','默认使用勾选帧，未勾选时导出全部。编码绑定当前项目修订，后续编辑不会改变已排队作品。GIF会使用二值透明与10ms时长。','hint'));controls.append(form(fields,'创建导出任务',async data=>{if(data.format==='atlas'&&(data.atlas_width||data.atlas_height)){if(!data.atlas_width||!data.atlas_height)throw Error('单页图集宽高需同时指定');data.size=[data.atlas_width,data.atlas_height]}delete data.atlas_width;delete data.atlas_height;const job=await post(endpoint('jobs'),{...data,frame_ids:ids(),clip_id:$('clipSelect').value||undefined,revision:project().revision});status('已创建导出任务 '+job.format);await renderJobs(output);if(!jobTimer)jobTimer=setInterval(()=>renderJobs(output).catch(e=>status(e.message,true)),2000)}));
  const atlas=element('button','预览紧凑图集');atlas.onclick=guard(async()=>{const blob=await work('生成图集预览…',()=>post(endpoint('atlas-preview'),{frame_ids:ids(),trim:true,padding:1,extrude:1,rotate:false}));const target=dialog('图集预览');showBlob(blob,target,'atlas-preview.png')});controls.append(atlas);
  const report=element('a','打开可打印制作报告');report.href=endpoint('report');report.target='_blank';report.rel='noopener';const csv=element('a','下载时间轴CSV');csv.href=endpoint('timeline.csv');const collision=element('a','下载枢轴与碰撞元数据JSON');collision.href=endpoint('collision-metadata');collision.download='collision-metadata.json';controls.append(element('p'),report,element('p'),csv,element('p'),collision);layout.append(controls,output);body.append(layout);await renderJobs(output);jobTimer=setInterval(()=>renderJobs(output).catch(e=>status(e.message,true)),2000);
}
async function renderJobs(output){
  if(!$('dialog').open)return;const jobs=await api(endpoint('jobs'));output.replaceChildren(element('h3','导出任务'));
  for(const job of jobs.slice(0,20)){const card=element('div',undefined,'job');card.append(element('h3',`${job.format.toUpperCase()} · ${job.status}`),element('p',`来源修订 ${job.source_revision} · ${job.stage}`));if(job.status==='completed'){const a=element('a',`↓ ${job.filename} (${Math.ceil(job.bytes/1024)} KB)`);a.href=`/api/jobs/${job.id}/download`;card.append(a);if(job.details?.warning)card.append(element('p',job.details.warning,'muted'));if(['apng','gif','webp'].includes(job.format)){const img=element('img',undefined,'preview');img.src=`/api/jobs/${job.id}/download`;card.append(element('p'),img)}}else if(['queued','running','cancelling'].includes(job.status)){const cancel=element('button','取消导出');cancel.onclick=guard(async()=>{await post(`/api/jobs/${job.id}/cancel`);await renderJobs(output)});card.append(cancel)}else{card.append(element('p',job.error||'任务未完成','form-error'));const retry=element('button','按当前项目重试');retry.onclick=guard(async()=>{await post(`/api/jobs/${job.id}/retry`);await renderJobs(output)});card.append(retry)}output.append(card)}if(!jobs.length)output.append(element('p','暂无导出任务','muted'));if(jobTimer&&!jobs.some(j=>['queued','running','cancelling'].includes(j.status))){clearInterval(jobTimer);jobTimer=null}
}
$('snapshots').onclick=guard(openSnapshots);
async function openSnapshots(){
  project();const body=dialog('命名关键版本与历史');body.append(form([text('name','版本名称','草稿版本'),text('description','版本说明','','textarea')],'保存当前版本',async data=>{await post(endpoint('snapshots'),{...data,revision:project().revision});await openSnapshots();status('关键版本已保存')}));
  const snapshots=await api(endpoint('snapshots'));const selected=[];for(const snapshot of snapshots){const card=element('div',undefined,'version-card');card.append(element('h3',snapshot.name),element('p',`${snapshot.frames} 帧 · ${snapshot.width}×${snapshot.height} · 修订${snapshot.source_revision} · ${snapshot.created_at}`,'muted'));const c=element('input');c.type='checkbox';c.onchange=()=>{c.checked?selected.push(snapshot.id):selected.splice(selected.indexOf(snapshot.id),1)};const l=element('label',' 对比');l.prepend(c);card.append(l);const restore=element('button','恢复（可撤销）');restore.onclick=guard(async()=>{await mutate('恢复关键版本…',endpoint(`snapshots/${snapshot.id}/restore`));$('dialog').close()});const fork=element('button','另建分支项目');fork.onclick=guard(async()=>{const p=await post(endpoint(`snapshots/${snapshot.id}/fork`));S.frame=S.layer=null;await acceptProject(p);$('dialog').close()});card.append(element('p'),restore,fork);body.append(card)}const compare=element('button','对比勾选的两个版本');compare.onclick=guard(async()=>{if(selected.length!==2)throw Error('请勾选两个关键版本');showData('关键版本差异',await post(endpoint('snapshots/compare'),{first:selected[0],second:selected[1]}))});body.append(compare);const history=await api(endpoint('history'));const details=element('details');details.append(element('summary','近期操作历史'));renderData(history,details);body.append(details);
}
$('maps').onclick=guard(openMaps);
async function openMaps(mapId=null){
  project();const body=dialog('瓦片地图编辑器');if(!project().tilemaps.length){body.append(element('p','将当前选中帧作为瓦片集合，可创建关卡草图并导出Tiled JSON与瓦片PNG。未选时使用全部帧。','hint'));body.append(form([text('name','地图名称','我的关卡'),num('columns','地图列数',12,1,100),num('rows','地图行数',8,1,100),num('tile_width','瓦片宽度',Math.min(32,project().width),8,256),num('tile_height','瓦片高度',Math.min(32,project().height),8,256)],'创建地图',async data=>{await mutate('创建瓦片地图…',endpoint('maps'),{...data,tiles:ids()});await openMaps()}));return}
  let map=project().tilemaps.find(m=>m.id===mapId)||project().tilemaps[0];const layout=element('div',undefined,'map-layout'),controls=element('div'),right=element('div');const chooser=element('select');for(const item of project().tilemaps)chooser.append(new Option(item.name,item.id));chooser.value=map.id;chooser.onchange=()=>openMaps(chooser.value);controls.append(chooser);let tile=1,layerId=map.layers[0].id;const layerSelect=element('select');for(const layer of map.layers)layerSelect.append(new Option(layer.name,layer.id));layerSelect.onchange=()=>layerId=layerSelect.value;controls.append(element('p','当前地图图层','muted'),layerSelect,element('p','点击瓦片，再点击地图格子绘制。','hint'));
  const tiles=element('div',undefined,'map-tiles');const eraser=element('button','空');eraser.onclick=()=>{tile=0;tiles.querySelectorAll('button').forEach(b=>b.classList.remove('active'));eraser.classList.add('active')};tiles.append(eraser);map.tiles.forEach((id,index)=>{const b=element('button',undefined,index===0?'active':'');const img=element('img');img.src=endpoint(`frames/${id}/image`)+`?rev=${project().revision}`;b.title='瓦片 '+(index+1);b.append(img);b.onclick=()=>{tile=index+1;tiles.querySelectorAll('button').forEach(e=>e.classList.toggle('active',e===b))};tiles.append(b)});controls.append(tiles);const flipX=element('input'),flipY=element('input');flipX.type=flipY.type='checkbox';const fx=element('label',' 水平翻转','check'),fy=element('label',' 垂直翻转','check');fx.prepend(flipX);fy.prepend(flipY);controls.append(fx,fy);
  const phaseField=field(num('phase_ms','本笔实例相位 ms',0,0,3000000)),phaseInput=phaseField.querySelector('input');controls.append(phaseField);
  controls.append(element('p','动态瓦片使用无限循环动作片段，每张地图最多64种瓦片与相位组合；相位按毫秒存储，图像文件可复用。','hint'));
  controls.append(form([num('tile','动态瓦片编号',1,1,map.tiles.length),select('clip_id','循环动作片段','',[['','移除该瓦片的动画'],...project().clips.map(c=>[c.id,c.name])])],'保存动态瓦片',async data=>{await mutate('保存动态瓦片…',endpoint(`maps/${map.id}/animation`),data);await openMaps(map.id)}));
  const timeField=field(num('map_time','地图预览时间 ms',0,0,1000000000000)),timeInput=timeField.querySelector('input'),exportToggle=element('input'),exportLabel=element('label',' 查看导出资源预览','check');exportToggle.type='checkbox';exportToggle.id='mapExportPreview';exportLabel.prepend(exportToggle);const playMap=element('button','播放地图'),modeLabel=element('p','正在编辑实时地图','hint');right.append(timeField,exportLabel,playMap,modeLabel);timeInput.onchange=()=>refresh();exportToggle.onchange=()=>{modeLabel.textContent=exportToggle.checked?'导出资源预览（只读）':'正在编辑实时地图';refresh()};playMap.onclick=()=>{if(mapTimer){clearInterval(mapTimer);mapTimer=null;playMap.textContent='播放地图';return}const base=Number(timeInput.value),start=performance.now();playMap.textContent='暂停地图';mapTimer=setInterval(()=>{timeInput.value=String(base+Math.floor(performance.now()-start));refresh()},150)};
  const mapCanvas=element('canvas');mapCanvas.id='mapCanvas';const view=element('div',undefined,'map-viewport');view.append(mapCanvas);right.append(view);const ctx=mapCanvas.getContext('2d');let renderToken=0;async function refresh(){const token=++renderToken;map=project().tilemaps.find(m=>m.id===map.id);const image=new Image();image.onload=()=>{if(token!==renderToken)return;mapCanvas.width=image.naturalWidth;mapCanvas.height=image.naturalHeight;ctx.imageSmoothingEnabled=false;ctx.clearRect(0,0,mapCanvas.width,mapCanvas.height);ctx.drawImage(image,0,0);ctx.strokeStyle='#e0edff35';for(let x=0;x<mapCanvas.width;x+=map.tile_width){ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,mapCanvas.height);ctx.stroke()}for(let y=0;y<mapCanvas.height;y+=map.tile_height){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(mapCanvas.width,y);ctx.stroke()}};image.src=endpoint(`maps/${map.id}/image`)+`?rev=${project().revision}&time_ms=${Number(timeInput.value)}&export_preview=${exportToggle.checked}`}
  mapCanvas.onclick=guard(async e=>{if(exportToggle.checked)throw Error('导出资源预览只读，请切回实时地图绘制');const rect=mapCanvas.getBoundingClientRect();const x=Math.floor((e.clientX-rect.left)/rect.width*map.columns),y=Math.floor((e.clientY-rect.top)/rect.height*map.rows);await mutate('保存地图格子…',endpoint(`maps/${map.id}/paint`),{layer_id:layerId,tile,cells:[[x,y]],phase_ms:Number(phaseInput.value),flip_x:flipX.checked,flip_y:flipY.checked});await refresh()});
  controls.append(form([select('mode','填充模式','checker',[['solid','纯色瓦片'],['checker','棋盘'],['random','固定种子随机'],['border','边框']]),num('first','第一瓦片编号',1,0,map.tiles.length),num('second','第二瓦片编号',Math.min(2,map.tiles.length),0,map.tiles.length),num('seed','随机种子',42,0,1000000)],'填充地图第一层',async data=>{await mutate('填充地图…',endpoint(`maps/${map.id}/fill`),data);await refresh()}));
  const add=element('button','＋ 地图图层');add.onclick=guard(async()=>{await mutate('添加地图图层…',endpoint(`maps/${map.id}/layers`),{name:'新地图层'});await openMaps(map.id)});const del=element('button','移除此地图（可撤销）');del.onclick=guard(async()=>{await mutate('移除地图…',endpoint(`maps/${map.id}`),{},'DELETE');await openMaps()});controls.append(add,element('p'),del);const exportLink=element('a','↓ 下载地图与瓦片ZIP');exportLink.href=endpoint(`maps/${map.id}/export`);right.append(element('p'),exportLink);layout.append(controls,right);body.append(layout);await refresh();
}
async function init(){S.catalog=await api('/api/catalog');await loadProjects()}
init().catch(error=>status(error.message,true));

async function openRecipes(){
  project();const recipes=await api('/api/recipes');const body=dialog('可复用像素效果配方'),layout=element('div',undefined,'dialog-grid'),controls=element('div'),output=element('div',undefined,'tool-output');
  controls.append(form([select('recipe_id','保存的效果配方',recipes[0]?.id,recipes.map(r=>[r.id,r.name]))],'应用到目标帧',async data=>{await mutate('应用效果配方…',endpoint(`recipes/${data.recipe_id}/apply`),{frame_ids:ids()});status('配方已应用，像素变更可撤销')}));
  controls.append(form([text('name','新配方名称','我的像素效果'),text('description','用途说明','','textarea'),arr('steps','效果步骤JSON',[{operation:'posterize',levels:4},{operation:'outline',radius:1,color:'#152437ff'}])],'保存新配方',async data=>{await post('/api/recipes',data);await openRecipes();status('效果配方已保存')}));
  for(const recipe of recipes){const card=element('div',undefined,'version-card');card.append(element('h3',recipe.name),element('p',recipe.description||'像素处理配方','muted'));renderData(recipe.steps,card);const link=element('a','下载配方JSON');link.href='/api/recipes/'+recipe.id+'/export';card.append(link);output.append(card)}
  const importLabel=element('label','导入配方JSON');const file=element('input');file.type='file';file.accept='.json';file.onchange=guard(async e=>{if(!e.target.files[0])return;const data=JSON.parse(await e.target.files[0].text());await post('/api/recipes/import',data);await openRecipes();status('像素配方已导入')});importLabel.append(file);controls.append(element('p'),importLabel);layout.append(controls,output);body.append(layout);
}

$('compose').onclick=guard(openComposition);
async function openComposition(){
  const body=dialog('分轨镜头生成');
  body.append(element('p','选择同尺寸动画或片段，依照真实时间安排最多4条轨道。每条轨道画面在镜头中作为一层，源项目内的图层仍留在源项目。镜头最多30秒、300个曝光帧；普通动画保持20毫秒下限。','hint'));
  if(!S.projects.length){body.append(element('p','先创建或导入用于镜头的源动画。'));return}
  const rows=[],trackArea=element('div');
  async function addTrack(){
    if(rows.length>=4)throw Error('最多4条轨道');
    const panel=element('div',undefined,'composition-track import-panel'),source=element('select'),clip=element('select');source.name='source_project';clip.name='source_clip';
    for(const p of S.projects)source.append(new Option(p.name,p.id));
    const sourceLabel=element('label','源动画'),clipLabel=element('label','源动作片段');sourceLabel.append(source);clipLabel.append(clip);panel.append(element('h3','轨道 '+(rows.length+1)),sourceLabel,clipLabel);
    const fields={};
    for(const definition of [num('start','开始时间 ms',0,0,30000),num('repeat','播放轮数（0为持续循环）',1,0,1000),check('hold','结束后保留末画面'),num('x','轨道位置X',0,-2048,2048),num('y','轨道位置Y',0,-2048,2048)]){const item=field(definition);fields[definition.name]=item.querySelector('input');panel.append(item)}
    const row={source,clip,fields};rows.push(row);trackArea.append(panel);
    async function refreshClips(){const p=await api('/api/projects/'+source.value);clip.replaceChildren(new Option('完整时间轴',''));for(const c of p.clips)clip.append(new Option(c.name,c.id))}
    source.onchange=guard(refreshClips);await refreshClips();
  }
  body.append(trackArea);await addTrack();
  const add=element('button','添加镜头轨道');add.onclick=guard(addTrack);body.append(add);
  const settings=[text('name','镜头名称','我的分轨镜头'),num('total','镜头总时长 ms',1000,20,30000)];
  body.append(form(settings,'生成独立镜头',async data=>{
    data.tracks=rows.map(row=>({project_id:row.source.value,clip_id:row.clip.value||null,start:Number(row.fields.start.value),repeat:Number(row.fields.repeat.value),hold:row.fields.hold.checked,x:Number(row.fields.x.value),y:Number(row.fields.y.value)}));
    const p=await work('生成分轨镜头…',()=>post('/api/compositions',data));S.frame=S.layer=null;await acceptProject(p);$('dialog').close();status('镜头已生成，可检查时间轴并导出APNG或可编辑项目归档');
  }));
}

function renderRoleChoices(){
  const current=$('roleInk').value;
  $('roleInk').replaceChildren();
  for(const role of project().ink_roles||[]){$('roleInk').append(new Option(`${role.id} · ${role.name}`,String(role.id)))}
  if([...$('roleInk').options].some(o=>o.value===current))$('roleInk').value=current;
}
$('roleInk').onchange=()=>{const role=project().ink_roles?.find(r=>r.id===Number($('roleInk').value));if(role)$('color').value='#'+role.rgb.map(x=>x.toString(16).padStart(2,'0')).join('')};
$('roleTools').onclick=guard(openRoleTools);
$('rigTools').onclick=guard((...args)=>openRig(...args));
async function openRoleTools(){
  project();const data=await api(endpoint('ink-roles/status'));const body=dialog('角色色墨水与像素归属');
  body.append(element('p','角色色图层使用角色编号决定颜色。两个角色可暂时同色，改色与重排不应合并它们；墨水覆盖度独立于颜色。旧像素没有足够归属依据时，应先明确指定区域。','hint'));
  body.append(form([arr('roles','角色定义 [{id,name,rgb:[r,g,b]},…]',data.roles)],'保存角色定义',async values=>{await mutate('保存角色定义…',endpoint('ink-roles'),values);await openRoleTools()}));
  const enable=element('button','当前层启用角色色墨水');enable.onclick=guard(async()=>{await mutate('启用角色色墨水…',layerPath('ink/enable'));await openRoleTools()});body.append(enable);
  const options=data.roles.map(r=>[String(r.id),`${r.id} · ${r.name}`]);
  body.append(form([select('role_id','指定颜色角色',options[0]?.[0],options),num('x','原图层区域X',0,0),num('y','原图层区域Y',0,0),num('width','区域宽度',project().width,1),num('height','区域高度',project().height,1)],'明确指定该区域归属',async values=>{await mutate('指定像素颜色角色…',layerPath('ink/assign'),{...values,type:'rectangle'});await openRoleTools()}));
  const plain=element('button','转为普通图层，保留当前外观（可撤销）');plain.onclick=guard(async()=>{await mutate('转换为普通图层…',layerPath('ink/materialize'));await openRoleTools()});body.append(plain);
  body.append(element('h3','未确认的可见像素'));renderData(data.layers,body);
  body.append(element('p','启用后关闭本窗口，用画布上方的墨水角色和覆盖度绘制。需要混色、整体几何或普通像素特效时，可先转为普通图层；旧版本仍可撤销恢复。','hint'));
}

$('celTools').onclick=guard(openCelTools);
async function openCelTools(){
  const p=project(),source=currentLayer(),frame=currentFrame(),body=dialog('共用原画与独立实例');
  body.append(element('p','共用原画只共享像素，位置、旋转、缩放、透明度和显示仍由各实例保留。铅笔、橡皮与线条形状可直接在变换后的实例画布绘制；一笔可整体撤销。其他像素处理可先脱离，烘焙也只脱离当前实例。','hint'));
  if(source.cel_id){
    const linked=p.frames.flatMap(f=>f.layers.filter(l=>l.cel_id===source.cel_id).map(l=>({frame:f.name,layer:l.name,transform:l.transform})));
    body.append(element('h3','当前共用原画：'+linked.length+' 个实例'));renderData(linked,body);
    const detach=element('button','当前实例脱离共用原画');detach.onclick=guard(async()=>{const result=await post(layerPath('cel-detach'),{revision:project().revision});await acceptProject(result);openCelTools()});body.append(detach);return;
  }
  body.append(element('p','当前层作为原件来源。下列所选实例将使用它的像素，保留各自的实例属性；已有原件组请先脱离。','muted'));
  const choices=[];
  for(const f of p.frames)for(const layer of f.layers){
    if(layer.id===source.id&&f.id===frame.id)continue;
    const label=element('label',undefined,'check'),input=element('input');input.type='checkbox';input.disabled=layer.locked||Boolean(layer.cel_id);label.append(input,document.createTextNode(f.name+' / '+layer.name+(layer.cel_id?'（已共用）':'')));body.append(label);choices.push({input,frame_id:f.id,layer_id:layer.id});
  }
  const create=element('button','建立共用原画','primary');create.onclick=guard(async()=>{const targets=choices.filter(c=>c.input.checked).map(c=>({frame_id:c.frame_id,layer_id:c.layer_id}));const result=await post(endpoint('cels/link'),{revision:project().revision,source_frame:frame.id,source_layer:source.id,targets});await acceptProject(result);openCelTools()});body.append(create);
}

$('atlasWorkspace').onclick=guard(()=>openAtlasWorkspace());
async function openAtlasWorkspace(identifier){
  const p=project(),body=dialog('图集工程与固定UV版本'),items=p.atlas_workspaces||[];
  body.append(element('p','固定图块保留已发布的槽位及原画采样区域，预留像素可容纳原位置附近的后续修改；其他图块可重新排列。每次成功生成独立版本，旧版仍可下载。','hint'));
  if(!identifier&&items.length)identifier=items[0].id;
  if(items.length){const choose=element('select');choose.setAttribute('aria-label','图集工程');for(const item of items)choose.append(new Option(item.name,item.id));choose.value=identifier;choose.onchange=()=>openAtlasWorkspace(choose.value);body.append(choose)}
  const ws=items.find(item=>item.id===identifier);
  if(ws){
    body.append(element('h3',ws.name+' · '+ws.size.join('×')));const choices=[];
    body.append(element('p','勾选需要保留上次已发布槽位的图块；取消勾选允许重新排列。未勾选并不删除源帧。'));
    for(const frame of p.frames){const label=element('label',undefined,'check'),input=element('input');input.type='checkbox';input.checked=ws.locked.includes(frame.id);input.setAttribute('aria-label','固定 '+frame.name);label.append(input,document.createTextNode(frame.name));body.append(label);choices.push({input,id:frame.id})}
    const update=element('button','更新图集并保留固定槽位','primary');update.onclick=guard(async()=>{await mutate('生成图集版本…',endpoint('atlas-workspaces/'+ws.id+'/build'),{locked:choices.filter(c=>c.input.checked).map(c=>c.id)});await openAtlasWorkspace(ws.id)});body.append(update);
    const inspection=element('div');body.append(inspection);
    function showVersion(version){inspection.replaceChildren(element('h3','查看第 '+version.number+' 版'));const image=element('img');image.alt='图集第 '+version.number+' 版像素预览';image.src=endpoint('atlas-workspaces/'+ws.id+'/versions/'+version.number+'/preview');image.style.width=Math.min(512,ws.size[0]*4)+'px';image.style.maxWidth='100%';image.style.imageRendering='pixelated';inspection.append(image);renderData(version.entries.map(e=>({'图块':e.name,'槽位像素':e.uv_pixels.join(', '),'原画采样区域':e.region.join(', '),'旋转':e.rotated})),inspection)}
    const latest=ws.versions[ws.versions.length-1];if(latest)showVersion(latest);
    for(const version of [...ws.versions].reverse()){const row=element('div'),preview=element('button','预览第 '+version.number+' 版'),link=element('a','下载第 '+version.number+' 版图集');preview.onclick=()=>showVersion(version);link.href=endpoint('atlas-workspaces/'+ws.id+'/versions/'+version.number+'/download');row.append(preview,link);body.append(row)}
  }
  body.append(element('h3','另建图集工程'));
  body.append(form([text('name','工程名称','游戏图集'),num('width','图集宽',256,32,2048),num('height','图集高',256,32,2048),num('reserve','每侧原画预留像素',4,0,32),num('padding','图块间隔',1,0,16),num('extrude','边缘外扩',1,0,8),check('rotate','允许旋转')],'创建图集工程',async values=>{await mutate('建立图集工程…',endpoint('atlas-workspaces'),values);await openAtlasWorkspace(project().atlas_workspaces.at(-1).id)}));
}
