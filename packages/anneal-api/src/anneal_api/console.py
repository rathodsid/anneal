"""Console v0 — a minimal static SPA, a thin client of the API (D32). Runs list + run detail:
NL intent ↔ frames (step events) ↔ network. Deliberately dependency-free (no build step)."""
from __future__ import annotations

CONSOLE_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Anneal console</title>
<style>
 body{font:14px/1.5 system-ui,sans-serif;margin:0;color:#1a1a1a}
 header{background:#0f172a;color:#fff;padding:10px 16px;font-weight:600;display:flex;justify-content:space-between}
 header .muted{color:#94a3b8;font-weight:400}
 .wrap{display:flex;height:calc(100vh - 44px)}
 .list{width:360px;border-right:1px solid #e5e7eb;overflow:auto}
 .detail{flex:1;overflow:auto;padding:16px}
 .run{padding:10px 16px;border-bottom:1px solid #f0f0f0;cursor:pointer}
 .run:hover{background:#f8fafc}
 .v-green,.v-green_with_heals,.v-provisional{color:#15803d}
 .v-red{color:#b91c1c}.v-parked_auth,.v-parked_env{color:#b45309}
 .step{border:1px solid #e5e7eb;border-radius:6px;padding:8px;margin:6px 0}
 .step.failed{border-color:#fca5a5;background:#fef2f2}
 .muted{color:#64748b;font-size:12px}
 code{background:#f1f5f9;padding:1px 4px;border-radius:3px}
 input,textarea{padding:6px 8px;border:1px solid #cbd5e1;border-radius:4px;font:inherit;width:100%;box-sizing:border-box}
 .author{padding:12px 16px;border-bottom:2px solid #e5e7eb;background:#f8fafc}
 .author label{font-size:12px;font-weight:600;color:#334155;display:block;margin:8px 0 2px}
 button{background:#0f172a;color:#fff;border:0;border-radius:5px;padding:8px 14px;cursor:pointer;font:inherit;margin-top:10px}
 button:disabled{opacity:.5;cursor:progress}
 #status{font-size:12px;margin-top:8px}
 video{width:100%;max-width:720px;border:1px solid #e5e7eb;border-radius:6px;background:#000}
</style></head>
<body>
<header><span>Anneal</span><span class="muted" id="proj"></span></header>
<div class="wrap">
 <div class="list">
   <div class="author">
     <label>Describe the test in plain English</label>
     <textarea id="nl" rows="3" placeholder="Open the agents page and confirm the 'Add Agent' button is visible."></textarea>
     <label>Test name</label>
     <input id="tk" placeholder="agents-smoke">
     <button id="go" onclick="authorAndRun()">Author &amp; run on live app ▸</button>
     <div id="status" class="muted"></div>
   </div>
   <div id="list"></div>
 </div>
 <div class="detail" id="detail"><p class="muted">Author a test, or select a run.</p></div>
</div>
<script>
const q = new URLSearchParams(location.search);
const project = q.get('project') || 'golden';
const target = q.get('target') || '';
document.getElementById('proj').textContent = `project: ${project}` + (target?` · target set`:` · no target (add ?target=…)`);
async function loadRuns(sel){
  const runs = await (await fetch(`/projects/${project}/runs`)).json();
  document.getElementById('list').innerHTML = runs.map(x =>
    `<div class="run" onclick="openRun('${x.id}')">
       <div><b>${x.test_key}</b> <span class="v-${x.verdict}">${x.verdict}</span></div>
       <div class="muted">${x.environment} · ${x.duration_ms ?? '—'}ms</div>
     </div>`).join('') || '<p class="muted" style="padding:16px">No runs yet.</p>';
  if(sel) openRun(sel);
}
async function authorAndRun(){
  const nl = document.getElementById('nl').value.trim();
  const tk = document.getElementById('tk').value.trim() || ('test-'+Date.now());
  const st = document.getElementById('status'), btn = document.getElementById('go');
  if(!nl){ st.textContent='enter a description'; return; }
  if(!target){ st.innerHTML='<b class="v-red">no target</b> — open with ?project='+project+'&target=YOUR_TARGET_ID'; return; }
  btn.disabled=true;
  try{
    st.textContent='① authoring from natural language… (exploring the live app + calibrating)';
    const ar = await fetch(`/projects/${project}/tests?test_key=${encodeURIComponent(tk)}&target_id=${target}`,
      {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({nl})});
    const a = await ar.json();
    if(!ar.ok){ st.innerHTML='<b class="v-red">author failed:</b> '+(a.detail||JSON.stringify(a)); btn.disabled=false; return; }
    if(a.status==='needs_answers'){ st.innerHTML='needs answers: '+a.questions.join(' / '); btn.disabled=false; return; }
    st.innerHTML=`✓ frozen <code>${a.test_id.slice(0,8)}</code> v${a.version} (${a.steps} steps, calibrated) · ② running on live app…`;
    const rr = await fetch(`/projects/${project}/tests/${a.test_id}/run?target_id=${target}`,{method:'POST'});
    const r = await rr.json();
    st.innerHTML=`done: <span class="v-${r.verdict}">${r.verdict}</span>`;
    await loadRuns((r.results&&r.results[0])?r.results[0].run_result_id:null);
  }catch(e){ st.innerHTML='<b class="v-red">error:</b> '+e; }
  btn.disabled=false;
}
async function openRun(id){
  const d = await (await fetch(`/runs/${id}`)).json();
  const steps = (d.steps||[]).map(s => `
    <div class="step ${s.status==='failed'?'failed':''}">
      <div><b>[${s.step_index}] ${s.step_id}</b> — ${s.action}
        <span class="v-${s.status==='passed'?'green':'red'}">${s.status}</span></div>
      <div class="muted">${s.intent||''}</div>
      <div class="muted">rung=<code>${s.rung_used ?? '—'}</code> · ${s.elapsed_ms}ms
        ${s.error_class?('· <b>'+s.error_class+'</b>'):''}</div>
      ${(s.network&&s.network.length)?('<div class="muted">network: '+
        s.network.map(n=>`<code>${n.method} ${n.status}</code>`).join(' ')+'</div>'):''}
    </div>`).join('');
  const vid = d.video_path ? `<video src="/runs/${id}/video" controls autoplay muted loop></video>` :
    '<p class="muted">no video</p>';
  document.getElementById('detail').innerHTML =
    `<h2>${d.test_key} <span class="v-${d.verdict}">${d.verdict}</span></h2>
     <p><b>intent (NL):</b> ${d.intent_nl||''}</p>
     <p class="muted">env ${d.environment} · ${d.duration_ms??'—'}ms · trace ${d.trace_path?'✓':'—'}</p>
     <h3>video</h3>${vid}
     <h3>frames</h3>${steps}`;
}
loadRuns();
</script>
</body></html>"""
