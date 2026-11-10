import datetime,html,json,os,signal,subprocess,time
from pathlib import Path
r=Path(__file__).resolve().parent
g=json.loads((r/'awake.json').read_text())
expected=subprocess.check_output(['ps','-p',str(g['pid']),'-o','lstart=','-o','command='],text=True).strip()
g['process_identity']=expected;(r/'awake.json').write_text(json.dumps(g,indent=2))
while True:
 try:
  state=json.loads((r/'campaign.json').read_text())
  rows=[]
  for p in sorted((r/'snapshot/monitoring').glob('*/.results/**/report.html')):
   rows.append('<li><a href="'+html.escape(str(p.relative_to(r)))+'">'+html.escape(str(p.relative_to(r)))+'</a></li>')
  details=[]
  for p in (r/'snapshot/monitoring/validation/.results').glob('*/campaign.json'):
   d=json.loads(p.read_text());details.append(d.get('current',''))
  page='<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="30"><title>post-restart live progress</title><style>body{font:17px system-ui;background:#101c2b;color:#eef;margin:40px}a{color:#7bd}li{margin:12px}</style><h1>post-restart: '+html.escape(state['state'])+'</h1><p>'+html.escape(state.get('active',''))+' · '+html.escape(' / '.join(details))+'</p><p><a href="report.html">Campaign journal and scheduled studies</a></p><ul>'+''.join(rows)+'</ul><p>Every listed result belongs only to this post-restart campaign. A report can still be in progress or contain failed performance gates.</p>'
  (r/'live.html').write_text(page)
  if state['state'] not in ['preparing','running']:
   actual=subprocess.run(['ps','-p',str(g['pid']),'-o','lstart=','-o','command='],capture_output=True,text=True).stdout.strip()
   if actual==expected and 'caffeinate -i -s' in actual:
    os.kill(g['pid'],signal.SIGTERM);g.update(status='stopped',stopped=time.time());(r/'awake.json').write_text(json.dumps(g,indent=2))
   break
 except (FileNotFoundError,json.JSONDecodeError):pass
 time.sleep(15)
