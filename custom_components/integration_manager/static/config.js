let DOM=new URLSearchParams(location.search).get('domain')||'', ST=null, flow=null;

async function load(){
  ST=await (await fetch('api/status')).json();
  DOM=ST.integration||'';  // one integration per container
  $('#domnote').innerHTML=DOM?'the integration of this container (several versions of it can be in the store, one runs)':'no integration installed yet: <a href="/install">Install</a> one';
  const x=(ST.installed||{})[DOM]; $('#domtitle').textContent=DOM||'no integration';
  $('#runstate').innerHTML=x?(x.running?`<span class="ok">running ${esc(x.running_tag||'')}</span>`:'<span class="mut">stopped</span>'):'';
  renderVersions(x); loadReleases(); loadChanges(); loadPatches(); loadYaml(); flowNote(x); entries();
}
function renderVersions(x){
  const t=$('#vers'); t.querySelectorAll('tr:not(:first-child)').forEach(e=>e.remove()); if(!x) return;
  const tags=Object.keys(x.versions||{}).sort().reverse();
  for(const v of tags){const info=x.versions[v]||{}; const running=x.running&&x.running_tag===v; const tr=document.createElement('tr');
    tr.innerHTML=`<td><b>${esc(v)}</b>${x.previous_tag===v?' <span class="tag">previous</span>':''}</td><td>${esc(info.version||'')}</td><td class="mut">${esc((info.installed_at||'').slice(0,16))}</td><td>${running?'<span class="ok">running</span>':x.running_tag===v?'<span class="mut">deployed, stopped</span>':''}</td>
      <td>${running?`<button data-a="stop">Stop</button>`:`<button data-a="start" data-v="${esc(v)}">${x.running?'Switch to':'Start'} ${esc(v)}</button> <button data-a="remove" data-v="${esc(v)}">Remove</button>`}</td>`;
    t.appendChild(tr);}
  $('#rollbackfull').disabled=!(x.previous_tag&&x.pre_update_backup); $('#vermsg').textContent=x.pre_update_backup?`pre-update backup: ${x.pre_update_backup}`:'';
  t.querySelectorAll('button').forEach(b=>b.onclick=async()=>{const a=b.dataset.a, v=b.dataset.v;
    if(a==='start'){ if(!confirm(`${x.running?'Switch to':'Start'} ${DOM} ${v}? A backup is taken first when switching; requirements are installed, patches applied, entries enabled.`)) return; log('starting…'); const r=await post('api/run/start',{domain:DOM,tag:v}); log(r.ok?`started ${r.tag}${r.restart_required?' — RESTART REQUIRED (new code for an already loaded integration): use Restart on the manager page':''}${r.pip_failed&&r.pip_failed.length?' · pip failed: '+r.pip_failed.join(', '):''}`:'ERROR: '+r.error); load(); }
    if(a==='stop'){ if(!confirm(`Stop ${DOM}?`)) return; const r=await post('api/run/stop'); log(r.ok?'stopped':'ERROR: '+r.error); load(); }
    if(a==='remove'){ if(!confirm(`Remove ${DOM} ${v} from the version store?`)) return; const r=await post(`api/installed/${DOM}/remove_version`,{tag:v}); log(r.ok?'removed':'ERROR: '+r.error); load(); }
  });
}
$('#relrefresh').onclick=()=>{$('#relrefresh').disabled=true; loadReleases(true).finally(()=>{$('#relrefresh').disabled=false;});};
$('#rollbackfull').onclick=async()=>{ if(!confirm(`Full rollback of ${DOM}: start the previous version AND schedule the pre-update backup for the restart that follows?`)) return; const r=await post(`api/installed/${DOM}/rollback_full`); if(!r.ok){log('ERROR: '+r.error);return;} const rr=await post('api/restart'); if(!rr.ok){log('ERROR: '+rr.error);return;} log(`back to ${r.tag}; restoring ${r.restore} at restart…`); setTimeout(()=>location.reload(),10000); };
async function loadReleases(force){
  const t=$('#rels'); t.querySelectorAll('tr:not(:first-child)').forEach(e=>e.remove()); $('#relprev').textContent=''; if(!DOM) return;
  let r=[]; try{ r=await (await fetch('api/releases?domain='+encodeURIComponent(DOM)+(force?'&refresh=1':''),{headers:{'X-Requested-With':'fetch'}})).json(); }catch(e){ return; }
  if(!Array.isArray(r)){ $('#relprev').innerHTML='<span class="bad">releases: '+esc(r.message||'error')+'</span>'; return; }
  $('#relchecked').textContent=r.length&&r[0].checked_at?'· checked '+r[0].checked_at.slice(11,16)+' (cached 5 min)':'';
  for(const x of r){const tr=document.createElement('tr');
    tr.innerHTML=`<td>${esc(x.tag)}${x.installed?' <span class="tag ok">installed</span>':''}${x.running?' <span class="tag ok">running</span>':''}</td><td>${x.prerelease?'<span class="warn">prerelease</span>':'<span class="ok">stable</span>'}</td><td>${esc(x.published)}</td>
      <td><button data-prev="${esc(x.tag)}">What changes</button> <button data-pf="${esc(x.tag)}" title="unpack the release in a scratch dir, resolve its requirements with pip --dry-run, evaluate every patch against the new code, check dependencies and the minimum HA version; nothing is installed">Preflight</button> <button data-tag="${esc(x.tag)}">${x.installed?'Reinstall':'Install'}</button></td>`; t.appendChild(tr);}
  t.querySelectorAll('button[data-pf]').forEach(b=>b.onclick=()=>runPreflight(b.dataset.pf));
  t.querySelectorAll('button[data-tag]').forEach(b=>b.onclick=async()=>{const tag=b.dataset.tag; if(!confirm(`Install ${DOM} ${tag} into the version store?`)) return; log(`installing ${tag}…`); const res=await post('api/install',{domain:DOM,tag}); log(res.ok?`installed ${res.tag} (version ${res.version||'?'})${res.redeployed?' — running version refreshed, restart required':''}`:'ERROR: '+res.error); load();});
  t.querySelectorAll('button[data-prev]').forEach(b=>b.onclick=async()=>{const tag=b.dataset.prev; $('#relprev').textContent='loading…'; showPrev();
    const p=await (await fetch(`api/releases/preview?domain=${encodeURIComponent(DOM)}&tag=${encodeURIComponent(tag)}`)).json();
    if(!p.ok){$('#relprev').textContent='ERROR: '+p.error;return;}
    $('#relprev').innerHTML=`<b>${esc(tag)}</b> vs ${esc(p.compared_to||'nothing running')}: version ${esc(p.installed_version||'—')} → ${esc(p.new_version||'?')}${p.min_ha_version?' · needs HA ≥ '+esc(p.min_ha_version):''}<br>requirements added: ${p.requirements_added.map(esc).join(', ')||'none'}; removed: ${p.requirements_removed.map(esc).join(', ')||'none'}; unchanged: ${p.requirements_unchanged.length}<br>dependencies: ${p.dependencies.map(esc).join(', ')||'none'}${p.after_dependencies.length?' · after: '+p.after_dependencies.map(esc).join(', '):''}${p.notes?`<details style="margin-top:6px"><summary class="mut" style="cursor:pointer">release notes</summary><pre>${esc(p.notes)}</pre></details>`:''}`;
    const notes=$('#relprev details'); if(notes) notes.open=true; showPrev();});
}
// "What changes": bring the comparison into view (the releases table can be long)
function showPrev(){ const el=$('#relprev'); if(el) el.scrollIntoView({behavior:'smooth',block:'start'}); }
async function runPreflight(tag){
  const box=$('#preflight'); box.innerHTML=`<span class="mut">preflight of ${esc(tag)}: downloading, resolving requirements with pip (up to a few minutes)…</span>`;
  let r; try{ r=await post('api/releases/preflight',{domain:DOM,tag}); }catch(e){ box.innerHTML='<span class="bad">preflight failed: '+esc(e)+'</span>'; return; }
  if(!r.ok){ box.innerHTML='<span class="bad">preflight: '+esc(r.error)+'</span>'; return; }
  box.innerHTML=renderPreflight(r.report);
}
async function loadChanges(){
  let r; try{ r=await (await fetch('api/change_reports')).json(); }catch(e){ return; }
  const p=r.pending; $('#chgpending').textContent=p?`${p.domain} ${p.from_tag} → ${p.to_tag}: compared once the new version has run (after the smoke test)`:'';
  const reps=(r.reports||[]).filter(x=>!DOM||x.domain===DOM);
  $('#chglist').innerHTML=reps.length?reps.map((x,i)=>renderChange(x,i===0)).join(''):'<span class="mut">no version switch recorded yet</span>';
}
function renderChange(x,open){
  const list=(items,f)=>`<ul style="margin:2px 0 6px 18px">${items.map(i=>`<li>${f(i)}</li>`).join('')}</ul>`;
  const sec=(title,cls,items,f)=>items&&items.length?`<div class="${cls}" style="margin-top:4px">${title} (${items.length})</div>${list(items,f)}`:'';
  const ent=e=>`<code>${esc(e.entity_id)}</code>${e.name?' '+esc(e.name):''}`;
  const fields=f=>`<code>${esc(f.service)}</code>: ${f.fields.map(esc).join(', ')}`;
  const body=sec('entities removed','bad',x.entities_removed,ent)
    +sec('entities renamed','warn',x.entities_renamed,i=>`<code>${esc(i.from)}</code> → <code>${esc(i.to)}</code>`)
    +sec('entities changed','warn',x.entities_changed,i=>`<code>${esc(i.entity_id)}</code>: ${Object.entries(i.changes).map(([k,v])=>`${esc(k)} ${esc(v[0]??'—')} → ${esc(v[1]??'—')}`).join(', ')}`)
    +sec('services removed','bad',x.services_removed,s=>`<code>${esc(s)}</code>`)
    +sec('service fields removed','warn',x.fields_removed,fields)
    +sec('entities added','ok',x.entities_added,ent)
    +sec('services added','ok',x.services_added,s=>`<code>${esc(s)}</code>`)
    +sec('service fields added','ok',x.fields_added,fields);
  return `<details ${open?'open':''} style="margin-bottom:8px"><summary style="cursor:pointer"><b>${esc(x.from_tag)} → ${esc(x.to_tag)}</b> <span class="mut">${esc((x.at||'').replace('T',' ').slice(0,16))}</span> ${x.breaking?'<span class="tag warn">may affect the consuming side</span>':'<span class="tag ok">nothing removed or changed</span>'} <span class="mut">${x.entities_before} → ${x.entities_after} entities, ${x.services_before} → ${x.services_after} services</span></summary>${body||'<div class="mut">no difference in entities or services</div>'}</details>`;
}
async function loadPatches(){
  const t=$('#plist'); t.querySelectorAll('tr:not(:first-child)').forEach(e=>e.remove()); if(!DOM) return;
  const r=await (await fetch(`api/patches/${encodeURIComponent(DOM)}`)).json(); if(!r.ok) return;
  for(const p of r.patches){const tr=document.createElement('tr'); tr.innerHTML=`<td>${esc(p.name)}${p.bundled?' <span class="tag" title="shipped with the image (patches/<domain>/ in the repo); a user upload of the same name overrides it">bundled</span>':''}</td><td class="mut">${p.scope?p.scope.map(esc).join(', '):'all'}</td><td><span class="${p.status==='applied'||p.status==='already applied'?'ok':p.status==='skipped'?'mut':'warn'}">${esc(p.status)}</span></td><td class="mut">${esc(p.detail||'')}</td><td><button data-e="${esc(p.name)}">${p.bundled?'View / override':'Edit'}</button>${p.bundled?'':` <button data-n="${esc(p.name)}">Delete</button>`}</td>`; t.appendChild(tr);}
  t.querySelectorAll('button[data-e]').forEach(b=>b.onclick=()=>pedEdit(b.dataset.e));
  t.querySelectorAll('button[data-n]').forEach(b=>b.onclick=async()=>{ if(!confirm(`Delete patch ${b.dataset.n}?`)) return; await post(`api/patches/${encodeURIComponent(DOM)}/${encodeURIComponent(b.dataset.n)}/delete`); loadPatches(); });
  $('#papply').disabled=!(ST.running&&ST.running.domain===DOM);
}
$('#pupload').onclick=async()=>{const f=$('#pfile').files[0]; if(!DOM||!f){$('#pmsg').textContent='choose a .py or .patch file';return;}
  const fd=new FormData(); fd.append('file',f); const r=await (await fetch(`api/patches/${encodeURIComponent(DOM)}/upload`,{method:'POST',headers:{'X-Requested-With':'fetch'},body:fd})).json();
  $('#pmsg').textContent=r.ok?`uploaded ${r.name} (scope: ${r.scope?r.scope.join(', '):'all versions'}); applied when the integration starts`:'ERROR: '+r.error; loadPatches();};
