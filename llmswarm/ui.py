"""Web UI served at /ui."""

UI_HTML = """<!doctype html><html><head><meta charset=utf-8><title>LLMSwarm</title>
<style>
body{font-family:system-ui;background:#151515;color:#ddd;max-width:1280px;margin:1.5rem auto;font-size:16px}
h2{color:#9cf} input,textarea,select{background:#222;color:#ddd;border:1px solid #555;padding:8px 9px;border-radius:3px;font-size:15px}
.mrow{display:grid;grid-template-columns:160px minmax(220px,1.6fr) 66px 100px 100px 140px 90px 60px 56px;gap:8px;margin:4px 0;align-items:center}
.mhead{display:grid;grid-template-columns:160px minmax(220px,1.6fr) 66px 100px 100px 140px 90px 60px 56px;gap:8px;color:#8aa;font-size:13px}
.mrow.ext{background:#1b2531;border-left:3px solid #28a}
.rm{width:52px;padding:6px 4px;font-size:12px;background:#a33;color:#fff;border:0;border-radius:3px;cursor:pointer}
.rm:hover{background:#c44}
.tog{padding:5px 8px;font-size:12px;border:0;border-radius:3px;cursor:pointer;color:#fff}
.sp-toggle{background:#444;color:#9cf;border:1px solid #555;border-radius:3px;padding:5px 6px;cursor:pointer;font-size:12px;white-space:nowrap}
.sp-toggle.on{background:#2a6;color:#fff;border-color:#3c7}
.sp-section{margin:2px 0 6px 168px;padding:0 0 0 8px;border-left:2px solid #3a3a3a}
.field{margin:10px 0}.field label{display:block;margin-bottom:3px;color:#aaa}
textarea{width:100%;box-sizing:border-box}
button{background:#2a6;color:#fff;border:0;padding:8px 18px;border-radius:4px;cursor:pointer}
#status{color:#8c8;margin:0 10px}small{color:#777}
</style></head><body>
<h2>LLMSwarm</h2>
<p><small>member changes restart the llama-server processes; serve settings apply live.
Port/host changes need a full supervisor restart.</small></p>
<h3>Members <small>(highlighted rows = external / horde-worker endpoints: connect-only, never launched locally)</small></h3>
<div class=mhead><span>name</span><span>endpoint URL</span><span>temp</span><span>role</span><span>thinking</span><span>thinking style</span><span>enabled</span><span>SP</span><span>remove</span></div>
<div id=members></div>
<button id=addmember onclick=addMember() style="margin:6px 0 6px 0;background:#28a">Add Member</button>
<h3>Horde Models</h3>
<p style="color:#aaa;font-size:13px">Browse text models on the horde and add one as a <b>text member</b> that generates through the master (no local server). Image models arrive in a later pass.</p>
<div class=field>
  <button onclick=refreshHordeModels() style="background:#a26">Refresh text models</button>
  <span id=horde_cluster_note style="color:#777;font-size:12px;margin-left:8px"></span>
</div>
<div class=field><label>Horde request priority <small style="color:#777">(higher = faster, spends kudos)</small></label>
<select id=horde_priority>
  <option value=relaxed>relaxed — free, standard queue (default)</option>
  <option value=immediate>immediate — spend kudos, jump the queue</option>
  <option value=instant>instant — spend kudos, process immediately</option>
  <option value=undetermined>undetermined — let the horde decide</option>
  <option value=queue>queue</option>
  <option value=slow>slow</option>
  <option value=stall>stall — only when workers are idle</option>
</select>
<div style="color:#777;font-size:12px;margin-top:4px">Applied to every horde-routed member on Save &amp; Apply. immediate/instant cost kudos but return answers much faster.</div></div>
<div id=horde_models style="display:none;margin:8px 0;padding:8px;background:#1b2531;border-radius:4px">
  <input id=horde_model_search placeholder="filter models..." oninput=filterHordeModels() style="width:100%;margin-bottom:6px">
  <div id=horde_model_list style="max-height:220px;overflow-y:auto"></div>
  <div style="margin-top:8px;display:flex;gap:6px">
    <input id=horde_model_custom placeholder="or type any model name..." style="flex:1">
    <button onclick="addHordeTextMember(document.getElementById('horde_model_custom').value)" style="background:#28a">Add as text member</button>
  </div>
</div>
<div class=field><label>Serve mode</label>
<select id=mode><option>ensemble</option><option>swarm</option><option>agent</option></select>
<div id=modehelp style="color:#777;font-size:12px;margin-top:4px"></div></div>
<div class=field><label>Thinking/reasoning default (for members set to "auto")</label>
<select id=reasoning><option value=off>off</option><option value=on>on</option></select>
<div style="color:#777;font-size:12px;margin-top:4px">Requests may override per-call with {"reasoning": "on"/"off"}.</div></div>
<div class=field><label>Judge member (explicit override)</label><select id=judge></select>
<div style="color:#777;font-size:12px;margin-top:4px">Overrides role-based judge selection for ensemble serve, and is the horde primary judge. Leave it on a role=judge member to match; when unset, ensemble falls back to the first member with role=judge.</div></div>
<div class=field><label>Judge system prompt (live)</label>
<textarea id=jprompt rows=3></textarea></div>
<div class=field><label>Serve port (full restart)</label><input id=port size=6></div>
<div class=field><label>Member timeout s (0=off, ensemble)</label><input id=member_timeout size=6></div>
<div class=field><label><input type=checkbox id=spread_roles> Spread swarm roles across members (planner/critic/synth on distinct members, swarm mode)</label></div>
<div class=field><label>Swarm planner (pin)</label><select id=swarm_planner></select></div>
<div class=field><label>Swarm critic (pin)</label><select id=swarm_critic></select></div>
<div class=field><label>Swarm synth (pin)</label><select id=swarm_synth></select>
<div style="color:#777;font-size:12px;margin-top:4px">"auto" = role pool with rotation/spread. Pinning a member disables rotation for that role (swarm mode).</div></div>
<p><button id=save>Save &amp; Apply</button><span id=status></span></p>
<h3>Last Request Details</h3>
<button onclick=showDetails()>Show Member Outputs</button>
<div id=details style="display:none;margin-top:10px">
  <div id=details_content></div>
</div>
<pre id=out></pre>
<script>
let cfg;
function makeSPUI(enabled, text){
  const btn=document.createElement('button');
  btn.className='sp-toggle'+(enabled?' on':'');
  btn.textContent=enabled?'SP:on':'SP:off';
  btn.dataset.on = enabled ? 'true':'false';
  const sec=document.createElement('div');
  sec.className='sp-section';
  sec.style.display=enabled?'block':'none';
  const ta=document.createElement('textarea');
  ta.rows=3; ta.placeholder='System prompt (prepended to this member while on)';
  ta.value=text||'';
  sec.appendChild(ta);
  btn.onclick=()=>{
    const on=sec.style.display==='none';
    sec.style.display=on?'block':'none';
    btn.className='sp-toggle'+(on?' on':'');
    btn.textContent=on?'SP:on':'SP:off';
    btn.dataset.on = on ? 'true':'false';
  };
  return {btn,sec,ta};
}
async function loadCfg(){
  cfg = await (await fetch('/api/config')).json();
  const mrow = document.getElementById('members'); mrow.innerHTML='';
  cfg.members.forEach(m=>{
    const isH=!!m.horde_model;
    const d=document.createElement('div'); d.className='mrow'+((isH||m.url)?' ext':'');
    if(isH) d.dataset.hordeType=m.horde_type||'text';
    const inp=(ph,v)=>{const i=document.createElement('input');i.value=v??'';i.placeholder=ph||'';d.appendChild(i);return i;};
    const sel=(opts,v)=>{const s=document.createElement('select');
      opts.forEach(o=>{const x=document.createElement('option');x.value=o;x.textContent=o;s.appendChild(x);});
      s.value=v;d.appendChild(s);return s;};
    const sp=makeSPUI(!!m.system_prompt_enabled, m.system_prompt||'');
    const name=inp('name',m.name), url=inp(isH?'horde model':'http://host:port', isH?m.horde_model:(m.url||'')),
          temp=inp('temp',m.temperature),
          role=sel(['worker','judge','alt_judge','planner'],m.role||'worker'),
          rsn=sel(['auto','on','off'],m.reasoning||'auto'),
          rstyle=sel(['chat_template_kwargs','enable_thinking','thinking_type','reasoning_effort','none'],
                     m.reasoning_style||'chat_template_kwargs'),
          enb=sel(['true','false'],m.enabled?'true':'false');
    d.appendChild(sp.btn);
    const rm=inp('x','');
    rm.placeholder='remove'; rm.title='Click to remove this member'; rm.className='rm';
    enb.dataset.role='toggle';
    d._fields={name,url,temp,role,rsn,rstyle,enb,rm,spTa:sp.ta,spBtn:sp.btn};
    d._spsec=sp.sec;
    rm.onclick=()=>removeMember(d);
    mrow.appendChild(d);
    mrow.appendChild(sp.sec);
  });
  const jSel=document.getElementById('judge');
  jSel.innerHTML='';
  cfg.members.forEach(m=>{
    const o=document.createElement('option');
    o.value=m.name; o.textContent=m.name;
    jSel.appendChild(o);
  });
  jSel.value=(cfg.serve.judge && cfg.members.some(m=>m.name===cfg.serve.judge))
    ? cfg.serve.judge : cfg.members[cfg.members.length-1].name;
  document.getElementById('mode').value=cfg.serve.mode;
  updateModeHelp();
  document.getElementById('jprompt').value=cfg.serve.judge_prompt||'';
  document.getElementById('reasoning').value=cfg.serve.reasoning||'off';
  document.getElementById('port').value=cfg.serve.port;
  document.getElementById('member_timeout').value=cfg.serve.member_timeout??0;
  document.getElementById('spread_roles').checked=!!cfg.serve.spread_roles;
  fillSwarmRoleSels();
  ['swarm_planner','swarm_critic','swarm_synth'].forEach(id=>{
    const s=document.getElementById(id), v=cfg.serve[id];
    if(v && [...s.options].some(o=>o.value===v)) s.value=v;
  });
  document.getElementById('horde_priority').value=(cfg.horde&&cfg.horde.priority)||'relaxed';
}
const MODE_HELP={
  ensemble:"Ensemble: every member answers in parallel; the judge merges all replies into one final answer.",
  swarm:"Swarm: a planner decomposes the problem, workers solve subtasks in parallel, results merge at the end.",
  agent:"Agent: each member works independently with tools (read, bash, grep, etc.); the judge merges all findings."
};
function updateModeHelp(){
  const v=document.getElementById('mode').value;
  document.getElementById('modehelp').textContent=MODE_HELP[v]||"";
}
function makeMemberRow(opts){
  opts=opts||{};
  const d=document.createElement('div'); d.className='mrow'+(opts.ext?' ext':'');
  const inp=(ph,v)=>{const i=document.createElement('input');i.value=v??'';i.placeholder=ph||'';d.appendChild(i);return i;};
  const sel=(o,v)=>{const s=document.createElement('select');
    o.forEach(x=>{const y=document.createElement('option');y.value=x;y.textContent=x;s.appendChild(y);});
    s.value=v;d.appendChild(s);return s;};
  const sp=makeSPUI(false,'');
  const name=inp(opts.namePh||'name',opts.name||''),
        url=inp(opts.urlPh||'http://host:port',opts.url||''),
        temp=inp('temp',opts.temp??0.7),
        role=sel(['worker','judge','alt_judge','planner'],opts.role||'worker'),
        rsn=sel(['auto','on','off'],'auto'),
        rstyle=sel(['chat_template_kwargs','enable_thinking','thinking_type','reasoning_effort','none'],
                   'chat_template_kwargs'),
        enb=sel(['true','false'],opts.enabled?'true':'false');
  d.appendChild(sp.btn);
  const rm=inp('x','');
  rm.placeholder='remove'; rm.title='Click to remove this member';
  rm.className='rm'; rm.onclick=()=>removeMember(d);
  enb.dataset.role='toggle';
  d._fields={name,url,temp,role,rsn,rstyle,enb,rm,spTa:sp.ta,spBtn:sp.btn};
  d._spsec=sp.sec;
  return d;
}
function appendRow(d){
  const mrow=document.getElementById('members');
  mrow.appendChild(d);
  mrow.appendChild(d._spsec);
}
function addMember(){
  const d=makeMemberRow({});
  appendRow(d);
  d._fields.name.focus();
}
function fillSwarmRoleSels(){
  ['swarm_planner','swarm_critic','swarm_synth'].forEach(id=>{
    const s=document.getElementById(id);
    const cur=s.value;
    s.innerHTML='';
    const a=document.createElement('option');
    a.value=''; a.textContent='auto';
    s.appendChild(a);
    [...document.querySelectorAll('#members .mrow')].forEach(r=>{
      const o=document.createElement('option');
      o.value=r._fields.name.value.trim(); o.textContent=o.value||'(unnamed)';
      s.appendChild(o);
    });
    s.value=(cur && [...s.options].some(o=>o.value===cur)) ? cur : '';
  });
}
function syncJudgeOptions(){
  const jSel=document.getElementById('judge');
  // keep judge options in sync with remaining members
  jSel.innerHTML='';
  [...document.querySelectorAll('#members .mrow')].forEach(r=>{
    const o=document.createElement('option');
    o.value=r._fields.name.value.trim(); o.textContent=o.value||'(unnamed)';
    jSel.appendChild(o);
  });
  fillSwarmRoleSels();
}
function removeMember(d){
  if(d._spsec && d._spsec.parentNode) d._spsec.remove();
  d.remove();
  syncJudgeOptions();
}
async function refreshHordeModels(){
  const box=document.getElementById('horde_models');
  const note=document.getElementById('horde_cluster_note');
  try{
    const j=await (await fetch('/api/horde_models')).json();
    box.style.display='block';
    const src=j.source==='live'?'':' -- offline, curated list';
    note.textContent = j.cluster ? ('cluster: '+j.cluster+(j.has_key?'':' -- no api_key set!')+src)
                                 : 'no [horde] cluster configured';
    const list=document.getElementById('horde_model_list');
    list.innerHTML='';
    const meta=j.meta||{};
    // own worker first (routes to this machine's horde worker)
    if(j.own_worker) renderModelRow(list, j.own_worker, '  (you)', meta[j.own_worker]||{workers:1,eta:0,performance:0});
    (j.models||[]).filter(m=>m!==j.own_worker).forEach(m=>renderModelRow(list, m, '', meta[m]));
  }catch(e){ note.textContent='error loading models: '+e; }
}
function renderModelRow(list, name, marker, meta){
  const row=document.createElement('div');
  row.className='horde-model'; row.dataset.name=name;
  row.style.cssText='display:flex;justify-content:space-between;align-items:center;padding:4px 6px;border-bottom:1px solid #2a3542;gap:8px';
  const left=document.createElement('div');
  left.style.cssText='flex:1;min-width:0';
  const nm=document.createElement('div'); nm.textContent=name+marker;
  nm.style.cssText='overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
  left.appendChild(nm);
  if(meta){
    const st=document.createElement('div');
    st.textContent=(meta.workers??0)+' workers · ETA '+(meta.eta??0)+'s · perf '+
      (meta.performance!=null?(+meta.performance).toFixed(1):'0');
    st.style.cssText='color:#8aa;font-size:11px';
    left.appendChild(st);
  }
  const btn=document.createElement('button'); btn.textContent='Add';
  btn.style.cssText='background:#28a;color:#fff;border:0;padding:3px 10px;border-radius:3px;cursor:pointer;font-size:12px;flex-shrink:0';
  btn.onclick=()=>addHordeTextMember(name);
  row.appendChild(left); row.appendChild(btn);
  list.appendChild(row);
}
function filterHordeModels(){
  const q=(document.getElementById('horde_model_search').value||'').toLowerCase();
  document.querySelectorAll('.horde-model').forEach(r=>{
    r.style.display=r.dataset.name.toLowerCase().includes(q)?'flex':'none';
  });
}
function addHordeTextMember(model){
  model=(model||'').trim();
  if(!model) return;
  const d=makeMemberRow({ext:true, namePh:'member name', enabled:true});
  d.dataset.hordeType='text';
  d._fields.url.value=model; d._fields.url.placeholder='horde model';
  d._fields.name.value=model;
  appendRow(d);
  syncJudgeOptions();
  d._fields.name.focus();
}

function collect(){
  const members=[...document.querySelectorAll('#members .mrow')].map(d=>{
    const f=d._fields;
    const htype=d.dataset.hordeType;
    return {name:f.name.value.trim(),
            url:htype?'':f.url.value.trim(),
            temperature:f.temp.value,
            role:f.role.value,
            reasoning:f.rsn.value,reasoning_style:f.rstyle.value,
            enabled:f.enb.value==='true',
            system_prompt:f.spTa?f.spTa.value:'',
            system_prompt_enabled:f.spBtn?f.spBtn.dataset.on==='true':false,
            horde_type:htype||'',
            horde_model:htype?f.url.value.trim():''};
  }).filter(m=>m.name);  // drop blank rows left from an unfilled Add Member
  return {serve:{mode:document.getElementById('mode').value,
                 reasoning:document.getElementById('reasoning').value,
                 judge:document.getElementById('judge').value,
                 judge_prompt:document.getElementById('jprompt').value,
                 port:+document.getElementById('port').value,
                 member_timeout:+document.getElementById('member_timeout').value||0,
                 spread_roles:document.getElementById('spread_roles').checked,
                 swarm_planner:document.getElementById('swarm_planner').value,
                 swarm_critic:document.getElementById('swarm_critic').value,
                 swarm_synth:document.getElementById('swarm_synth').value},
          horde:{priority:document.getElementById('horde_priority').value},
          members};
}
async function save(){
  const st=document.getElementById('status'); st.textContent='saving...';
  const r=await fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(collect())});
  const j=await r.json();
  st.textContent=(j.restarted?'saved, members restarted':'saved (live)')+
    (j.error?' -- '+j.error:'');
  const out=document.getElementById('out');
  out.textContent=JSON.stringify(j,null,1).slice(0,600);
  loadCfg();
}
document.getElementById('mode').addEventListener('change',updateModeHelp);
document.getElementById('save').onclick=save;

function showDetails(){
  const div=document.getElementById('details');
  const content=document.getElementById('details_content');
  if(div.style.display==='none'){
    div.style.display='block';
    fetch('/api/last_details').then(r=>r.json()).then(d=>{
      if(!d.members){content.innerHTML='<em>No details available</em>';return;}
      let html='<h4>Member Outputs (request: '+d.request_id+')</h4>';
      for(const [name, text] of Object.entries(d.members)){
        html+='<div style="margin:10px 0;padding:10px;background:#222;border-radius:4px">';
        html+='<strong>'+name+':</strong> '+text.slice(0,500)+'...';
        html+='</div>';
      }
      html+='<h4>Judge Output:</h4><div style="padding:10px;background:#333;border-radius:4px">'+d.final+'</div>';
      content.innerHTML=html;
    }).catch(e=>{content.innerHTML='<em>Error: '+e+'</em>';});
  } else {
    div.style.display='none';
  }
}

loadCfg();
</script></body></html>"""


