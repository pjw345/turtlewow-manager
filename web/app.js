const $ = id => document.getElementById(id);
const names = {start: 'Start server', stop: 'Shut down server', backup: 'Create backup', build: 'Build latest source', activate: 'Activate build'};
const descriptions = {
  start: 'Start the database, login and world services using the selected image. Initialization may take a few minutes.',
  stop: 'Stop the world, login and database services cleanly. Connected players will be disconnected; saved data is retained.',
  backup: 'Stop the services, archive the database and configuration, and verify archive checksums. Previously running services will be restarted after success.',
  build: 'Build the committed development revision with two compile jobs. Servers will stop and a cold backup will be taken first. They remain stopped after the build; activation and startup are separate steps.',
  activate: 'Stop services, take a cold backup and select the last verified build for the next startup. This does not start the server.'
};
let current = null, sending = false, pending = null;
function text(id, value) { $(id).textContent = value ?? '—'; }
function bytes(n) { return (n / 1073741824).toFixed(1); }
function timeLabel(value) { return value ? new Date(value).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) : '—'; }
function toast(message) { text('toast', message); $('toast').hidden=false; setTimeout(() => {$('toast').hidden=true;},6500); }
function element(tag, cls, value) { const el=document.createElement(tag); if(cls)el.className=cls; if(value !== undefined)el.textContent=value; return el; }
function render(data) {
  current=data;
  $('connection').className='connection live'; $('connection').lastChild.textContent=' Live connection';
  $('error-banner').hidden=!(data.error||data.warning); text('error-banner',data.error||data.warning);
  const ready=data.services?.mangosd?.ready;
  text('realm-status',data.busy ? 'An operation is in progress' : data.warning ? 'The realm needs attention' : ready ? 'The realm is ready for adventure' : data.services?.mangosd?.state==='running' ? 'World server is initializing' : 'The realm is resting');
  $('realm-dot').className='status-dot '+(data.busy ? 'busy' : ready ? 'online' : '');
  text('refresh-time','Updated '+timeLabel(data.sampled_at));
  for(const key of ['realmd','mangosd','db']) {
    const s=data.services?.[key], b=$(key+'-badge');
    b.textContent=!s ? 'Unavailable' : s.ready ? 'Running' : s.state==='running' ? 'Starting' : ['exited','created','missing'].includes(s.state) ? 'Stopped' : s.state;
    b.className='badge '+(!s ? 'bad' : s.ready ? 'good' : s.state==='running' ? 'pending' : 'muted');
    if(s?.state==='exited'&&s.exit_code) {b.textContent='Exit '+s.exit_code;b.className='badge bad';}
  }
  const stale=data.counts_sampled_at && Date.now()-Date.parse(data.counts_sampled_at)>45000;
  text('users',stale ? '—' : data.counts?.users); text('bots',stale ? '—' : data.counts?.bots);
  text('bots-detail',stale ? 'Telemetry is stale; waiting for update' : data.counts ? `${data.counts.active_bots} active · native Playerbot telemetry` : 'Waiting for native telemetry');
  if(data.memory) { $('memory').replaceChildren(document.createTextNode(bytes(data.memory.used)),element('small','',' GB')); text('memory-detail',`${bytes(data.memory.total)} GB available to the VM`); $('memory-meter').style.width=`${Math.min(100,data.memory.used/data.memory.total*100)}%`; }
  if(data.disk) { $('disk').replaceChildren(document.createTextNode(bytes(data.disk.free)),element('small','',' GB')); text('disk-detail',`${bytes(data.disk.total)} GB filesystem capacity`); $('disk-meter').style.width=`${Math.min(100,(data.disk.total-data.disk.free)/data.disk.total*100)}%`; }
  text('revision',data.version?.slice(0,8)); text('image',data.image); text('selected-image',data.selected_image); text('dev-revision',data.development_revision?.slice(0,12));
  text('version-state',ready ? 'Running' : 'Container version');
  text('operation-badge',data.busy ? 'Operation running' : 'Ready'); $('operation-badge').className='badge '+(data.busy?'pending':'good');
  $('operation-progress').hidden=!data.active; text('operation-title',names[data.active?.action]); text('operation-message',data.active?.message);
  $('available-build').hidden=!data.build || data.build.image===data.selected_image;
  document.querySelectorAll('[data-action]').forEach(button => {button.disabled=sending||data.busy||!!data.error||(button.dataset.action==='start'&&ready)||(button.dataset.action==='activate'&&!data.build);});
  const list=$('backup-list');
  if(data.backups?.length) { list.replaceChildren(); for(const backup of data.backups.slice(0,4)) {const row=element('div','backup-row'); row.append(element('span','backup-icon','▤')); const info=element('div'); info.append(element('strong','',backup.name),element('p','','Database · marker · configuration')); row.append(info,element('span','backup-size',bytes(backup.size)+' GB')); list.append(row);} }
  const history=[...(data.active?[data.active]:[]),...(data.history||[])].slice(0,6), activities=$('activity-list');
  if(history.length) { activities.replaceChildren(); for(const op of history) {const row=element('div','activity-row'); const info=element('div'); info.append(element('strong','',(names[op.action]||op.action)+' · '+op.status),element('p','',op.message)); row.append(element('span','activity-dot '+op.status),info,element('span','activity-time',timeLabel(op.finished_at||op.started_at))); activities.append(row);} }
  text('operation-log',data.log||'No operation output yet.'); text('sample-time','Last sample '+timeLabel(data.sampled_at)+' · 5 second refresh');
}
async function poll() {
  try { const response=await fetch('/api/status',{cache:'no-store'}); if(!response.ok)throw Error('Dashboard status unavailable'); render(await response.json()); }
  catch(error) { $('connection').className='connection lost'; $('connection').lastChild.textContent=' Connection lost'; text('error-banner','Cannot reach the manager. Check your SSH tunnel; displayed values may be stale.'); $('error-banner').hidden=false; document.querySelectorAll('[data-action]').forEach(button=>button.disabled=true); }
}
document.querySelectorAll('[data-action]').forEach(button=>button.addEventListener('click',()=>{pending=button.dataset.action; text('confirm-title',names[pending]); text('confirm-description',descriptions[pending]); text('confirm-button',names[pending]); $('confirm-dialog').showModal();}));
$('confirm-dialog').addEventListener('close',async()=>{
  if($('confirm-dialog').returnValue!=='confirm'||!pending)return;
  const action=pending; pending=null; sending=true; if(current)render(current);
  try { const response=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},body:JSON.stringify({action})}); const result=await response.json(); if(!response.ok)throw Error(result.error||'Operation was not accepted'); toast('Operation started. Progress will appear below.'); }
  catch(error){toast(error.message);}finally{sending=false;await poll();}
});
$('toggle-log').addEventListener('click',()=>{const expanded=$('operation-log').hidden; $('operation-log').hidden=!expanded; $('toggle-log').setAttribute('aria-expanded',String(expanded)); text('toggle-log',expanded?'Hide output ↑':'Show output ↓');});
poll();setInterval(poll,5000);
