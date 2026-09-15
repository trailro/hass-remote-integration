let REG={}, SEL=null, CUR=null;
function replaceOk(d){
  if(!CUR||CUR===d) return true;
  return confirm(`This container holds ${CUR}. Installing ${d} REPLACES it: its config entries, patches, YAML and retained MQTT documents are removed after a backup (pre-replace-${CUR}); coming back means restoring that backup. Continue?`);
}
async function regLoad(){
  const s=await (await fetch('api/status')).json(); REG=s.registry||{}; CUR=s.integration||null; $('#curdom').textContent=CUR||'(none)';
  if(!$('#regsel').options.length||[...$('#regsel').options].map(o=>o.value).join()!==Object.keys(REG).join()){
    $('#regsel').innerHTML=Object.entries(REG).filter(([d,x])=>x.repo).map(([d,x])=>`<option value="${esc(d)}" ${d===SEL?'selected':''}>${esc(d)} — ${esc(x.name||'')}</option>`).join('');
  }
  $('#regrepo').textContent=(REG[$('#regsel').value]||{}).repo||'';
}
$('#regsel').onchange=()=>{SEL=$('#regsel').value; $('#regrepo').textContent=(REG[SEL]||{}).repo||'';};
$('#regadd').onclick=async()=>{const r=await post('api/registry',{domain:$('#regdom').value,repo:$('#regrepo_in').value,name:$('#regname').value}); log(r.ok?`added ${r.domain}`:'ERROR: '+r.error); SEL=r.domain; await regLoad(); await options();};
$('#reginstall').onclick=async()=>{const dom=$('#regsel').value; if(!dom) return; const rels=await (await fetch('api/releases?domain='+encodeURIComponent(dom))).json(); if(!Array.isArray(rels)){log('releases: '+(rels.message||'error'));return;} const st=rels.find(x=>!x.prerelease); if(!st){log('no stable release found');return;}
  if(!replaceOk(dom)) return; if(!confirm(`Install ${dom} ${st.tag} into the version store? (nothing runs until you start it)`)) return; log(`installing ${dom} ${st.tag}…`); const r=await post('api/install',{domain:dom,tag:st.tag,replace:CUR!==dom}); log(r.ok?`installed ${r.domain} ${r.tag} (version ${r.version||'?'}); open its Config page to configure and start it`:'ERROR: '+r.error); await regLoad(); await options();};

