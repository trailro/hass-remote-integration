let REG={}, RUN=null, INSTALLED={};
async function sysStatus(){
  const s=await (await fetch('api/status',{headers:{'X-Requested-With':'fetch'}})).json(); REG=s.registry||{}; RUN=s.running; INSTALLED=s.installed||{};
  if(IMS) imRender();
  $('#restart').disabled=s.busy; $('#restartnote').textContent=s.state.restart_required?'restart required (new code for an already loaded integration)':'';
  $('#last').textContent=s.state.last_action?('last action: '+s.state.last_action):'';
  wdRender(s.watchdog);
}
// what the watchdog last did, what it is waiting for and where it stopped: one line under the setting
function wdRender(w){ if(!w) return; const p=[];
  if(w.gave_up) p.push('<span class="warn">given up: '+esc(w.gave_up)+'</span>');
  else if(w.pending) p.push(`<span class="warn">${esc(w.pending.state||'error')} for ${Math.round(w.pending.bad_for_s/60)} min of ${Math.round(w.pending.window_s/60)}; next step: ${esc(w.pending.next||'restart')}</span>`);
  if(w.last_reload) p.push(`last reload ${esc(w.last_reload.at)} after ${Math.round((w.last_reload.unhealthy_s||0)/60)} min ${esc(w.last_reload.state||'')} (${esc(w.last_reload.reason||'')}): ${esc(w.last_reload.result||'')}`);
  if(w.last) p.push(`last action ${esc(w.last.at)}: restarted after ${Math.round((w.last.unhealthy_s||0)/60)} min of ${esc(w.last.state||'error')} (${esc(w.last.reason||'')}), attempt ${w.last.attempt} — ${esc(w.last.next||'')}`);
  if(w.reloads_24h) p.push(`${w.reloads_24h} automatic reload${w.reloads_24h>1?'s':''} in the last 24 h (max ${w.max_reloads_per_day})`);
  if(w.restarts_24h) p.push(`${w.restarts_24h} automatic restart${w.restarts_24h>1?'s':''} in the last 24 h (max ${w.max_per_day})`);
  $('#wdlast').innerHTML=p.join(' · ');
}
$('#wd').onchange=$('#wdd').onchange=$('#wdafter').onchange=$('#wdint').onchange=$('#wdday').onchange=async()=>{
  const r=await post('api/settings',{watchdog:$('#wd').checked,watchdog_on_degraded:$('#wdd').checked,watchdog_after_min:parseInt($('#wdafter').value,10)||15,
    watchdog_min_interval_min:parseInt($('#wdint').value,10)||60,watchdog_max_per_day:parseInt($('#wdday').value,10)||3});
  $('#wdmsg').textContent=r.ok?(r.watchdog?`on: after ${r.watchdog_after_min} min of error${r.watchdog_on_degraded?' or degraded':''}, reload first, at most ${r.watchdog_max_per_day} restarts/day`:'off'):'ERROR: '+r.error; sysStatus();};
