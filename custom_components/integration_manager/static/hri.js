// shared helpers (loaded synchronously at the top of <body>) + the top bar
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function post(url,body){const r=await fetch(url,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body||{})});return r.json();}
async function del(url){const r=await fetch(url,{method:'DELETE'});return r.json();}
const log=m=>{console.log(m); const el=$('#flash'); if(el){el.textContent=m; clearTimeout(el._t); el._t=setTimeout(()=>{el.textContent=''},8000);}};
function chipBar(sel,counts,on,after){ $(sel).innerHTML=Object.keys(counts).sort().map(k=>`<span class="tag ${on.has(k)?'on':''}" data-k="${esc(k)}">${esc(k)} ${counts[k]}</span>`).join('');
 document.querySelectorAll(sel+' .tag').forEach(t=>t.onclick=()=>{const k=t.dataset.k;on.has(k)?on.delete(k):on.add(k);after();}); }

// top bar chips + the integration-specific log-files item
document.addEventListener('DOMContentLoaded',()=>{
(async()=>{try{
 const s=await fetch('/api/summary').then(r=>r.json());
 const r=s.running, h=s.health||'stopped', m=s.mqtt||{};
 const el=document.getElementById('tb-chips'); if(!el) return;
 el.innerHTML=(r?`<span class="chip ${r.loaded?'ok':'warn'}"><span class="dot"></span><b>${esc(r.domain)}</b> ${esc(r.running_tag||'')}</span>`:'<span class="chip"><span class="dot"></span>nothing running</span>')
  +`<span class="chip ${h==='ok'?'ok':h==='degraded'?'warn':h==='stopped'?'':'bad'}"><span class="dot"></span>health <b>${esc(h)}</b></span>`
  +`<span class="chip ${m.enabled?(m.connected?'ok':'bad'):''}"><span class="dot"></span>MQTT <b>${m.enabled?(m.connected?'connected':'disconnected'):'off'}</b></span>`
  +(s.restart_required?'<span class="chip warn"><span class="dot"></span><b>restart required</b></span>':'')
  +(s.notifications?`<a class="chip warn" href="/" style="text-decoration:none" title="persistent notifications of the integration"><span class="dot"></span><b>${s.notifications}</b> notification${s.notifications>1?'s':''}</a>`:'');
}catch(e){}})();
(async()=>{try{
 // the log-files item is integration-specific: show it only when the running
 // integration writes log files, labelled with the newest one
 const a=document.getElementById('nav-logfiles'); if(!a) return;
 const files=await fetch('/api/log_files').then(r=>r.json());
 if(!Array.isArray(files)||!files.length) return;
 const f=files[0]; const base=f.name.split('/').pop();
 a.textContent=base+(files.length>1?' +'+(files.length-1):''); a.title=`log files written by the running integration (${files.length})${f.active?', active':''}`; a.hidden=false;
}catch(e){}})();
});

// preflight report (Config page "Preflight", Build page "Check")
function renderPreflight(p){
  const v=p.versions||{};
  const head=`<div class="${p.ok?'ok':'bad'}" style="font-weight:600">${p.ok?'✓ nothing blocks':'✗ blocked'}: ${esc(p.domain)} ${esc(p.ref)} (version ${esc(v.new||'?')}${v.running?', running '+esc(v.running)+' '+esc(v.running_tag||''):''}) on Home Assistant ${esc(p.target_ha)}${v.min_ha?' · needs ≥ '+esc(v.min_ha):''} · ${p.duration_s}s</div>`;
  const list=(items,cls)=>items.length?`<ul style="margin:4px 0 4px 18px">${items.map(x=>`<li class="${cls}">${esc(x)}</li>`).join('')}</ul>`:'';
  const reqs=(p.requirements||[]).length?`<table style="margin-top:6px"><tr><th>requirement</th><th>installed</th><th>after</th><th>action</th></tr>${p.requirements.map(r=>`<tr><td>${esc(r.requirement)}${r.from_dependency?' <span class="tag">dependency</span>':''}</td><td class="mut">${esc(r.installed||'—')}</td><td>${esc(r.after||'—')}</td><td class="${r.action==='unchanged'?'mut':'warn'}">${esc(r.action)}</td></tr>`).join('')}</table>`:'<div class="mut">no python requirements</div>';
  const extra=(p.also_installed||[]).length?`<div class="mut" style="margin-top:4px">pip would also bring: ${p.also_installed.map(x=>esc(x.name+' '+x.version)).join(', ')}</div>`:'';
  const pipErr=p.pip_error?`<pre class="bad" style="white-space:pre-wrap">${esc(p.pip_error)}</pre>`:'';
  const pat=(p.patches||[]).length?`<table style="margin-top:6px"><tr><th>patch</th><th>against the new code</th><th>after the update (applies-to)</th><th>detail</th></tr>${p.patches.map(x=>`<tr><td>${esc(x.name)}${x.bundled?' <span class="tag">bundled</span>':''}</td><td class="${x.status==='pending'||x.status==='applied'?'ok':x.status==='skipped'?'mut':'warn'}">${esc(x.status)}</td><td class="${x.after_update==='applies'?'ok':x.after_update==='skipped'?'mut':''}">${esc(x.after_update)}</td><td class="mut">${esc(x.detail||'')}</td></tr>`).join('')}</table>`:'<div class="mut">no patches</div>';
  const deps=(p.dependencies||[]).length?p.dependencies.map(d=>`<span class="tag ${d.found?'ok':'bad'}">${esc(d.domain)}${d.found?'':' missing'}</span>`).join(' '):'<span class="mut">none</span>';
  const c=p.config||{};
  const repl=p.replaces?`<div class="warn" style="margin-top:4px">prepares a different integration: <b>${esc(p.replaces)}</b> is replaced (after a backup)</div>`:'';
  return `${head}${repl}${list(p.blockers||[],'bad')}${list(p.warnings||[],'warn')}
   <details open style="margin-top:6px"><summary class="mut" style="cursor:pointer">requirements (pip --dry-run in this venv)</summary>${reqs}${extra}${pipErr}</details>
   <details open style="margin-top:6px"><summary class="mut" style="cursor:pointer">patches</summary>${pat}</details>
   <details style="margin-top:6px"><summary class="mut" style="cursor:pointer">configuration &amp; dependencies</summary><div style="margin-top:4px">config flow: <b>${c.config_flow?'yes':'no'}</b> · YAML stored here: <b>${c.yaml_config_present?'yes':'no'}</b> · config entries here: <b>${c.entries_here??0}</b> · type ${esc(c.integration_type||'—')} · iot_class ${esc(c.iot_class||'—')}<br>dependencies: ${deps}</div></details>`;
}
