"""Web UI served at /ui."""

UI_HTML = """<!doctype html><html><head><meta charset=utf-8><title>LLMSwarm</title>
<style>
body{font-family:system-ui;background:#151515;color:#ddd;max-width:1280px;margin:1.5rem auto;font-size:16px}
h2{color:#9cf} input,textarea,select{background:#222;color:#ddd;border:1px solid #555;padding:8px 9px;border-radius:3px;font-size:15px}
.mrow{display:grid;grid-template-columns:160px minmax(220px,1.6fr) 66px 100px 100px 140px 90px 60px 56px;gap:8px;margin:4px 0;align-items:center}
.mhead{display:grid;grid-template-columns:160px minmax(220px,1.6fr) 66px 100px 100px 140px 90px 60px 56px;gap:8px;color:#8aa;font-size:13px}
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
<h3>Members</h3>
<div class=mhead><span>name</span><span>endpoint URL</span><span>temp</span><span>role</span><span>thinking</span><span>thinking style</span><span>enabled</span><span>SP</span><span>remove</span></div>
<div id=members></div>
<button id=addmember onclick=addMember() style="margin:6px 0;background:#28a">Add Member</button>
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
    const d=document.createElement('div'); d.className='mrow';
    const inp=(ph,v)=>{const i=document.createElement('input');i.value=v??'';i.placeholder=ph||'';d.appendChild(i);return i;};
    const sel=(opts,v)=>{const s=document.createElement('select');
      opts.forEach(o=>{const x=document.createElement('option');x.value=o;x.textContent=o;s.appendChild(x);});
      s.value=v;d.appendChild(s);return s;};
    const sp=makeSPUI(!!m.system_prompt_enabled, m.system_prompt||'');
    const name=inp('name',m.name), url=inp('http://host:port',m.url||''),
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
function addMember(){
  const d=document.createElement('div'); d.className='mrow';
  const inp=(ph,v)=>{const i=document.createElement('input');i.value=v??'';i.placeholder=ph||'';d.appendChild(i);return i;};
  const sel=(opts,v)=>{const s=document.createElement('select');
    opts.forEach(o=>{const x=document.createElement('option');x.value=o;x.textContent=o;s.appendChild(x);});
    s.value=v;d.appendChild(s);return s;};
  const sp=makeSPUI(false,'');
  const name=inp('name'), url=inp('http://host:port'),
        temp=inp('temp',0.7),
        role=sel(['worker','judge','alt_judge','planner'],'worker'),
        rsn=sel(['auto','on','off'],'auto'),
        rstyle=sel(['chat_template_kwargs','enable_thinking','thinking_type','reasoning_effort','none'],
                   'chat_template_kwargs'),
        enb=sel(['true','false'],'false');
  d.appendChild(sp.btn);
  const rm=inp('x','');
  rm.placeholder='remove'; rm.title='Click to remove this member';
  rm.className='rm'; rm.onclick=()=>removeMember(d);
  enb.dataset.role='toggle';
  d._fields={name,url,temp,role,rsn,rstyle,enb,rm,spTa:sp.ta,spBtn:sp.btn};
  d._spsec=sp.sec;
  const mrow=document.getElementById('members');
  mrow.appendChild(d);
  mrow.appendChild(sp.sec);
  name.focus();
}
function removeMember(d){
  if(d._spsec && d._spsec.parentNode) d._spsec.remove();
  d.remove();
  const jSel=document.getElementById('judge');
  // keep judge options in sync with remaining members
  jSel.innerHTML='';
  [...document.querySelectorAll('#members .mrow')].forEach(r=>{
    const o=document.createElement('option');
    o.value=r._fields.name.value.trim(); o.textContent=o.value||'(unnamed)';
    jSel.appendChild(o);
  });
}

function collect(){
  const members=[...document.querySelectorAll('#members .mrow')].map(d=>{
    const f=d._fields;
    return {name:f.name.value.trim(),url:f.url.value.trim(),temperature:f.temp.value,
            role:f.role.value,
            reasoning:f.rsn.value,reasoning_style:f.rstyle.value,
            enabled:f.enb.value==='true',
            system_prompt:f.spTa?f.spTa.value:'',
            system_prompt_enabled:f.spBtn?f.spBtn.dataset.on==='true':false};
  }).filter(m=>m.name);  // drop blank rows left from an unfilled Add Member
  return {serve:{mode:document.getElementById('mode').value,
                 reasoning:document.getElementById('reasoning').value,
                 judge:document.getElementById('judge').value,
                 judge_prompt:document.getElementById('jprompt').value,
                 port:+document.getElementById('port').value,
                 member_timeout:+document.getElementById('member_timeout').value||0},
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