$('#smoke').onchange=$('#autorb').onchange=$('#relcheck').onchange=async()=>{const r=await post('api/settings',{smoke_test_s:parseInt($('#smoke').value,10)||0,auto_rollback:$('#autorb').checked,release_check:$('#relcheck').checked}); $('#automsg').textContent=r.ok?'saved':'ERROR: '+r.error;};
$('#relnow').onclick=async()=>{$('#relnow').disabled=true; $('#automsg').textContent='checking…'; const r=await post('api/updates/check'); $('#automsg').textContent=r.ok?(Object.keys(r.updates).length?'updates: '+Object.entries(r.updates).map(([d,t])=>d+' → '+t).join(', '):'everything up to date'):'ERROR: '+r.error; $('#relnow').disabled=false; sysStatus();};
async function ghState(){const r=await (await fetch('api/settings')).json(); $('#ghstate').textContent=r.github_token_set?'set':'not set'; if(document.activeElement!==$('#allowedhosts')) $('#allowedhosts').value=r.allowed_hosts||'';}
$('#allowedsave').onclick=async()=>{const r=await post('api/settings',{allowed_hosts:$('#allowedhosts').value}); $('#ghmsg').textContent=r.ok?'allowed hosts saved':'ERROR: '+r.error;};
$('#ghsave').onclick=async()=>{const t=$('#ghtoken').value.trim(); if(!t){$('#ghmsg').textContent='paste a token first';return;} $('#ghmsg').textContent='checking…'; const r=await post('api/settings',{github_token:t}); $('#ghmsg').textContent=r.ok?r.note:'ERROR: '+r.error; if(r.ok){$('#ghtoken').value='';} ghState();};
$('#ghclear').onclick=async()=>{if(!confirm('Clear the stored GitHub token?')) return; const r=await post('api/settings',{github_token:''}); $('#ghmsg').textContent=r.ok?'token cleared':'ERROR: '+r.error; ghState();};
ghState().catch(()=>{});
// fetchDownload is in hri.js: the Log files page downloads a log file the same way
$('#diagzip').onclick=()=>fetchDownload($('#diagzip'),'api/diagnostics','Diagnostics zip','hri-diagnostics.zip');
$('#memsnap').onclick=()=>fetchDownload($('#memsnap'),'api/diag/memory','Memory snapshot',`hri-memory-${new Date().toISOString().slice(0,19).replace(/[-:]/g,'')}.json`);
// vcmp (version order) is in hri.js: the Config, Install and manager pages sort versions with the same one
// Whether a version's pinned requirements have a wheel for this image's Python.  Checked on the server (pip,
// no install), cached there per version for an hour; /api/ha hands over what that cache already holds, so
// painting the list and the selection costs nothing and the 60 s refresh never starts a pip run.
const HACHK={};
function haCheckText(c){
  if(!c) return '';
  if(!c.ok) return `<span class="bad">unlikely to install: ${esc((c.blockers||[]).join('; '))}</span>`;
  if(c.checked===false) return `<span class="mut">${esc((c.notes||[]).join('; ')||'not checked')}</span>`;
  return `<span class="ok">installs here</span> <span class="mut">${esc((c.notes||[]).join('; '))}</span>`;
}
// what is known about a version without asking anything: an answer already in hand, or the image's floor -
// everything below it is refused before an install is scheduled, and that is arithmetic, not a check
function haKnown(v){
  if(HACHK[v]) return HACHK[v];
  const b=HA&&HA.baseline;
  return b&&v&&vcmp(v,b)<0?{version:v,ok:false,checked:true,missing:[],warnings:[],notes:[],
    blockers:[`${v} is older than this image's floor ${b}; not installable here`]}:null;
}
function haCheckShow(v){ $('#hacheck').innerHTML=v?haCheckText(haKnown(v)):''; }
// the mark a version carries in the list; nothing is claimed about one nobody has checked
function haMark(v){
  const c=haKnown(v);
  if(c) return c.ok===false?' ✗':(c.checked===false?'':' ✓');
  return HA&&(HA.installed_venvs||[]).includes(v)?' ✓':'';  // already on the volume: it boots as it is
}
async function haCheck(v){
  if(!v) return null;
  const known=haKnown(v); if(known&&(HACHK[v]||!known.ok)){haCheckShow(v);return known;}  // answered, or refused before pip gets a say
  $('#hacheck').innerHTML='<span class="mut">checking the requirements of '+esc(v)+'…</span>';
  const r=await post('api/ha/check',{version:v});
  if(!r.ok){$('#hacheck').innerHTML=`<span class="warn">${esc(r.error||'check failed')}</span>`;return null;}
  HACHK[v]=r.check; if(HA) haOptions(HA);  // the answer becomes that version's mark in the list too
  if($('#haver').value===v) haCheckShow(v);  // the selection may have moved on while pip ran
  return r.check;
}
// The list: the newest few releases, plus everything this box already has - installed on the volume, running
// now, scheduled, or the one a rollback goes back to - however old that is, so nothing an operator has can
// fall off it.  "show all versions" asks /api/ha for the rest; nothing is lost either way.
function haOptions(h){
  const sel=$('#haver'), keep=sel.value, list=(h.all_versions||h.versions||[]).slice().reverse();
  const pick=keep&&list.includes(keep)?keep:h.current;  // the 60 s refresh must not reset what the user chose
  sel.innerHTML=list.map(v=>`<option value="${esc(v)}" ${v===pick?'selected':''}>${esc(v)}${haMark(v)}</option>`).join('');
  $('#halistnote').textContent=h.all_versions?`all ${list.length} releases`
    :`newest ${h.recent_n||list.length} of ${h.versions_total||list.length} releases, plus what this box has installed, runs or has scheduled`;
}
async function ha(force){
  const q=[force?'refresh=1':'',$('#haall').checked?'all=1':''].filter(Boolean).join('&');
  const h=await (await fetch('api/ha'+(q?'?'+q:''),{headers:{'X-Requested-With':'fetch'}})).json(); HA=h;
  Object.assign(HACHK,h.verdicts||{});  // what the server already knew: painted, never re-resolved
  $('#hacur').innerHTML=`<span class="ok">${esc(h.current)}</span> <span class="mut">python ${esc(h.python)}${h.in_venv?'':' · <span class="warn">not running from a venv (old image)</span>'}</span>`;
  $('#halatest').innerHTML=(h.latest_stable?`${esc(h.latest_stable)} <span class="mut">(${esc(h.latest_published||'')})</span>${h.update_available?' <span class="tag warn">update available</span>':' <span class="tag ok">up to date</span>'}`:`<span class="warn">${esc(h.error||'unknown')}</span>`)+` <span class="mut">· checked ${esc((h.checked_at||'').slice(11,16))}</span>`;
  $('#havenvs').textContent=(h.installed_venvs||[]).join(', ')||'—';
  const apt=h.apt;  // what the entrypoint did with HRI_APT_PACKAGES at this boot; a failure is not fatal, so it shows here
  $('#haapt').innerHTML=!apt?'<span class="mut">none (HRI_APT_PACKAGES not set)</span>'
    :`${esc((apt.packages||[]).join(', ')||'—')} <span class="${apt.error?'mut':apt.ok?'ok':'bad'}">${esc(apt.note||(apt.ok?'ok':'failed'))}</span>`
      +(apt.error?` <span class="bad">${esc(apt.error)}</span>`:'')
      +((apt.refused||[]).length?` <span class="warn">refused: ${esc(apt.refused.join(', '))}</span>`:'')
      +' <span class="mut">· log: integration_manager/apt-install.log</span>';
  $('#haerr').textContent=h.last_error||'—';
  $('#haupdate').textContent=`Update to ${h.latest_stable||'…'}`; $('#haupdate').disabled=!h.update_available; $('#haupdate').dataset.v=h.latest_stable||'';
  $('#harollback').disabled=!h.previous; $('#harollback').textContent=h.previous?`Roll back HA to ${h.previous}`:'Roll back HA';
  haOptions(h);
  $('#hanote').textContent=h.pending?`wanted version ${h.desired}: applied at the next process restart (choose ${h.current} and Install selected version to cancel)`:'';
  haPlan($('#haver').value);
  haCheckShow($('#haver').value);  // the cached verdict, if this version was already checked; never a new pip run from the 60 s refresh
}
function haPlan(v){
  const box=$('#haplan'); if(!HA||!v||vcmp(v,HA.current)>=0){box.hidden=true;box.innerHTML='';box.dataset.v='';return 'keep';}
  const b=(HA.config_backups||{})[v], was=box.dataset.v===v?(box.querySelector('input[name=haconfig]:checked')||{}).value:null;
  const mode=was&&(was!=='restore'||b)?was:(b?'restore':'rebuild');
  const opt=(val,title,text,off)=>`<label style="display:flex;gap:6px;align-items:flex-start;margin:4px 0"><input type="radio" name="haconfig" value="${val}" ${val===mode?'checked':''} ${off?'disabled':''} style="margin-top:3px"><span><b>${title}</b> ${text}</span></label>`;
  box.innerHTML=`<div>Downgrade to ${esc(v)}. Home Assistant migrates its configuration forward only, so choose what ${esc(v)} starts with:</div>`
   +opt('restore','Restore from a backup.',b?`The configuration (<code>.storage</code>) comes back from <b>${esc(b.name)}</b>, made on Home Assistant ${esc(b.ha_version)}. Changes made after that backup are lost.`:`Not available: no backup was made on Home Assistant ${esc(v)} or older.`,!b)
   +opt('rebuild','Start clean and rebuild the integration.',`Home Assistant ${esc(v)} starts like a fresh install. After it boots, the integration's config entries are created again with their data and options, its own store files are copied, and entity ids, names, icons, hidden and disabled flags and device names are applied again. Not carried over: areas, labels, other entity settings such as unit overrides, and the last known states.`)
   +opt('keep','Keep the current configuration.',`Works only when ${esc(v)} can read the newer storage formats; otherwise the boot fails and the container falls back to ${esc(HA.current)}.`);
  box.dataset.v=v; box.hidden=false; return mode;
}
async function haSet(v,action){
  if(!v)return;
  if(vcmp(v,HA.current)===0){
    if(!HA.pending){log(`Home Assistant ${v} is already running`);return;}
    if(!confirm(`Cancel the scheduled switch to Home Assistant ${HA.desired}? What it prepared for the next boot is dropped.`))return;
    const c=await post('api/ha/update',{version:v}); log(c.ok?`switch to ${c.cancelled} cancelled${c.dropped&&c.dropped.length?' (dropped: '+c.dropped.join(', ')+')':''}`:'ERROR: '+c.error); ha(); return;
  }
  const down=vcmp(v,HA.current)<0; if(down&&$('#haplan').dataset.v!==v) haPlan(v);
  const mode=down?(($('#haplan input[name=haconfig]:checked')||{}).value||'keep'):'keep', b=(HA.config_backups||{})[v];
  const cfg={restore:`The configuration (.storage) is restored from ${b?b.name:''}; changes made after that backup are lost.`,
    rebuild:`Home Assistant ${v} starts with a clean configuration and the integration is rebuilt from the backup taken now.`,
    keep:down?'The current configuration is kept.':'The new version migrates the configuration as usual.'}[mode];
  if(!confirm(`${action==='rollback'?'Roll back':'Switch'} to Home Assistant ${v} and restart the process?\n\n- A backup of the current configuration is taken first.\n- ${cfg}\n- A version that is not installed yet takes a few minutes; the current venv is kept for rollback.`))return;
  let r=await post('api/ha/'+action,action==='update'?{version:v,config:mode}:{config:mode});
  if(!r.ok&&r.needs_force){
    HACHK[v]=r.check; haCheckShow(v);
    if(!confirm(`Home Assistant ${v} is unlikely to install:\n\n- ${(r.check&&r.check.blockers||[r.error]).join('\n- ')}\n\nThe container would fall back to ${HA.current} after pip fails. Schedule it anyway?`)){log('ERROR: '+r.error);return;}
    r=await post('api/ha/'+action,{version:v,config:mode,force:true});
  }
  if(!r.ok){log('ERROR: '+r.error);return;}
  if((r.warnings||[]).length&&!confirm(`Home Assistant ${v} is scheduled, but:\n\n- ${r.warnings.join('\n- ')}\n\nRestart now? (Cancel keeps it scheduled: choose the running version to drop it.)`)){ log(`HA ${v} scheduled, not restarted: ${r.warnings.join(' · ')}`); ha(); return; }
  const rr=await post('api/restart'); if(!rr.ok){log('ERROR: '+rr.error);return;}
  log(`HA ${v} scheduled, backup ${r.backup}${r.restore?', configuration from '+r.restore:''}${r.config==='rebuild'?', clean start with rebuild':''}; restarting…`); setTimeout(()=>location.reload(),8000);
}
let HA=null;
$('#haupdate').onclick=()=>haSet($('#haupdate').dataset.v,'update');
$('#hapick').onclick=()=>haSet($('#haver').value,'update');
$('#haver').onchange=()=>{haPlan($('#haver').value); haCheck($('#haver').value);};
$('#haall').onchange=()=>{$('#haall').disabled=true; ha().finally(()=>{$('#haall').disabled=false;});};
$('#hacheckbtn').onclick=()=>{$('#hacheckbtn').disabled=true; delete HACHK[$('#haver').value]; haCheck($('#haver').value).finally(()=>{$('#hacheckbtn').disabled=false;});};
$('#harefresh').onclick=()=>{$('#harefresh').disabled=true;ha(true).finally(()=>{$('#harefresh').disabled=false;});};
$('#harollback').onclick=()=>{const v=HA&&HA.previous; if(!v)return; const sel=$('#haver'); if(![...sel.options].some(o=>o.value===v)) sel.add(new Option(v,v));
  const shown=$('#haplan').dataset.v===v; sel.value=v;
  if(vcmp(v,HA.current)<0&&!shown){haPlan(v); $('#haplan').scrollIntoView({block:'center'}); log(`Choose below what Home Assistant ${v} starts with, then click Roll back again.`); return;}
  haSet(v,'rollback');};