// ----- patch editor -----
const PATCH_TEMPLATES={py:`# integration-version: all
# applies-to: some-lib<2.0
"""What this patch fixes, and where upstream tracks it."""

import os

MARKER = "LOCAL PATCH (my-fix)"
OLD = "    return None\\n"
NEW = "    return None  # LOCAL PATCH (my-fix)\\n"


def _target(ctx):
    return os.path.join(ctx.site_packages, "some_lib", "module.py")


def status(ctx):
    path = _target(ctx)
    if not os.path.isfile(path):
        return "absent"
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    if MARKER in src:
        return "applied"
    return "pending" if OLD in src else "not applicable"


def apply(ctx):
    state = status(ctx)
    if state != "pending":
        return "already applied" if state == "applied" else state
    path = _target(ctx)
    with open(path, encoding="utf-8") as fh:
        src = fh.read().replace(OLD, NEW, 1)
    compile(src, path, "exec")  # never leave broken Python behind
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(src)
    return "applied"
`, patch:`# integration-version: all
--- a/some_lib/module.py
+++ b/some_lib/module.py
@@ -10,3 +10,3 @@
 def value():
-    return None
+    return 0
`};
function pedShow(name,text,bundled,isNew){ PED_NEW=isNew; $('#ped').hidden=false; $('#pedname').value=name; $('#pedname').disabled=!isNew; $('#pedtext').value=text;
  $('#pednote').textContent=bundled?'bundled with the image: Save stores your copy under the same name, which takes its place':isNew?'the extension decides the format: .py module or .patch diff':'';
  $('#pedreport').innerHTML=''; $('#pedmsg').textContent=''; $('#ped').scrollIntoView({behavior:'smooth',block:'nearest'}); }