let OPT=null, REPORT=null;
function domain(){ return $('#bdomain').value.trim().toLowerCase()||$('#bdom').value; }
function ref(){ return $('#bref').value.trim()||$('#brel').value; }
function ha(){ return $('#bhafree').value.trim()||$('#bha').value; }
async function options(){
  OPT=await (await fetch('api/build/options')).json();
  const reg=OPT.registry||{}; const curDom=$('#bdom').value, curHa=$('#bha').value;
  $('#bdom').innerHTML=Object.entries(reg).map(([d,s])=>`<option value="${esc(d)}" ${d===(curDom||(OPT.running&&OPT.running.domain))?'selected':''}>${esc(d)}${s.repo?' — '+esc(s.repo):' (local)'}</option>`).join('')||'<option value="">(registry empty)</option>';
  const h=OPT.ha||{}; const vers=[...new Set([h.current,h.latest_stable,...(h.recent||[]),...(h.installed_venvs||[]),curHa].filter(Boolean))].sort().reverse();
  $('#bha').innerHTML=vers.map(v=>`<option value="${esc(v)}" ${v===(curHa||h.current)?'selected':''}>${esc(v)}${v===h.current?' (running)':v===h.latest_stable?' (latest stable)':(h.installed_venvs||[]).includes(v)?' (venv on the volume)':''}</option>`).join('');
  $('#bhainfo').textContent=`python ${h.python||'?'}${h.pending?' · '+h.desired+' already wanted for the next restart':''}${h.error?' · PyPI: '+h.error:''}`;
  domInfo(); releases();
}
// the report on screen is only valid for the exact combination it was made for
let CHECK=null;
function combo(){ return JSON.stringify([domain(),ref(),ha()]); }
function invalidate(){ if(CHECK&&combo()!==CHECK.combo){ CHECK=null; $('#bprepare').disabled=$('#bstart').disabled=true; $('#bmsg').innerHTML='<span class="warn">selection changed: run Check again</span>'; } }
['#bdom','#bdomain','#brepo','#brel','#bref','#bha','#bhafree'].forEach(s=>{ $(s).addEventListener('change',invalidate); $(s).addEventListener('input',invalidate); });
function domInfo(){
  const d=domain(); const s=(OPT.registry||{})[d]; const inst=(OPT.installed||{})[d]||[];
  $('#bdominfo').innerHTML=s?`<b>${esc(d)}</b> · ${s.repo?'github.com/'+esc(s.repo):'local directory (dev mode: no releases)'} · in store: ${inst.map(esc).join(', ')||'nothing'}${OPT.running&&OPT.running.domain===d?' · <span class="ok">running '+esc(OPT.running.tag||'')+'</span>':''}`:(d?`<b>${esc(d)}</b> is new: give its GitHub owner/repo; it is added to the registry at Check`:'');
}
async function releases(force){
  const d=domain(); const sel=$('#brel'); const keep=sel.value; sel.innerHTML='<option value="">(loading…)</option>'; $('#brelinfo').textContent='';
  const s=(OPT.registry||{})[d]; if(!d||!s||!s.repo){ sel.innerHTML='<option value="">(no releases: type a ref)</option>'; return; }
  let r=[]; try{ r=await (await fetch('api/releases?domain='+encodeURIComponent(d)+(force?'&refresh=1':''),{headers:{'X-Requested-With':'fetch'}})).json(); }catch(e){}
  if(!Array.isArray(r)){ sel.innerHTML='<option value="">(releases unavailable)</option>'; $('#brelinfo').textContent=r.message||''; return; }
  sel.innerHTML=r.map(x=>`<option value="${esc(x.tag)}" ${x.tag===keep?'selected':''}>${esc(x.tag)}${x.prerelease?' (prerelease)':''}${x.installed?' · in store':''}${x.running?' · running':''}</option>`).join('')||'<option value="">(no releases: type a ref)</option>';
  $('#brelinfo').textContent=r.length?`${r.length} releases on GitHub; a branch name or commit sha works too`:'';
}
$('#bdom').onchange=()=>{$('#bdomain').value=''; domInfo(); releases();};
$('#bdomain').oninput=domInfo; $('#brelrefresh').onclick=()=>releases(true);
function body(extra){ const b={domain:domain(),ref:ref(),ha:ha(),...extra}; const rp=$('#brepo').value.trim(); if($('#bdomain').value.trim()&&rp){b.repo=rp;b.name=$('#bname').value.trim();} return b; }
$('#bcheck').onclick=async()=>{
  if(!domain()||!ref()){$('#bmsg').textContent='choose an integration and a version';return;}
  $('#bmsg').textContent='checking (download, pip --dry-run: up to a few minutes)…'; $('#breport').innerHTML=''; $('#bprepare').disabled=$('#bstart').disabled=true;
  const r=await post('api/build/check',body({})); if(!r.ok){$('#bmsg').innerHTML='<span class="bad">'+esc(r.error)+'</span>'; return;}
  REPORT=r.report; CHECK={id:r.check_id,combo:combo()}; $('#bmsg').textContent=''; $('#breport').innerHTML=renderPreflight(r.report)+(r.ha_check&&r.ha_check.version?`<div class="${r.ha_check.ok?'ok':'bad'}" style="margin-top:6px">Home Assistant ${esc(r.ha_check.version)}: ${r.ha_check.ok?'installable here':esc(r.ha_check.error)}</div>`:'');
  $('#bprepare').disabled=$('#bstart').disabled=!r.report.ok;
};
async function prepare(start){
  const d=domain(), rf=ref(), hv=ha(); const running=OPT.running&&OPT.running.domain;
  if(!CHECK||CHECK.combo!==combo()){ $('#bmsg').innerHTML='<span class="warn">run Check for this exact combination first</span>'; return; }
  if(!replaceOk(d)) return;
  if(!confirm(`${start?'Prepare and start':'Prepare'} ${d} ${rf}${hv&&hv!==OPT.ha.current?' on Home Assistant '+hv+' (installed at the restart that follows)':''}?${start&&running&&running!==d?' '+running+' is stopped first.':''}`)) return;
  $('#bmsg').textContent=start?'installing and starting…':'installing…'; const r=await post('api/build/prepare',body({start,replace:!!CUR&&CUR!==d,check_id:CHECK.id}));
  const steps=(r.steps||[]).map(s=>`${esc(s.step)}: ${s.ok?(s.deferred?'after the restart ('+esc(s.note||'')+')':'ok'):'FAILED '+esc(s.error||'')}${s.step==='start'&&s.pip_failed&&s.pip_failed.length?' (pip failed: '+s.pip_failed.map(esc).join(', ')+')':''}`).join(' · ');
  $('#bmsg').innerHTML=(r.ok?`<span class="ok">done</span> · ${steps}`:`<span class="bad">${esc(r.error)}</span> · ${steps}`)+(r.restart_required?' · <b>restart required</b>':'');
  $('#brestart').hidden=!r.restart_required; CHECK=null; $('#bprepare').disabled=$('#bstart').disabled=true; await regLoad(); if(OPT) options();
}
// ----- HACS catalog search -----
let CATSEQ=0, CATTIMER=null;
async function catSearch(){
  const q=$('#catq').value.trim(), t=$('#catlist');
  if(q.length<2){ t.hidden=true; $('#catinfo').textContent=''; return; }
  const seq=++CATSEQ; $('#catinfo').textContent='searching…';
  let r; try{ r=await (await fetch('api/catalog?q='+encodeURIComponent(q),{headers:{'X-Requested-With':'fetch'}})).json(); }catch(e){ r={error:String(e),results:[]}; }
  if(seq!==CATSEQ) return;
  t.querySelectorAll('tr:not(:first-child)').forEach(e=>e.remove());
  const res=r.results||[];
  for(const x of res){ const tr=document.createElement('tr');
    // the integration in this container is never "taken", also when HACS lists its repository under a new name
    const taken=x.in_registry&&!x.installed&&String(x.registry_repo||'').toLowerCase()!==String(x.repo||'').toLowerCase();
    const state=x.installed&&!taken?'<span class="tag ok">in this container</span>':taken?`<span class="tag warn" title="the registry has ${esc(x.registry_repo)} for this domain">domain taken</span>`:x.in_registry?'<span class="tag">in registry</span>':'';
    tr.innerHTML=`<td><b>${esc(x.name)}</b> <span class="mut">${esc(x.domain)}</span> ${state}<br><span class="mut" style="font-size:12px">${esc(x.description)}</span></td><td><a href="https://github.com/${esc(x.repo)}" target="_blank" rel="noopener">${esc(x.repo)}</a></td><td>${esc(x.last_version||'—')}</td><td class="mut" style="white-space:nowrap">${esc(x.last_updated||'')}</td><td><button data-cat="${esc(x.domain)}" data-repo="${esc(x.repo)}" data-name="${esc(x.name)}" ${taken?'disabled':''}>Use</button></td>`;
    t.appendChild(tr); }
  t.hidden=!res.length;
  const size=r.catalog_size||0;
  $('#catinfo').textContent=(r.error&&!size)?'catalog unavailable: '+(r.error||r.message):`${r.total||0} match${r.total===1?'':'es'}${(r.total||0)>res.length?', first '+res.length+' shown':''} · ${size} integrations${r.fetched_at?' · list of '+r.fetched_at.slice(0,16).replace('T',' '):''}${r.error?' · refresh failed: '+r.error:''}`;
  t.querySelectorAll('button[data-cat]').forEach(b=>b.onclick=async()=>{ const d=b.dataset.cat;
    if(!(REG[d]&&REG[d].repo===b.dataset.repo)){ const added=await post('api/registry',{domain:d,repo:b.dataset.repo,name:b.dataset.name}); if(!added.ok){ log('ERROR: '+added.error); return; } }
    SEL=d; await regLoad(); await options(); $('#bdomain').value=''; $('#bdom').value=d; domInfo(); releases(); invalidate();
    $('#bdom').closest('.card').scrollIntoView({behavior:'smooth',block:'start'});
    log(`${d} (${b.dataset.repo}) selected in the environment builder: choose a version and Check`); catSearch(); });
}
$('#catq').addEventListener('input',()=>{ clearTimeout(CATTIMER); CATTIMER=setTimeout(catSearch,300); });
$('#bprepare').onclick=()=>prepare(false); $('#bstart').onclick=()=>prepare(true);
$('#brestart').onclick=async()=>{ if(!confirm('Restart the process now?')) return; const r=await post('api/restart'); if(!r.ok){ $('#bmsg').textContent='restart refused: '+r.error; return; } $('#bmsg').textContent='restarting…'; setTimeout(()=>location.href='/',10000); };