let IMS=null, IMSEL=null;
function imRender(){
  const s=IMS, box=$('#imsummary'); if(!s){box.innerHTML='';$('#imform').hidden=true;$('#imall').hidden=true;return;}
  let h=`<div class="mut">backup <b>${esc(s.name||'')}</b> · ${esc((s.date||'').slice(0,19))} · HA ${esc(s.ha_version||'?')} · ${s.protected?'encrypted':'not encrypted'} · ${Object.keys(s.domains).length} integrations with config entries</div>
   <table><tr><th>integration</th><th>entry</th><th>v</th><th>entities</th><th>devices</th><th>store files</th><th></th></tr>`;
  for(const [dom,d] of Object.entries(s.domains).sort()) for(const e of d.entries){
    const known=!!REG[dom], here=!!INSTALLED[dom];
    h+=`<tr><td>${esc(dom)}${known?' <span class="tag ok">in registry</span>':''}${here?' <span class="tag">installed here</span>':''}</td><td>${esc(e.title||'')} <span class="mut">${esc(String(e.entry_id).slice(0,8))}</span></td><td>${esc(e.version)}.${esc(e.minor_version)}</td><td>${esc(d.entities)}</td><td>${esc(d.devices)}</td><td>${d.storage_files.map(esc).join(', ')||'—'}</td>
      <td><button data-dom="${esc(dom)}" data-eid="${esc(e.entry_id)}" ${here&&d.importable?'':'disabled title="install the integration here first (registry)"'}>Prepare import</button></td></tr>`;
  }
  box.innerHTML=h+'</table>';
  $('#imall').hidden=!Object.entries(s.domains).some(([dom,d])=>INSTALLED[dom]&&d.importable);
  box.querySelectorAll('button').forEach(b=>b.onclick=()=>{IMSEL={dom:b.dataset.dom,eid:b.dataset.eid}; const e=IMS.domains[IMSEL.dom].entries.find(x=>x.entry_id===IMSEL.eid);
    $('#imtitle').textContent=`${IMSEL.dom} · ${e.title||''}`; $('#imdata').value=JSON.stringify(e.data||{},null,1); $('#imoptions').value=JSON.stringify(e.options||{},null,1); $('#imstorage').checked=IMS.domains[IMSEL.dom].storage_files.length>0; $('#imresult').textContent=''; $('#imform').hidden=false;});
}
async function imLoad(){const r=await (await fetch('api/import/inspect')).json(); IMS=r.summary; imRender(); if(r.summary) $('#immsg').textContent='inspected backup ready (archive already deleted)';}
$('#imupload').onclick=async()=>{const f=$('#imfile').files[0];
  if(f){ const fd=new FormData(); fd.append('file',f); $('#immsg').textContent=`uploading ${(f.size/1048576).toFixed(0)} MB…`;
    const u=await (await fetch('api/import/upload',{method:'POST',headers:{'X-Requested-With':'fetch'},body:fd})).json(); if(!u.ok){$('#immsg').textContent='ERROR: '+u.error;return;} }
  $('#immsg').textContent='inspecting…'; const r=await post('api/import/inspect',{password:$('#impass').value||null});
  $('#immsg').textContent=r.ok?'inspected':'ERROR: '+r.error; if(r.ok){IMS=r.summary; imRender();}};