let PED_NEW=false;
async function pedEdit(name){ let r; try{ r=await (await fetch(`api/patch_editor/${encodeURIComponent(DOM)}?name=${encodeURIComponent(name)}`,{headers:{'X-Requested-With':'fetch'}})).json(); }catch(e){ r={error:String(e)}; }
  if(!r.ok){ $('#pmsg').textContent='ERROR: '+(r.error||r.message); return; } pedShow(r.name,r.text,r.bundled,false); }
$('#pnewpy').onclick=()=>{ if(DOM) pedShow('my_fix.py',PATCH_TEMPLATES.py,false,true); };
$('#pnewdiff').onclick=()=>{ if(DOM) pedShow('my_fix.patch',PATCH_TEMPLATES.patch,false,true); };
$('#pedclose').onclick=()=>{ $('#ped').hidden=true; };
function renderPatchCheck(r){
  const cls=s=>/^(applied|already applied|pending)$/.test(s)?'ok':s==='skipped'?'mut':'warn';
  let h=`<div><b>${esc(r.name)}</b> against ${esc(r.against)} · scope ${r.scope?r.scope.map(esc).join(', '):'all versions'} · status <span class="${cls(r.status)}">${esc(r.status)}</span>${r.applies?'':` <span class="mut">(${esc(r.detail)}; would be <span class="${cls(r.status_if_applied)}">${esc(r.status_if_applied)}</span>)</span>`}</div>`;
  for(const f of (r.files||[])){
    h+=`<div style="margin-top:6px"><code>${esc(f.path)}</code> → ${f.target?`<code>${esc(f.target)}</code>`:'<span class="bad">no such file in the component or its site-packages</span>'}</div>`;
    for(const x of (f.hunks||[])){
      h+=`<div style="margin:2px 0 0 12px"><code>${esc(x.header)}</code> <span class="${cls(x.state)}">${esc(x.state)}</span>${x.line?` · line ${x.line}`:''}${x.found_diff?' · closest lines:':''}</div>`;
      if(x.found_diff) h+=`<pre class="pdiff">${x.found_diff.map(l=>`<span class="${l[0]==='+'?'add':l[0]==='-'?'del':''}">${esc(l)}</span>`).join('\n')}</pre><div class="mut" style="margin-left:12px;font-size:12px">− what the hunk expects · + what the file has there now</div>`;
    }
  }
  return h;
}
$('#pedcheck').onclick=async()=>{ if(!DOM) return; $('#pedreport').innerHTML='<span class="mut">checking…</span>';
  let r; try{ r=await post(`api/patch_editor/${encodeURIComponent(DOM)}/check`,{name:$('#pedname').value.trim(),text:$('#pedtext').value}); }catch(e){ r={error:String(e)}; }
  $('#pedreport').innerHTML=r.ok?renderPatchCheck(r):`<span class="bad">${esc(r.error||r.message)}</span>`; };
