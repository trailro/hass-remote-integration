// shared helpers (loaded synchronously at the top of <body>) + the top bar
// with a password set, an expired session (or a changed password) answers 401: back to the login page
const _hriFetch=window.fetch.bind(window);
window.fetch=async(...a)=>{const r=await _hriFetch(...a); if(r.status===401&&location.pathname!=='/login') location.href='/login?next='+encodeURIComponent(location.pathname+location.search); return r;};
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// X-Requested-With on every call: the endpoints that run or store code (the patch editor) refuse a request without it.
// A body that is not JSON (a proxy's or aiohttp's plain-text 500) comes back as an error object: r.json() threw, and
// callers that await without a catch left their buttons disabled and their "checking…" text up for good
async function _answer(r){const text=await r.text(); try{return JSON.parse(text);}catch(e){const error=`HTTP ${r.status}: ${text.trim().slice(0,200)||r.statusText||'no answer'}`; return {ok:false,error,message:error};}}
async function post(url,body){return _answer(await fetch(url,{method:'POST',headers:{'content-type':'application/json','X-Requested-With':'fetch'},body:JSON.stringify(body||{})}));}
async function del(url){return _answer(await fetch(url,{method:'DELETE',headers:{'X-Requested-With':'fetch'}}));}
const log=m=>{console.log(m); const el=$('#flash'); if(el){el.textContent=m; clearTimeout(el._t); el._t=setTimeout(()=>{el.textContent=''},8000);}};
// a fetch with the header the endpoint requires, saved under the server's file name or `fallback` (a plain link
// cannot send it); a refusal (a probe already running: 429) is shown, not saved
async function fetchDownload(b,path,label,fallback){b.disabled=true; try{
  const r=await fetch(path,{headers:{'X-Requested-With':'fetch'}});
  if(!r.ok){const j=await r.json().catch(()=>({})); alert(label+': '+(j.message||('HTTP '+r.status))); return;}
  const name=((r.headers.get('Content-Disposition')||'').match(/filename="?([^";]+)"?/)||[])[1]||fallback;
  const url=URL.createObjectURL(await r.blob()); const a=document.createElement('a'); a.href=url; a.download=name;
  document.body.appendChild(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(url),30000);
 }catch(err){alert(label+': '+err);}finally{b.disabled=false;}}
function chipBar(sel,counts,on,after){ $(sel).innerHTML=Object.keys(counts).sort().map(k=>`<span class="tag ${on.has(k)?'on':''}" data-k="${esc(k)}">${esc(k)} ${counts[k]}</span>`).join('');
 document.querySelectorAll(sel+' .tag').forEach(t=>t.onclick=()=>{const k=t.dataset.k;on.has(k)?on.delete(k):on.add(k);after();}); }

// a newer hass-remote-integration: a banner under the top bar with the release notes of every newer release;
// hidden until a release newer than the dismissed one appears
function releaseBanner(mu){
 const rel=(mu&&mu.releases)||[]; if(!rel.length||document.getElementById('hri-banner')) return;
 const top=rel[0].tag; let dismissed=''; try{dismissed=localStorage.getItem('hri-banner-dismissed')||'';}catch(e){}
 if(dismissed===top) return;
 const nav=document.querySelector('nav.topbar'); if(!nav) return;
 // only http(s): a javascript: or data: URL in the release data must not become a link that runs in this origin
 const notes=rel.map(r=>`<a href="${esc(/^https?:\/\//i.test(r.url||'')?r.url:'#')}" target="_blank" rel="noopener" title="${esc(r.name)}${r.published_at?' · '+esc(r.published_at.slice(0,10)):''}">${esc(r.tag)}</a>`).join('');
 nav.insertAdjacentHTML('afterend',`<div class="hri-banner" id="hri-banner" role="status"><span class="msg">hass-remote-integration <b>${esc(rel[0].version)}</b> is available`
  +` (this container runs ${esc(mu.installed)}${rel.length>1?`, ${rel.length} newer releases`:''}).</span><span class="notes">Release notes: ${notes}</span>`
  +`<a class="how" href="https://github.com/trailro/hass-remote-integration#updating-hass-remote-integration" target="_blank" rel="noopener">How to update</a>`
  +`<button type="button" class="x" title="hide until a newer release is out" aria-label="hide">×</button></div>`);
 document.querySelector('#hri-banner .x').onclick=()=>{try{localStorage.setItem('hri-banner-dismissed',top);}catch(e){} document.getElementById('hri-banner').remove();};
}

// start or switch the integration: the server runs the preflight first; blockers ask before starting anyway
async function startIntegration(body){
 let r=await post('api/run/start',body);
 if(!r.ok&&r.needs_force){
  const b=(r.preflight&&r.preflight.blockers)||[];
  if(!confirm(`Preflight of ${body.domain} ${body.tag} found ${b.length} blocker${b.length===1?'':'s'}:\n\n- ${b.join('\n- ')}\n\nStart it anyway? A backup is taken first, and the smoke test rolls it back if it is unhealthy.`))
   return {ok:false,cancelled:true,error:'not started (preflight blockers)'};
  r=await post('api/run/start',{...body,force:true});
 }
 return r;
}

// a logout the volume could not record still ended every session, until a restart: said before leaving the page
async function logout(e){e.preventDefault(); const r=await post('/api/logout'); if(!r.ok) alert(r.error||r.message||'log out failed'); location.href='/login';}

// top bar chips + the integration-specific log-files item
document.addEventListener('DOMContentLoaded',()=>{
(async()=>{try{
 const s=await fetch('/api/summary').then(r=>r.json());
 try{releaseBanner(s.manager_update);}catch(e){}
 const r=s.running, h=s.health||'stopped', m=s.mqtt||{};
 const el=document.getElementById('tb-chips'); if(!el) return;
 el.innerHTML=(r?`<span class="chip ${r.loaded?'ok':'warn'}"><span class="dot"></span><b>${esc(r.domain)}</b> ${esc(r.running_tag||'')}</span>`:'<span class="chip"><span class="dot"></span>nothing running</span>')
  +`<span class="chip ${h==='ok'?'ok':h==='degraded'?'warn':h==='stopped'?'':'bad'}"><span class="dot"></span>health <b>${esc(h)}</b></span>`
  +`<span class="chip ${m.enabled?(m.connected?'ok':'bad'):''}"><span class="dot"></span>MQTT <b>${m.enabled?(m.connected?'connected':'disconnected'):'off'}</b></span>`
  +(s.restart_required?'<span class="chip warn"><span class="dot"></span><b>restart required</b></span>':'')
  +(s.notifications?`<a class="chip warn" href="/" style="text-decoration:none" title="persistent notifications of the integration"><span class="dot"></span><b>${s.notifications}</b> notification${s.notifications>1?'s':''}</a>`:'');
 if(s.auth){ el.insertAdjacentHTML('beforeend','<a class="chip" href="#" id="tb-logout" title="end this browser session" style="text-decoration:none">log out</a>'); document.getElementById('tb-logout').onclick=logout; }
}catch(e){}})();
(async()=>{try{
 // the log-files item is integration-specific: show it only when the running
 // integration writes log files, labelled with the newest one
 const a=document.getElementById('nav-logfiles'); if(!a) return;
 const files=await fetch('/api/log_files',{headers:{'X-Requested-With':'fetch'}}).then(r=>r.json());
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