$('#imclear').onclick=async()=>{if(!confirm('Discard the uploaded backup and everything inspected from it?')) return; const r=await post('api/import/clear'); if(!r.ok){$('#immsg').textContent='ERROR: '+r.error;return;} IMS=null; IMSEL=null; imRender(); $('#immsg').textContent='cleared';};
$('#imapplyall').onclick=async()=>{ const doms=Object.keys(IMS.domains).filter(d=>INSTALLED[d]&&IMS.domains[d].importable); if(!doms.length) return;
  if(!confirm(`Import the config entries of ${doms.join(', ')} with their data/options exactly as in the backup? Integrations that are not running get their entry stored disabled.`)) return; $('#imallresult').textContent='importing…';
  const r=await post('api/import/apply_all',{domains:doms,align:$('#imallalign').checked,copy_storage:$('#imallstorage').checked});
  $('#imallresult').textContent=r.ok?`imported: ${r.imported.map(x=>x.domain+' ('+x.state+')').join(', ')||'none'}; skipped: ${r.skipped.map(x=>x.domain).join(', ')||'none'}; failed: ${r.failed.map(x=>x.domain+': '+x.error).join('; ')||'none'}; ${r.cleaned_up?'extracted backup removed':'extracted backup kept for a retry (Clear removes it)'}`:'ERROR: '+r.error; if(r.ok&&r.cleaned_up){IMS=null;IMSEL=null;imRender();} sysStatus(); };