$('#pedsave').onclick=async()=>{ if(!DOM) return; const name=$('#pedname').value.trim();
  let r; try{ r=await post(`api/patch_editor/${encodeURIComponent(DOM)}/save`,{name,text:$('#pedtext').value,create:PED_NEW}); }catch(e){ r={error:String(e)}; }
  $('#pedmsg').textContent=r.ok?`saved ${r.name}${r.overrides_bundled?' (overrides the bundled file)':''}; ${ST.running&&ST.running.domain===DOM?'Re-apply now applies it':'applied when the integration starts'}`:'ERROR: '+(r.error||r.message);
  if(r.ok){ PED_NEW=false; $('#pedname').disabled=true; loadPatches(); } };
$('#papply').onclick=async()=>{const r=await post(`api/patches/${encodeURIComponent(DOM)}/_all/apply`); $('#pmsg').textContent=r.ok?r.result:'ERROR: '+r.error; loadPatches();};
async function loadYaml(){ if(!DOM){$('#yamltext').value='';return;} const r=await (await fetch(`api/yaml/${encodeURIComponent(DOM)}`)).json(); if(!r.ok) return; $('#yamltext').value=r.text||''; $('#yamlmsg').textContent=r.text?(r.applied_at_boot?'applied at boot (running integration)':'stored; applied when this integration runs'):''; }
$('#yamlsave').onclick=async()=>{ const r=await post(`api/yaml/${encodeURIComponent(DOM)}`,{text:$('#yamltext').value}); $('#yamlmsg').textContent=r.ok?(r.removed?'YAML removed':`saved, ${r.keys} top-level key(s)`)+(r.restart_required?' — restart required to apply':''):'ERROR: '+r.error; };
function flowNote(x){ const running=!!(x&&x.running); $('#start').disabled=!running; $('#flownote').innerHTML=running?`Runs the integration's own config flow in-process (no HA frontend). Entries created here are enabled because <b>${esc(DOM)}</b> is running.`:`The config flow needs the integration's code loaded: <b>start ${esc(DOM||'it')}</b> first. Settings can also be imported from a Home Assistant backup on the manager page (stored disabled until started).`; }

