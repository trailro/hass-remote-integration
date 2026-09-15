let files=[];
// example for Home Assistant style lines: 2026-01-01 12:00:00.123 WARNING (MainThread) [custom_components.demo] text
const EXAMPLE={pattern:'^(?P<time>\\S+ \\S+) (?P<level>[A-Z]+) \\((?P<thread>[^)]*)\\) \\[(?P<logger>[^\\]]+)\\] (?P<message>.*)$',
 hide:['thread'], dim:['time','logger'], color_by:'level', colors:{WARNING:'warn',ERROR:'bad',CRITICAL:'bad',DEBUG:'muted'}};
async function loadFiles(){
 files=await (await fetch('/api/log_files')).json();
 const sel=$('#file'), cur=sel.value;
 sel.innerHTML=files.map(f=>`<option value="${esc(f.name)}" ${f.name===cur?'selected':''}>${esc(f.name)} (${(f.bytes/1024).toFixed(0)} KB${f.active?', active':''}${f.source?', '+esc(f.source):''})</option>`).join('');
 if(!files.length) sel.innerHTML='<option value="">(the running integration writes no log file)</option>';
}
let LOADING=false, AGAIN=false;
async function load(){ if(LOADING){ AGAIN=true; return; } LOADING=true; try{ do{ AGAIN=false; await loadNow(); }while(AGAIN); } finally { LOADING=false; } }  // no overlapping polls; a change made meanwhile loads right after
async function loadNow(){
 const p=new URLSearchParams({file:$('#file').value,lines:$('#lines').value,q:$('#q').value});
 const r=await (await fetch('/api/log_files/tail?'+p,{headers:{'X-Requested-With':'fetch'}})).json();
 $('#path').textContent=r.path||'—'; $('#size').textContent=r.bytes!=null?`${(r.bytes/1024).toFixed(0)} KB · ${r.total_lines_scanned} lines read`:'';
 $('#ts').textContent=new Date().toLocaleTimeString(); $('#n').textContent=`${(r.lines||[]).length} lines`;
 $('#fmterr').textContent=r.format_error?'the stored format is ignored: '+r.format_error:'';
 const cols=r.columns||[], span=Math.max(1,cols.length);
 $('#th').innerHTML=cols.length?cols.map(c=>`<th>${esc(c.name)}</th>`).join(''):'<th>line</th>';
 const wrap=document.querySelector('.wrap'); const atBottom=wrap.scrollTop+wrap.clientHeight>=wrap.scrollHeight-20;
 $('#tb').innerHTML=(r.lines||[]).map(l=>l.cells
   ?`<tr class="${l.color?'c-'+esc(l.color):''}">${l.cells.map((v,i)=>`<td class="${cols[i]&&cols[i].dim?'mut':''}">${esc(v)}</td>`).join('')}</tr>`
   :`<tr><td colspan="${span}" class="${cols.length?'raw':''}">${esc(l.raw)}</td></tr>`).join('');
 if(atBottom) wrap.scrollTop=wrap.scrollHeight;
}
async function loadFormat(){
 const s=await (await fetch('/api/settings')).json(); const f=s.log_format||{};
 $('#fmt').value=Object.keys(f).length?JSON.stringify(f,null,2):'';
}
$('#fmtsave').onclick=async()=>{
 const text=$('#fmt').value.trim(); let fmt=null;
 if(text){ try{ fmt=JSON.parse(text); }catch(e){ $('#fmtmsg').textContent='not valid JSON: '+e.message; return; } }
 const r=await post('/api/settings',{log_format:fmt});
 $('#fmtmsg').textContent=r.ok?(fmt?'saved':'cleared: lines are shown whole'):(r.error||'error');
 if(r.ok){ await loadFormat(); load(); }
};
$('#fmtexample').onclick=()=>{ $('#fmt').value=JSON.stringify(EXAMPLE,null,2); $('#fmtmsg').textContent='example inserted, not saved yet'; };
['#file','#lines'].forEach(s=>$(s).addEventListener('change',load)); let qt; $('#q').addEventListener('input',()=>{clearTimeout(qt); qt=setTimeout(load,350);});
setInterval(()=>{ if($('#follow').checked) load().catch(()=>{}); },5000);
setInterval(loadFiles,60000);
loadFiles().then(load); loadFormat();