$('#imapply').onclick=async()=>{ if(!IMSEL) return; let data,options; try{data=JSON.parse($('#imdata').value||'{}'); options=JSON.parse($('#imoptions').value||'{}');}catch(e){$('#imresult').textContent='invalid JSON: '+e.message;return;}
  if(!confirm(`Import ${IMSEL.dom} into this instance and set it up now?`)) return; $('#imresult').textContent='importing…';
  const r=await post('api/import/apply',{domain:IMSEL.dom,entry_id:IMSEL.eid,data,options,align:$('#imalign').checked,copy_storage:$('#imstorage').checked});
  $('#imresult').textContent=r.ok?`ok: entry ${r.entry_id.slice(0,8)} state ${r.state}; storage copied: ${r.copied_storage.join(', ')||'none'}${r.alignment?`; aligned ${r.alignment.entities} entities / ${r.alignment.devices} devices (${r.alignment.pending_entities} pending)`:''}; extracted backup removed`:'ERROR: '+r.error; if(r.ok){IMS=null;IMSEL=null;imRender();} sysStatus(); };
imLoad().catch(e=>log('import: '+e));
const fmtB=b=>b>1048576?(b/1048576).toFixed(1)+' MB':(b/1024).toFixed(0)+' KB';
let BK={};
async function doRestore(n,parts,ha,force){
  const r=await post(`api/backups/${encodeURIComponent(n)}/restore`,{parts,ha,force:!!force});
  if(!r.ok&&r.needs_force&&!force){ if(confirm(`${r.error}\n\nRestore anyway?`)) return doRestore(n,parts,ha,true); $('#bkmsg').textContent='restore not scheduled'; return; }
  if(!r.ok){$('#bkmsg').textContent='ERROR: '+r.error;return;}
  const rr=await post('api/restart'); if(!rr.ok){$('#bkmsg').textContent='restore scheduled, but the restart was refused: '+rr.error;return;}
  log(r.ha?`restore with Home Assistant ${r.ha} scheduled (backup ${r.pre_change_backup} taken first); restarting…`:'restore scheduled; restarting…');
  setTimeout(()=>location.reload(),r.ha?20000:10000);
}
// a backup made on another Home Assistant version: Home Assistant only migrates a configuration forward
function pendingConfigSwitch(){ const c=BK.change; return c&&['restore','rebuild'].includes(c.mode)&&c.to!==BK.ha_current?c:null; }
function restorePlan(n,parts,made,boot){
  const box=$('#bkrestore'), newer=vcmp(made,boot)>0, installed=(BK.ha_installed||[]).includes(made), sw=pendingConfigSwitch();
  const what=parts?parts.join(' + '):'everything', replaces=boot!==BK.ha_current?` It replaces the scheduled switch to ${esc(boot)}.`:'';
  const venv=installed?'its venv is still on the volume':'it is downloaded and installed at the restart (a few minutes)';
  const opt=(value,checked,title,note)=>`<label style="display:flex;gap:8px;align-items:flex-start;margin:6px 0;cursor:pointer"><input type="radio" name="bkha" value="${value}" ${checked?'checked':''} style="margin-top:3px"><span><b>${title}</b><br><span class="mut">${note}</span></span></label>`;
  let body;
  if(made===BK.ha_current){  // made on the running version while a switch to another one is scheduled
    body=opt('backup',true,`Stay on Home Assistant ${esc(made)}`,`Cancels the scheduled switch to ${esc(boot)} and restores the backup on the running version.`);
  }else if(newer){
    body=`<div class="warn">Home Assistant ${esc(boot)} cannot read a configuration made on ${esc(made)}, so the restore switches Home Assistant to ${esc(made)}: ${venv}. A backup of the current configuration is taken first and brought back if ${esc(made)} does not start.${replaces}</div><input type="radio" name="bkha" value="backup" checked hidden>`;
  }else{
    body=(sw?`<div class="warn" style="margin-bottom:4px">A switch to Home Assistant ${esc(sw.to)} with a ${sw.mode==='restore'?'configuration restore':'clean start'} is scheduled, so the backup cannot be restored on it: cancel that switch under Home Assistant first, or go back to ${esc(made)} instead.</div>`
          :opt('keep',true,`Keep Home Assistant ${esc(boot)} (default)`,`The restored configuration is migrated forward when Home Assistant starts; nothing to download.`))
      +opt('backup',!!sw,`Go back to Home Assistant ${esc(made)}`,`Exactly the state of the backup: ${venv}. A backup of the current configuration is taken first and brought back if ${esc(made)} does not start.${replaces}`);
  }
  box.innerHTML=`<div style="font-weight:600;margin-bottom:6px">Restore ${esc(n)} (${esc(what)})</div>
   <div style="margin-bottom:6px">This backup was made on Home Assistant <b>${esc(made)}</b>; this container ${boot===BK.ha_current?'runs':'boots next with'} <b>${esc(boot)}</b>. Home Assistant migrates a configuration forward, never back.</div>
   ${body}
   <div class="row" style="margin-top:8px"><button id="bkrgo" class="primary">Restore and restart</button><button id="bkrcancel">Cancel</button></div>`;
  box.hidden=false; box.scrollIntoView({behavior:'smooth',block:'center'});
  $('#bkrcancel').onclick=()=>{box.hidden=true;box.innerHTML='';};
  $('#bkrgo').onclick=()=>{const choice=(box.querySelector('input[name="bkha"]:checked')||{}).value||'keep'; box.hidden=true; box.innerHTML=''; doRestore(n,parts,choice);};
}
async function backups(){
  const r=await (await fetch('api/backups')).json(); BK=r;
  if(document.activeElement!==$('#bkkeep')) $('#bkkeep').value=String(r.keep??5);
  const t=$('#bklist'); t.querySelectorAll('tr:not(:first-child)').forEach(e=>e.remove());
  for(const b of r.backups){
    const tr=document.createElement('tr');
    tr.innerHTML=`<td>${esc(b.name)}</td><td>${esc(b.label||'')}</td><td class="mut">${esc(b.ha_version||'—')}</td><td>${fmtB(b.bytes)}</td><td>${esc(b.files??'—')}</td>
      <td><a href="api/backups/${encodeURIComponent(b.name)}/download" download>download</a> <button data-a="restore" data-n="${esc(b.name)}">Restore</button> <button data-a="delete" data-n="${esc(b.name)}">Delete</button></td>`;
    t.appendChild(tr);
  }
  t.querySelectorAll('button').forEach(b=>b.onclick=async()=>{
    const n=b.dataset.n, a=b.dataset.a;
    if(a==='delete'){ if(!confirm(`Delete backup ${n}?`)) return; const r=await post(`api/backups/${encodeURIComponent(n)}/delete`); $('#bkmsg').textContent=r.ok?'deleted':'ERROR: '+r.error; return backups(); }
    if(a==='restore'){ const pv=$('#bkparts').value; const parts=pv?pv.split(','):null;
      const bk=(BK.backups||[]).find(x=>x.name===n)||{}, boot=BK.ha_boot||BK.ha_current;
      // the Home Assistant version only matters when .storage comes back
      if(bk.ha_version&&boot&&vcmp(bk.ha_version,boot)!==0&&(!parts||parts.includes('storage'))) return restorePlan(n,parts,bk.ha_version,boot);
      const sw=pendingConfigSwitch();
      if(sw){ $('#bkmsg').textContent=`ERROR: a switch to Home Assistant ${sw.to} with a ${sw.mode==='restore'?'configuration restore':'clean start'} is scheduled: cancel it under Home Assistant first`; return; }
      const unknown=!bk.ha_version&&(!parts||parts.includes('storage'));
      if(!confirm(`Restore ${n}${parts?' ('+parts.join(' + ')+' only)':''}?${unknown?`

This backup does not record the Home Assistant version it was made on. If it was made on a version newer than ${boot}, Home Assistant cannot read the restored .storage. Restore anyway only if it was made on ${boot} or older.`:''}

The process restarts now; the entrypoint replaces ${parts?parts.join(', '):'.storage, custom_components and integration_manager'} from the backup (a pre-restore backup is taken first) and boots HA again.`)) return;
      doRestore(n,parts,'keep',unknown); }
  });
  $('#bkcancel').hidden=!r.pending_restore;
  const lr=r.last_restore; $('#bkpending').textContent=r.pending_restore?`a restore is scheduled for the next restart (${(r.pending_parts||[]).join(', ')})`:(lr?`last restore ${lr.at}: ${lr.ok?'ok, '+lr.files+' files (pre-restore copy '+lr.pre_restore+')':'FAILED: '+lr.error}`:'');
}
async function bkSettings(){const r=await (await fetch('api/settings')).json(); if(document.activeElement!==$('#bkhour')){$('#bkdaily').checked=!!r.backup_daily; $('#bkhour').value=r.backup_daily_hour;} $('#smoke').value=r.smoke_test_s; $('#autorb').checked=!!r.auto_rollback; $('#relcheck').checked=!!r.release_check;
  $('#wd').checked=!!r.watchdog; $('#wdd').checked=!!r.watchdog_on_degraded; for(const [id,k] of [['#wdafter','watchdog_after_min'],['#wdint','watchdog_min_interval_min'],['#wdday','watchdog_max_per_day']]) if(document.activeElement!==$(id)) $(id).value=r[k];}