function optionsOf(sel){ return (sel.options||[]).map(o=>typeof o==='object'?{value:o.value,label:o.label??o.value}:{value:o,label:o}); }
function field(f){
  if(f.type==='expandable'){  // a form section: its fields are sent as one object under the section's name
    const sec=document.createElement('fieldset'); sec.dataset.name=f.name; sec.dataset.kind='section'; sec.style.cssText='border:1px solid #30363d;border-radius:6px;margin:8px 0;padding:6px 10px';
    const lg=document.createElement('legend'); lg.textContent=f.name; sec.appendChild(lg);
    (Array.isArray(f.schema)?f.schema:[]).forEach(c=>sec.appendChild(field(c)));
    const e=document.createElement('div'); e.className='err'; sec._err=e; sec.appendChild(e); return sec;
  }
  const sv=(f.description||{}).suggested_value;
  const name=f.name, req=f.required?' *':'', dflt=(sv!==undefined&&sv!==null)?sv:f.default;
  const sel=f.selector||{}; const kind=Object.keys(sel)[0];
  const wrap=document.createElement('div'); wrap.dataset.name=name;
  const lab=document.createElement('label'); lab.textContent=name+req; wrap.appendChild(lab);
  let el;
  if(kind==='boolean' || f.type==='boolean'){ el=document.createElement('input'); el.type='checkbox'; el.checked=!!dflt; el.style.width='auto'; wrap.dataset.kind='boolean'; }
  else if(kind==='select' || f.options || f.type==='multi_select'){
    const raw=f.options; const opts=kind==='select'?optionsOf(sel.select):Array.isArray(raw)?raw.map(o=>Array.isArray(o)?{value:o[0],label:o[1]}:(o&&typeof o==='object')?{value:o.value,label:o.label??o.value}:{value:o,label:o}):Object.entries(raw||{}).map(([v,l])=>({value:v,label:l}));
    const mode=kind==='select'&&sel.select&&sel.select.mode;
    const vmap={}; opts.forEach(o=>{vmap[String(o.value)]=o.value;}); wrap._values=vmap;  // the HTML value is a string; send what the schema offered
    if(mode==='list'){ wrap.dataset.kind='radio'; el=document.createElement('div'); el.className='radio';
      opts.forEach(o=>{const l=document.createElement('label'); l.innerHTML=`<input type="radio" name="r_${esc(name)}" value="${esc(o.value)}" ${String(dflt)===String(o.value)?'checked':''}> ${esc(o.label)}`; el.appendChild(l);});
    }else{ el=document.createElement('select'); const multi=(kind==='select'&&sel.select&&sel.select.multiple)||f.type==='multi_select'; wrap.dataset.kind=multi?'multiselect':'select';
      if(multi){ el.multiple=true; el.size=Math.min(opts.length,8); } else if(!f.required) el.appendChild(new Option('—',''));
      const dv=Array.isArray(dflt)?dflt.map(String):[String(dflt)];
      opts.forEach(o=>{const op=new Option(o.label,o.value); if(dv.includes(String(o.value))) op.selected=true; el.appendChild(op);}); }
  }else if(kind==='number' || f.type==='integer' || f.type==='float'){
    el=document.createElement('input'); el.type='number'; wrap.dataset.kind=(f.type==='integer'||(sel.number&&sel.number.step===1))?'integer':'number';
    const n=sel.number||{}; if(n.min!=null) el.min=n.min; if(n.max!=null) el.max=n.max; el.step=n.step??'any'; if(dflt!=null) el.value=dflt;
  }else if(kind==='object'){ el=document.createElement('textarea'); wrap.dataset.kind='object'; el.value=dflt!=null?JSON.stringify(dflt,null,2):'{}'; }
  else if(kind==='text' || f.type==='string' || kind===undefined){
    const t=sel.text||{}; if(t.multiline){ el=document.createElement('textarea'); } else { el=document.createElement('input'); el.type=t.type==='password'?'password':'text'; }
    if(dflt!=null) el.value=dflt; wrap.dataset.kind='text';
  }else{ el=document.createElement('textarea'); wrap.dataset.kind='object'; el.value=dflt!=null?JSON.stringify(dflt,null,2):''; lab.textContent+=` (selector "${kind}" unsupported, JSON)`; }
  wrap._el=el; wrap.appendChild(el);  // references, not ids: field names may hold characters a selector cannot
  const e=document.createElement('div'); e.className='err'; wrap._err=e; wrap.appendChild(e);
  return wrap;
}
function collect(root){
  const out={}; root=root||$('#form');
  for(const w of root.querySelectorAll(':scope > [data-name]')){
    const n=w.dataset.name, k=w.dataset.kind, el=w._el; let v;
    if(k==='section'){ out[n]=collect(w); continue; }
    const orig=x=>(w._values&&Object.prototype.hasOwnProperty.call(w._values,x))?w._values[x]:x;
    if(k==='boolean') v=el.checked;
    else if(k==='radio'){const c=w.querySelector('input[type=radio]:checked'); if(!c) continue; v=orig(c.value);}
    else if(k==='select'){ v=el.value; if(v==='') continue; v=orig(v); }
    else if(k==='multiselect'){ v=[...el.selectedOptions].map(o=>orig(o.value)); }
    else if(k==='integer'){ if(el.value==='') continue; v=parseInt(el.value,10); }
    else if(k==='number'){ if(el.value==='') continue; v=parseFloat(el.value); }
    else if(k==='object'){ if(el.value.trim()==='') continue; v=JSON.parse(el.value); }
    else { if(el.value==='') continue; v=el.value; }
    out[n]=v;
  }
  return out;
}
function render(r){
  $('#flowid').textContent=r.flow_id?`flow ${r.flow_id.slice(0,8)}… · ${r.handler||''}`:'';
  $('#abort').disabled=!r.flow_id;
  const card=$('#stepcard'); card.hidden=false;
  $('#form').innerHTML=''; $('#baseerr').textContent=''; $('#desc').textContent=''; $('#submit').dataset.external='';
  if(r.type==='form'){
    $('#steptitle').textContent=`Step: ${r.step_id}`+(r.last_step===true?' (last)':'');
    if(r.description_placeholders) $('#desc').textContent=JSON.stringify(r.description_placeholders,null,1);
    (Array.isArray(r.data_schema)?r.data_schema:[]).forEach(f=>$('#form').appendChild(field(f)));
    const errs=r.errors||{}; for(const [k,v] of Object.entries(errs)){ if(k==='base') $('#baseerr').textContent=v; else {const w=[...$('#form').querySelectorAll('[data-name]')].find(x=>x.dataset.name===k); if(w&&w._err) w._err.textContent=typeof v==='string'?v:JSON.stringify(v);} }
    $('#submit').hidden=false; $('#submit').textContent='Submit';
  }else if(r.type==='menu'){
    $('#steptitle').textContent=`Menu: ${r.step_id}`; const w=document.createElement('div'); w.className='radio'; w.dataset.name='next_step_id'; w.dataset.kind='radio';
    const mo=r.menu_options||[]; const opts=Array.isArray(mo)?mo.map(o=>[String(o),String(o)]):Object.entries(mo).map(([k,v])=>[k,String(v??k)]);  // HA allows a list of step ids or {step_id: label}
    opts.forEach(([v,lab],i)=>{const l=document.createElement('label');l.innerHTML=`<input type="radio" name="r_next_step_id" value="${esc(v)}" ${i===0?'checked':''}> ${esc(lab)}`;w.appendChild(l);});
    $('#form').appendChild(w); $('#submit').hidden=false; $('#submit').textContent='Continue';
  }else if(r.type==='create_entry'){
    $('#steptitle').innerHTML=`<span class="ok">Entry created</span>: ${esc(r.title||'')}`; $('#desc').textContent=(r.manager_note?r.manager_note+'\n':'')+`entry_id: ${r.result?.entry_id||r.entry_id||'?'}`; $('#submit').hidden=true; flow=null; $('#abort').disabled=true; entries(); load();
  }else if(r.type==='abort'){
    $('#steptitle').innerHTML=`<span class="warn">Aborted</span>: ${esc(r.reason||'')}`; $('#desc').textContent=JSON.stringify(r.description_placeholders||{}); $('#submit').hidden=true; flow=null; $('#abort').disabled=true;
  }else if(r.type==='progress'){
    // the integration works in the background (discovery, pairing, a login): HA
    // advances the flow when its task finishes; ask for the current step until
    // it is no longer a progress step
    $('#steptitle').textContent=`In progress: ${r.step_id}${r.progress_action?' · '+r.progress_action:''}`;
    $('#desc').textContent=(r.description_placeholders?JSON.stringify(r.description_placeholders,null,1)+'\n':'')+'waiting for the integration… (checked every 2 s; Abort stops it)';
    $('#submit').hidden=true; pollProgress();
  }else if(r.type==='progress_done'){
    // older progress API (async_show_progress without a task): HA hands back
    // progress_done and expects the client to configure the flow again
    $('#steptitle').textContent=`Progress done: ${r.step_id||''} · continuing…`; $('#submit').hidden=true; pollProgress(0);
  }else if(r.type==='external'){
    // a login or authorisation on another site: open it, finish there, then continue the flow
    $('#steptitle').textContent=`External step: ${r.step_id||''}`;
    $('#desc').innerHTML=/^https?:\/\//.test(r.url||'')?`Open <a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.url)}</a>, finish there, then click Continue.`:'Finish the step outside this page, then click Continue.';
    $('#submit').hidden=false; $('#submit').textContent='Continue'; $('#submit').dataset.external='1';
  }else if(r.type==='external_done'){
    $('#steptitle').textContent='External step done · continuing…'; $('#submit').hidden=true; pollProgress(0);
  }else{ $('#steptitle').textContent=`Result: ${r.type}`; $('#desc').textContent=JSON.stringify(r,null,1); $('#submit').hidden=true; }
}
let PROGRESS_T=null;
function pollProgress(delay=2000){
  clearTimeout(PROGRESS_T);
  PROGRESS_T=setTimeout(async()=>{ if(!flow) return;
    try{ const url=flow.kind==='config'?`api/flow/${flow.id}`:`api/options/${flow.id}`; const r=await post(url,{user_input:null});
      if(r.message){ $('#baseerr').textContent=r.message; return; }
      if(r.type==='progress'){ $('#steptitle').textContent=`In progress: ${r.step_id}${r.progress_action?' · '+r.progress_action:''}`; pollProgress(); } else render(r);
    }catch(e){ $('#baseerr').textContent='progress check failed: '+e.message; }
  },delay);
}
$('#start').onclick=async()=>{try{log(`start flow ${DOM}`);const r=await post('api/flow/start',{domain:DOM});if(r.message){log('error: '+r.message);return;}flow={id:r.flow_id,kind:'config'};render(r);}catch(e){log('error: '+e.message)}};
$('#submit').onclick=async()=>{ if(!flow) return; let input; try{input=$('#submit').dataset.external==='1'?null:collect();}catch(e){$('#baseerr').textContent='invalid JSON: '+e.message;return;}
  try{ const url=flow.kind==='config'?`api/flow/${flow.id}`:`api/options/${flow.id}`; const r=await post(url,{user_input:input}); if(r.type==='invalid_data'){ for(const [k,v] of Object.entries(r.errors||{})){ const w=[...$('#form').querySelectorAll('[data-name]')].find(x=>x.dataset.name===k); if(w&&w._err) w._err.textContent=String(v); else $('#baseerr').textContent=`${k}: ${v}`; } return; } if(r.message){$('#baseerr').textContent=r.message;return;} render(r);}catch(e){log('error: '+e.message)} };