async function loadDev(){
  let r; try{ r=await (await fetch('api/dev')).json(); }catch(e){ return; }
  $('#devdir').innerHTML=r.exists?`<code>${esc(r.dir)}</code> mounted`:`<code>${esc(r.dir)}</code> <span class="warn">not mounted</span>`;
  const d=r.debugpy||{}; $('#devdbg').innerHTML=d.enabled?(d.listening?`· debugpy <span class="ok">listening on :${esc(d.port)}</span>`:`· debugpy <span class="bad">${esc(d.error||'not listening')}</span>`):'· debugpy off (HRI_DEBUGPY unset)';
  const t=$('#devlist'); t.querySelectorAll('tr:not(:first-child)').forEach(e=>e.remove());
  const ST=await (await fetch('api/status')).json();
  for(const c of (r.candidates||[])){ const tr=document.createElement('tr'); const running=ST.running&&ST.running.domain===c.domain&&ST.running.running_tag===r.local_tag;
    tr.innerHTML=`<td><code>${esc(c.path)}</code></td><td><b>${esc(c.domain)}</b>${c.in_registry?'':' <span class="tag">new</span>'}</td><td>${esc(c.version||'')}</td><td class="mut">${c.config_flow?'yes':'no'}</td><td>${running?'<span class="ok">running as local</span>':c.installed?'<span class="mut">in store as local</span>':''}</td>
     <td><button data-dev="${esc(c.domain)}" data-path="${esc(c.path)}">${running?'Reinstall'+($('#devrestart').checked?' + restart':''):c.installed?'Reinstall as local':'Install as local'}</button></td>`; t.appendChild(tr); }
  if(!(r.candidates||[]).length){ const tr=document.createElement('tr'); tr.innerHTML=`<td colspan="6" class="mut">${r.exists?'no manifest.json found under the directory':'mount a directory first'}</td>`; t.appendChild(tr); }
  t.querySelectorAll('button[data-dev]').forEach(b=>b.onclick=async()=>{ if(!replaceOk(b.dataset.dev)) return; if($('#devrestart').checked&&!confirm('Reinstall from the directory and restart the process if that copy is running?')) return; $('#devmsg').textContent='installing…'; const res=await post('api/dev/install',{domain:b.dataset.dev,path:b.dataset.path,restart:$('#devrestart').checked,replace:!!CUR&&CUR!==b.dataset.dev});
    $('#devmsg').textContent=res.ok?`${res.domain} ${res.tag} (version ${res.version||'?'}) from ${res.path}${res.redeployed?(res.restarting?' — running copy refreshed, restarting…':' — running copy refreshed, restart required'):''}${res.pip_failed&&res.pip_failed.length?' · pip failed: '+res.pip_failed.join(', '):''}`:'ERROR: '+res.error;
    if(res.restarting) setTimeout(()=>location.reload(),8000); else { loadDev(); regLoad().then(options); } });
}
$('#devrefresh').onclick=loadDev;
regLoad().then(options).catch(e=>log('build: '+e)); loadDev().catch(()=>{});