$('#bkdaily').onchange=$('#bkhour').onchange=async()=>{const r=await post('api/settings',{backup_daily:$('#bkdaily').checked,backup_daily_hour:parseInt($('#bkhour').value,10)||0}); $('#bkmsg').textContent=r.ok?(r.backup_daily?`daily backup at ${r.backup_daily_hour}:00`:'daily backup off'):'ERROR: '+r.error;};
bkSettings().catch(()=>{});
$('#bkcancel').onclick=async()=>{ if(!confirm('Cancel the scheduled restore?')) return; const r=await post('api/backups/restore/cancel'); $('#bkmsg').textContent=r.ok?'restore cancelled':'ERROR: '+r.error; backups(); };
$('#bkkeep').onchange=async()=>{const r=await post('api/settings',{backup_keep:parseInt($('#bkkeep').value,10)}); $('#bkmsg').textContent=r.ok?`keeping ${r.backup_keep||'all'} backups`:'ERROR: '+r.error;};
$('#bkcreate').onclick=async()=>{$('#bkmsg').textContent='creating…';const r=await post('api/backups/create',{label:$('#bklabel').value});$('#bkmsg').textContent=r.ok?`created ${r.backup.name} (${fmtB(r.backup.bytes)}, ${r.backup.files} files)${r.pruned.length?'; pruned '+r.pruned.join(', '):''}`:'ERROR: '+r.error;backups();};
$('#bkupload').onclick=async()=>{const f=$('#bkfile').files[0]; if(!f){$('#bkmsg').textContent='choose a .zip first';return;}
  const fd=new FormData(); fd.append('file',f); $('#bkmsg').textContent='uploading…';
  const r=await (await fetch('api/backups/upload',{method:'POST',headers:{'X-Requested-With':'fetch'},body:fd})).json();
  $('#bkmsg').textContent=r.ok?`uploaded ${r.name} (${r.info.files} files)`:'ERROR: '+r.error; backups();};
backups().catch(e=>log('backups: '+e));
$('#restart').onclick=async()=>{if(!confirm('Restart the process?'))return;log('restarting…');const r=await post('api/restart');if(!r.ok){log('ERROR: restart refused: '+r.error);return;}setTimeout(()=>location.reload(),6000)};
sysStatus().catch(e=>log('error: '+e)); ha().catch(e=>log('ha: '+e));
setInterval(sysStatus,15000); setInterval(ha,60000);