$('#abort').onclick=async()=>{ if(!flow) return; clearTimeout(PROGRESS_T); await del(flow.kind==='config'?`api/flow/${flow.id}`:`api/options/${flow.id}`); log('aborted'); flow=null; $('#stepcard').hidden=true; $('#abort').disabled=true; $('#flowid').textContent=''; };
async function entries(){
  const r=await (await fetch('api/entries')).json();
  const t=$('#entries'); t.querySelectorAll('tr:not(:first-child)').forEach(e=>e.remove());
  for(const e of r){ if(DOM&&e.domain!==DOM) continue; const tr=document.createElement('tr');
    tr.innerHTML=`<td>${esc(e.domain)}</td><td>${esc(e.title)}</td><td class="${e.state==='loaded'?'ok':(e.disabled_by?'mut':'warn')}">${e.disabled_by?'disabled ('+esc(e.disabled_by)+')':esc(e.state)}</td><td>${esc(e.version)}</td>
      <td><button data-a="options" data-id="${esc(e.entry_id)}" ${e.supports_options&&e.state==='loaded'?'':'disabled'}>Options</button> ${e.supports_reconfigure?`<button data-a="reconfigure" data-id="${esc(e.entry_id)}">Reconfigure</button> `:''}<button data-a="reload" data-id="${esc(e.entry_id)}">Reload</button> <button data-a="delete" data-id="${esc(e.entry_id)}">Delete</button></td>`;
    t.appendChild(tr); }
  t.querySelectorAll('button').forEach(b=>b.onclick=async()=>{const a=b.dataset.a,id=b.dataset.id;
    if(a==='delete'){ if(!confirm('Delete this config entry?')) return; await post(`api/entries/${id}/delete`,{}); return entries(); }
    if(a==='reload'){ await post(`api/entries/${id}/reload`,{}); return entries(); }
    if(a==='options'){ const r=await post(`api/entries/${id}/options`,{}); if(r.message||!r.flow_id){log('error: '+(r.message||'no flow'));return;} flow={id:r.flow_id,kind:'options'}; render(r); }
    if(a==='reconfigure'){ const r=await post('api/flow/start',{domain:DOM,source:'reconfigure',entry_id:id}); if(r.message||!r.flow_id){log('error: '+(r.message||r.reason||'no flow'));return;} flow={id:r.flow_id,kind:'config'}; render(r); }
    if(a==='continue'){ const r=await post(`api/flow/${id}`,{user_input:null}); if(r.message||!r.type){log('error: '+(r.message||'no flow'));return;} flow={id,kind:'config'}; render(r); } });
  let prog=[]; try{ prog=await (await fetch('api/flow/progress')).json(); }catch(e){}
  const pb=$('#flowsprogress'); if(!pb) return; pb.innerHTML='';
  for(const f of (Array.isArray(prog)?prog:[]).filter(f=>f.handler===DOM&&f.source!=='user')){ const b=document.createElement('button'); b.textContent=`Continue ${f.source||'flow'}${f.step_id?' · '+f.step_id:''}`; b.onclick=async()=>{ const r=await post(`api/flow/${f.flow_id}`,{user_input:null}); if(r.message){log('error: '+r.message);return;} flow={id:f.flow_id,kind:'config'}; render(r); }; pb.appendChild(b); pb.appendChild(document.createTextNode(' ')); }
}
load().catch(e=>log('error: '+e.message));
