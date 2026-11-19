'use strict';
const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
const viewer = location.pathname.endsWith('/view');
let token = viewer ? '' : sessionStorage.getItem('classroomToken') || '';
let course = null, editIndex = 0, roomId = null, snapshot = null, pc = null, eventAbort = null;
if (viewer) document.body.classList.add('viewer');
if (params.get('program') === '1') document.body.classList.add('program');
$('key').value = token;
const root = () => '/api/classrooms/' + encodeURIComponent(roomId);
function notice(text, error = false) { $('message').textContent = text; $('message').classList.toggle('error', error); }
async function api(url, data, method = 'GET') {
  const headers = token ? {Authorization: 'Bearer ' + token} : {};
  let body;
  if (data instanceof FormData) body = data;
  else if (data !== undefined) { body = JSON.stringify(data); headers['Content-Type'] = 'application/json'; }
  const response = await fetch(url, {method, headers, body});
  if (!response.ok) throw new Error((await response.text()).slice(0, 350));
  return (await response.json()).data;
}
function action(id, handler) {
  $(id).addEventListener('click', async () => {
    $(id).disabled = true;
    try { await handler(); } catch (error) { notice(error.message, true); }
    finally { $(id).disabled = false; }
  });
}
function options(id, rows, label) {
  $(id).replaceChildren();
  rows.forEach(row => { const opt = document.createElement('option'); opt.value = row.id; opt.textContent = label(row); $(id).append(opt); });
}
async function library() {
  options('courses', await api('/api/courses'), c => c.title + ' · ' + c.pages + ' 页');
  options('rooms', await api('/api/classrooms'), r => r.title);
  $('library').hidden = false;
}
function rememberScript() { if (course) course.slides[editIndex].script = $('script').value; }
async function editCourse(id) {
  course = await api('/api/courses/' + id); editIndex = 0;
  options('editPage', course.slides.map(s => ({id: s.index, title: s.title})), s => (s.id + 1) + ' · ' + s.title);
  $('editor').hidden = false; showPage();
}
function showPage() {
  const pageId = course.id + ':' + editIndex;
  $('script').value = course.slides[editIndex].script;
  // Course previews use an authorized fetch, never place presenter secrets in URLs.
  const headers = {Authorization: 'Bearer ' + token};
  fetch('/api/courses/' + course.id + '/slides/' + editIndex + '.png', {headers})
    .then(r => { if (!r.ok) throw Error('课件预览加载失败'); return r.blob(); })
    .then(blob => { if (course.id + ':' + editIndex !== pageId) return; const previous = $('preview').src; $('preview').src = URL.createObjectURL(blob); if (previous.startsWith('blob:')) URL.revokeObjectURL(previous); })
    .catch(e => notice(e.message, true));
}
$('editPage').onchange = () => { rememberScript(); editIndex = Number($('editPage').value); showPage(); };
async function saveCourse() {
  rememberScript();
  course = await api('/api/courses/' + course.id + '/scripts', {scripts: course.slides.map(s => s.script)}, 'PUT');
  notice('讲稿已保存。已有课堂使用创建时的版本；修改后请创建新课堂。');
}
action('login', async () => {
  token = $('key').value.trim(); await library(); sessionStorage.setItem('classroomToken', token);
  if (params.get('room')) await enter(params.get('room'));
  else notice('控制台已连接。');
});
action('upload', async () => {
  const file = $('file').files[0]; if (!file) throw Error('请选择 PDF 或 PPTX');
  if (file.size > 50 * 1024 * 1024) throw Error('课件超过 50 MB');
  const form = new FormData(); form.append('file', file);
  notice('正在转换课件，请稍候…');
  const imported = await api('/api/courses', form, 'POST'); await library(); $('courses').value = imported.id;
  await editCourse(imported.id); notice('已导入 ' + imported.slides.length + ' 页，请检查逐页讲稿。');
});
action('load', () => editCourse($('courses').value));
action('save', saveCourse);
action('create', async () => { await saveCourse(); const room = await api('/api/classrooms', {course_id: course.id}, 'POST'); await enter(room.id); });
action('resumeRoom', () => enter($('rooms').value));
async function enter(id) {
  if (!id) throw Error('请选择课堂');
  if (eventAbort) eventAbort.abort(); if (pc) { pc.close(); pc = null; }
  roomId = id; snapshot = await api(root());
  $('live').hidden = $('askPanel').hidden = false;
  $('share').value = location.origin + '/classroom/view?room=' + encodeURIComponent(id);
  $('program').href = $('share').value + '&program=1';
  history.replaceState(null, '', location.pathname + '?room=' + encodeURIComponent(id) + (params.get('program') === '1' ? '&program=1' : ''));
  render(snapshot); eventAbort = new AbortController(); watch(eventAbort.signal);
  notice('已进入课堂。点击播放音视频；主持人可开始授课。');
  if (params.get('program') === '1') await connect();
}
function render(s) {
  snapshot = s;
  const names = {paused:'已暂停', teaching:'授课中', answering:'回答中', finished:'讲授完成', ended:'已结束', error:'任务异常'};
  $('title').textContent = s.title; $('state').textContent = names[s.state] || s.state;
  $('pageLabel').textContent = '第 ' + (s.page + 1) + ' / ' + s.pages + ' 页';
  const src = root() + '/slides/' + s.page + '.png'; if ($('slide').getAttribute('src') !== src) $('slide').src = src;
  $('caption').textContent = s.caption;
  $('auto').checked = s.auto_approve;
  if (s.error) notice(s.error, true);
  else if (s.pause_requested) notice('将在当前段落 / 回答结束后暂停。');
  $('questions').replaceChildren();
  s.questions.slice().reverse().forEach(q => {
    const item = document.createElement('article'); item.className = 'question';
    const info = document.createElement('small'); info.textContent = q.name + ' · 第 ' + (q.page + 1) + ' 页 · ' + q.status;
    const text = document.createElement('p'); text.textContent = q.text; item.append(info, text);
    if (q.answer) { const answer = document.createElement('p'); answer.className='answer'; answer.textContent=q.answer; item.append(answer); }
    if (!viewer && ['pending', 'approved', 'failed'].includes(q.status)) {
      ['approve', 'reject'].forEach(a => { const b = document.createElement('button'); b.textContent = a === 'approve' ? '通过 / 重试' : '拒绝'; b.onclick = () => api(root() + '/questions/' + q.id, {action:a}, 'POST').catch(e=>notice(e.message,true)); item.append(b); });
    }
    $('questions').append(item);
  });
  if (s.state === 'ended') { if (eventAbort) eventAbort.abort(); if (pc) pc.close(); notice('课堂已结束。'); }
}
async function watch(signal) {
  const eventUrl = root() + '/events';
  while (!signal.aborted) {
    try {
      const response = await fetch(eventUrl, {headers: token ? {Authorization:'Bearer '+token} : {}, signal});
      if (!response.ok) { notice('课堂不可用：' + await response.text(), true); return; }
      const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '';
      while (!signal.aborted) {
        const result = await reader.read(); if (result.done) break;
        buffer += decoder.decode(result.value, {stream:true});
        let end;
        while ((end = buffer.indexOf('\n\n')) >= 0) {
          const block = buffer.slice(0,end); buffer=buffer.slice(end+2);
          const data = block.split('\n').find(line=>line.startsWith('data: '));
          if (block.includes('event: state') && data) render(JSON.parse(data.slice(6)));
          if (block.includes('event: ended')) { notice('课堂已结束。'); if (pc) pc.close(); return; }
        }
      }
    } catch (e) { if (signal.aborted) return; notice('状态连接断开，正在重连…'); }
    await new Promise(resolve=>setTimeout(resolve,2000));
  }
}
async function connect() {
  if (!roomId) throw Error('先进入课堂');
  if (pc) pc.close();
  pc = new RTCPeerConnection(); const current = pc, stream = new MediaStream(); $('video').srcObject = stream;
  pc.ontrack = e => { stream.addTrack(e.track); $('video').play().catch(()=>notice('点击视频播放按钮以启用声音。')); };
  pc.onconnectionstatechange = () => $('mediaState').textContent = current.connectionState;
  pc.addTransceiver('audio', {direction:'recvonly'}); pc.addTransceiver('video', {direction:'recvonly'});
  try {
    await pc.setLocalDescription(await pc.createOffer());
    await new Promise((resolve,reject)=>{
      if (current.iceGatheringState==='complete') return resolve();
      const timer = setTimeout(()=>{current.removeEventListener('icegatheringstatechange',done);reject(Error('ICE 连接超时，请检查网络'));},15000);
      function done(){if(current.iceGatheringState==='complete'){clearTimeout(timer);current.removeEventListener('icegatheringstatechange',done);resolve();}}
      current.addEventListener('icegatheringstatechange',done);
    });
    const answer = await api(root()+'/offer', {sdp:current.localDescription.sdp,type:current.localDescription.type}, 'POST');
    await current.setRemoteDescription(answer);
  } catch (e) { current.close(); throw e; }
}
async function control(action, value) { render(await api(root()+'/control', {action,page:value,enabled:value}, 'POST')); }
action('connect',connect); action('start',()=>control('start')); action('pause',()=>control('pause'));
action('previous',()=>control('goto',Math.max(0,snapshot.page-1))); action('next',()=>control('goto',Math.min(snapshot.pages-1,snapshot.page+1)));
$('auto').onchange = ()=>control('auto_approve',$('auto').checked).catch(e=>notice(e.message,true));
action('end',async()=>{await api(root(),undefined,'DELETE'); eventAbort?.abort(); pc?.close(); $('live').hidden=true; await library(); notice('课堂已结束，课程和讲稿已保留。');});
action('ask',async()=>{const q=await api(root()+'/questions',{text:$('question').value,name:$('name').value},'POST');$('question').value='';notice(q.status==='approved'?'问题已排队，稍后回答。':'问题已提交，等待主持人审核。');});
window.addEventListener('pagehide',()=>{eventAbort?.abort();pc?.close();});
if (viewer) { if (params.get('room')) enter(params.get('room')).catch(e=>notice(e.message,true)); else notice('请使用主持人提供的课堂邀请链接。',true); }
