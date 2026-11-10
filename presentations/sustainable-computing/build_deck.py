"""Build editable PowerPoint, offline HTML slides, and a visual review sheet.

Requires python-pptx and Pillow. All charts are native text/shapes, not screenshots.
Run from any directory. No cluster access or benchmark execution.
"""
from pathlib import Path
import html
import hashlib
import json
import math
import statistics
import textwrap

from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
BG, PANEL, WHITE, MUTED = '101C2B', '1A2B3F', 'F2F5FA', 'B7C5D7'
JAVA, WASM, AMBER = '69AEFF', '55DFC1', 'FFC878'
slides = []
V = 'monitoring/validation/RESULTS.md'
C = 'monitoring/capacity/VALIDATION_RESULTS.md'
S = 'monitoring/scaleout/.results/repeated-20261103T234859Z/series.json'


def slide(title, subtitle, source, notes=''):
    s = {'title': title, 'subtitle': subtitle, 'source': source, 'notes': notes, 'items': []}
    slides.append(s)
    rect(s, 0, 0, 1280, 7, WASM)
    txt(s, title, 65, 48, 1150, 90, 39, WHITE, True)
    txt(s, subtitle, 65, 145, 1150, 62, 22, MUTED)
    return s


def rect(s, x, y, w, h, color=PANEL):
    s['items'].append(dict(kind='rect', x=x, y=y, w=w, h=h, color=color))


def txt(s, text, x, y, w, h, size=25, color=WHITE, bold=False):
    s['items'].append(dict(kind='text', text=text, x=x, y=y, w=w, h=h,
                           size=size, color=color, bold=bold))


def card(s, x, y, w, heading, value, detail, color=WASM):
    rect(s, x, y, w, 195)
    txt(s, heading, x+20, y+18, w-40, 38, 21, MUTED)
    txt(s, value, x+20, y+64, w-40, 65, 44, color, True)
    txt(s, detail, x+20, y+137, w-40, 50, 19, MUTED)


def message(s, text, color=WASM):
    rect(s, 65, 578, 1150, 65)
    txt(s, text, 83, 590, 1112, 49, 23, color, True)


def bars(s, x, y, w, title, j, a, unit, maximum=None):
    maximum = maximum or max(j, a)*1.15
    txt(s, title, x, y, w, 42, 24, WHITE, True)
    for i, (name, val, color) in enumerate([('Java', j, JAVA), ('Wasm', a, WASM)]):
        yy = y+57+i*64
        txt(s, name, x, yy, 85, 33, 22, color)
        rect(s, x+90, yy+3, (w-215)*val/maximum, 29, color)
        label = f'{val:,.0f}' if val >= 100 else f'{val:g}'
        txt(s, label+' '+unit, x+w-115, yy-2, 120, 39, 22, color, True)


def table(s, headers, rows, widths, y=230, row_h=67):
    x = 65
    for label, w in zip(headers, widths):
        rect(s, x, y, w-2, 44)
        txt(s, label, x+12, y+8, w-24, 34, 20, MUTED, True)
        x += w
    for r, row in enumerate(rows):
        x = 65
        for col, (label, w) in enumerate(zip(row, widths)):
            txt(s, str(label), x+12, y+54+r*row_h, w-24, row_h-3,
                23, JAVA if col == 1 else WASM if col == 2 else WHITE, col > 0)
            x += w


s = slide('Can Wasm help us run fewer servers?',
          'Sustainable computing · Java services vs Rust/Wasm on wasmCloud', V+'; '+C,
          'The question is useful work per physical machine while meeting a service target. '
          'This deck evaluates evidence for consolidation, not a universal runtime ranking. '
          'Measurements are from this repository on a local MacBook setup, through 8 November 2026.')
txt(s, 'A smaller memory footprint.\nA larger CPU appetite.\nA conditional case for consolidation.', 65, 244, 1125, 230, 46, WHITE, True)
message(s, 'Promising for memory-bound fleets; a production replacement is not yet proven.')

s = slide('The result is a trade-off, not a winner',
          'The machine’s limiting resource determines which implementation fits better.', V+'; '+C)
card(s, 65, 241, 355, 'WASM’S STRONGEST RESULT', 'Less RAM', 'Repeated at idle and under load')
card(s, 435, 241, 355, 'JAVA’S STRONGEST RESULT', 'Less CPU', 'Lower typical and p95 latency', JAVA)
card(s, 805, 241, 410, 'THE SUSTAINABILITY QUESTION', 'Fewer hosts?', 'Only if CPU and latency still fit', AMBER)
message(s, 'A runtime change is valuable when it lets us consolidate and retire capacity.')
s['notes'] = 'Retiring capacity is the proposed sustainability mechanism, not an observed outcome. '
s['notes'] += 'No electrical energy, carbon emissions, or production server counts were measured.'

s = slide('What we actually compared',
          'Complete accepted-order HTTP request paths, not isolated language benchmarks.', V+'; '+C)
rect(s, 65, 230, 550, 270); rect(s, 640, 230, 575, 270)
txt(s, 'JAVA', 90, 253, 490, 43, 28, JAVA, True)
txt(s, 'Four service containers\nOrders · Inventory · Products · Notifications', 90, 315, 490, 145, 27)
txt(s, 'RUST / WASM', 665, 253, 520, 43, 28, WASM, True)
txt(s, 'wasmCloud host + native providers\nComponents share runtime resources', 665, 315, 520, 145, 27)
message(s, 'One 10-core Docker VM · application resource totals · shared dependencies excluded', MUTED)
s['notes'] = ('One 16 GiB MacBook Pro; three k3d nodes share a single ten-core Docker VM. '
 'Java totals include four application containers. Wasm totals include host and provider child processes. '
 'Wasm NATS sidecar, shared databases/messaging, operator, ingress, monitoring, and generator are outside '
 'application totals. This is not a whole-system resource or energy inventory. API shapes and architectures '
 'differ; accepted HTTP orders do not establish identical downstream work or notification completion.')

s = slide('How much evidence do we have?',
          'Historical troubleshooting was retained separately from frozen validation.', V+'; '+C)
table(s, ['Study', 'Coverage', 'Question'], [
 ['Footprint', '5 alternating pairs', 'Idle + loaded resource use'],
 ['Memory density', '3 sweeps per version', 'Smallest passing allocation'],
 ['Cold / cached startup', '5 pairs per version', 'First real accepted order'],
 ['Capacity + stability', '3 pairs + 30 min each', 'Sustained load and limits'],
], [340, 325, 485], row_h=65)
message(s, 'Multiple comparisons support the trade-off; long-term production behavior remains open.')
s['notes'] = ('Frozen validation retained 65 historical runs and 1,095 files unchanged. '
 'Frozen footprint trials reused processes rather than five independent fresh starts. '
 'Capacity repetitions used fresh pods and alternating implementation order. '
 'The thirty-minute runs were one per version. Cross-study profiles differ and are not pooled.')

s = slide('Idle memory is the clearest opportunity',
          'Frozen footprint study · medians across five runs · 100 orders/s when loaded', V)
bars(s, 65, 238, 545, 'Idle application RAM', 2732, 293, 'MiB')
bars(s, 670, 238, 545, 'Loaded application RAM', 2739, 323, 'MiB')
txt(s, '88% less loaded RAM', 65, 470, 1050, 72, 44, WASM, True)
message(s, 'A smaller resident footprint could reduce memory stranded between traffic bursts.')
s['notes'] = ('Loaded values are medians of separate runs; paired median ratios show about 88% lower RAM. '
 'These are working-set observations, not required limits or whole-server RAM. '
 'Wasm working set increased from 278 to 347 MiB over repeated runs; it was not stationary.')

s = slide('The CPU cost moves in the opposite direction',
          'Same frozen footprint trials · 100 orders/s · both versions accepted all work', V)
bars(s, 65, 238, 545, 'Loaded application CPU', .275, .511, 'cores')
bars(s, 670, 238, 545, 'Order p95 latency', 21.58, 35.62, 'ms')
txt(s, 'Wasm: ~1.86× CPU', 65, 470, 1050, 72, 44, JAVA, True)
message(s, 'More memory capacity is useful only while CPU and response-time headroom remain.')
s['notes'] = ('Idle CPU medians were Java .0198 and Wasm .00283 cores. Loaded p95 run ranges: '
 'Java 20.94–22.92 ms, Wasm 30.03–50.94 ms. Java had lower p95 in every pair. '
 'CPU time is not a direct measure of electricity or carbon; no conversion is made.')

s = slide('Memory density: a strong, bounded result',
          '25 orders/s per application · CPU headroom · p95 target below 100 ms', V)
bars(s, 65, 238, 600, 'Smallest cap passing all three sweeps', 2048, 512, 'MiB')
card(s, 760, 236, 455, '8 GiB APPLICATION MEMORY POOL', '4 vs 16', 'Java vs Wasm allocations', WASM)
txt(s, '4× projected memory-only density', 65, 470, 1130, 67, 40, WASM, True)
message(s, 'Projection, not sixteen tested concurrent applications. CPU and infrastructure still count.', AMBER)
s['notes'] = ('Java 2 GiB passed all three; next 1 GiB suffered observed OOMs under equal service splits. '
 'This is not an optimized JVM minimum. Wasm 512 MiB passed all; 384 MiB passed two, '
 'third inconclusive for insufficient CPU telemetry, not proven saturation. At 256 MiB two OOMs. '
 'The 4-versus-16 projection divides an application-only 8 GiB pool by passing caps. '
 'It excludes shared platform overhead and concurrent interactions. Do not apply these short 25/s '
 'caps to long-lived 350/s workloads.')

s = slide('The machine profile can reverse the answer',
          'Illustrative packing ceilings from the 100/s resource profile, not deployment sizing.', V)
table(s, ['Usable application pool', 'Java copies', 'Wasm copies'], [
 ['4 GiB RAM / 4 CPU cores', '1 · memory bound', '7 · CPU bound'],
 ['32 GiB RAM / 4 CPU cores', '11 · memory bound', '7 · CPU bound'],
], [570, 290, 290], row_h=95)
txt(s, 'copies ≤ min(RAM pool ÷ RAM per copy, CPU pool ÷ CPU per copy)', 65, 490, 1130, 58, 26, MUTED)
message(s, 'Wasm favors memory-tight, CPU-rich hosts; Java may fit CPU-tight hosts better.')
s['notes'] = ('Arithmetic illustration only, using rounded footprint medians: Java 2739 MiB/.275 cores '
 'and Wasm 323 MiB/.511 cores at 100/s each. floor(min(memory/RAM, CPU/cores)). '
 '4 GiB gives Java1/Wasm7;32 GiB givesJava11/Wasm7. Pools are already assumed net of '
 'infrastructure reservations; additional safety headroom and peaks reduce counts. '
 'No such concurrent packing trial was run. Mean working sets are not safe resource limits. '
 'Wasm growth, databases, network contention, SLA and shared runtime scaling can invalidate '
 'linear extrapolation. These numbers demonstrate bottleneck reversal, not a sales forecast.')

s = slide('Startup: Wasm served the first order sooner',
          'Five paired starts per version · complete application scaled from zero · warm dependencies', V)
bars(s, 65, 238, 545, 'Container images cold', 24.32, 16.29, 's')
bars(s, 670, 238, 545, 'Container images cached', 15.11, 8.24, 's')
txt(s, '~33% sooner cold · ~45% sooner cached', 65, 470, 1130, 67, 37, WASM, True)
message(s, 'Faster activation could reduce pre-warmed capacity—if the complete scaling loop works.')
s['notes'] = ('Median first accepted order, fresh owned worker then same-worker restart. '
 'Java cold22.65–25.28s,cached14.80–25.66; Wasm cold13.34–17.36,cached5.35–9.20. '
 'Image-cold is not whole-machine cold boot; databases remain warm. '
 'Wasm component/provider caches are new on both starts. '
 'Potential reduction in idle reserve was not measured as energy or fleet savings.')

series = json.loads((ROOT/S).read_text())
startup = {k: statistics.median(p['implementations'][k]['new_pod_order_accepted_seconds'] for p in series['pairs']) for k in ('java','wasm')}
capacity = {k: statistics.median(p['implementations'][k]['capacity_ready_seconds'] for p in series['pairs']) for k in ('java','wasm')}
s = slide('Faster startup did not guarantee faster scale-out',
          'Earlier five-pair 400/s burst study · readiness definitions change the interpretation', S)
bars(s, 65, 238, 545, 'New pod: first accepted order', round(startup['java'],2), round(startup['wasm'],2), 's')
bars(s, 670, 238, 545, 'Harness: capacity-ready event', round(capacity['java'],2), round(capacity['wasm'],2), 's')
txt(s, 'Wasm starts quickly; serving capacity arrives through a wider control loop.', 65, 470, 1130, 80, 30, WHITE, True)
message(s, 'Do not promise fewer warm servers from pod startup time alone.', AMBER)
s['notes'] = ('Medians computed directly from the five-pair series.json. New-pod first-order '
 'median Java16.43/Wasm7.34seconds; capacity-ready Java18.04/Wasm23.33seconds. '
 'The harness event includes runtime/control-loop readiness, not a universal scale-ready definition. '
 'Java was already healthy under the target in at least some trials; '
 'this is not equal pre-scale overload. Prior automatic-scaling experiments were sensitive '
 'to triggers, placement, telemetry and workload. These exploratory profiles are separate '
 'from the later frozen footprint/startup studies and optimized capacity trials.')

s = slide('Tune allocation before declaring a capacity gap',
          'Same total application budget: 2 CPU cores / 4 GiB RAM', C)
bars(s, 65, 238, 600, 'Java: clean five-minute confirmed rate', 200, 350, '/s')
txt(s, 'Original split', 65, 425, 300, 40, 22, JAVA)
txt(s, 'Alternative split', 355, 425, 300, 40, 22, WASM)
card(s, 760, 236, 455, 'ONLY CPU DISTRIBUTION CHANGED', '+75%', 'Confirmed rate at the same budget')
message(s, 'Right-sizing the existing Java stack is also a consolidation opportunity.')
s['notes'] = ('On this slide both bars are Java: original equal500m per four services vs '
 'orders1200m,inventory600m,notifications150m,products50m. Memory1GiB per service unchanged. '
 'Matched200/s short-step p95 fell56.72→15.36ms. '
 'This allocation is orders-specific and made product startup several minutes; '
 'not recommended blindly for mixed traffic. Earlier apparent Wasm capacity advantage '
 'was confounded by CPU allocation. Bar legend is overridden to original/alternative below.')
# Override generic bar names for this allocation-only comparison.
for item in s['items']:
    if item.get('text') == 'Java': item['text'] = 'Equal'
    elif item.get('text') == 'Wasm': item['text'] = 'Tuned'

s = slide('At sustained 350/s, both accepted every issued order',
          'One fresh-pod 30-minute run each · tuned Java · identical 2-core / 4-GiB budgets', C)
table(s, ['Metric', 'Java', 'Wasm'], [
 ['Accepted orders', '629,954', '630,001'],
 ['HTTP / body failures', '0', '0'],
 ['Unscheduled arrivals', '47', '0'],
 ['p95 latency', '21.77 ms', '51.65 ms'],
], [570, 290, 290], row_h=64)
message(s, 'Wasm passed the strict gate; Java missed zero-drop by 0.0075% of planned work.')
s['notes'] = ('Dropped arrivals were never issued; these are not HTTP failures. '
 'Both pods remained ready/unchanged. Each long trial started fresh with3min25/s '
 'priming and3min100/s warmup, no preceding upper-rate probe. Wasm long run was a '
 'separately declared addendum; failed450/400confirmations are retained. '
 'Three paired short350/s trials passed each version. Exact throughput maxima unresolved; '
 '400/s varied, and finite generator workers/shared-machine stalls can cause drops. '
 'One long run each does not establish universal reliability.')

s = slide('Sustained load confirms the resource trade-off',
          '30-minute matched 350/s runs · application CPU and working-set memory', C)
bars(s, 65, 238, 545, 'Mean CPU consumed', .662, 1.178, 'cores')
bars(s, 670, 238, 545, 'Mean working-set RAM', 1249, 323, 'MiB')
txt(s, 'Wasm: 1.78× CPU · 74% less average RAM', 65, 470, 1130, 67, 38, WASM, True)
message(s, 'For consolidation, model both resources at the traffic each instance must handle.')
s['notes'] = ('CPU90s smoothed rates sampled15s; complete current-container coverage100%. '
 'Java .6617cores,Wasm1.1778. RAM1249.32/322.52MiB. '
 'These means do not measure hardware energy or establish safe CPU/memory limits. '
 'Do not compare absolute RAM directly with unlimited100/s footprints as if configurations matched.')

s = slide('The unresolved issue: Wasm memory kept growing',
          'First vs last five-minute mean RAM in the 30-minute 350/s trial', C)
table(s, ['Working-set memory', 'Java', 'Wasm'], [
 ['First five minutes', '1,215 MiB', '202 MiB'],
 ['Last five minutes', '1,288 MiB', '445 MiB'],
 ['Increase', '+6%', '+120%'],
], [570, 290, 290], row_h=82)
message(s, 'No demonstrated plateau. Long-lived density requires a longer memory investigation.', AMBER)
s['notes'] = ('First/last values are window means, not endpoints or a fitted linear trend. '
 'Wasm maximum468MiB;mean322.5. Could be pools,caches,allocator behavior or leak; cause unknown. '
 'Do not linearly extrapolate growth. Ending-window RAM still65%belowJava, but advantage narrowed. '
 'The512MiB25/s density cap is not validated for long-lived350/s operation.')

s = slide('Sustainability: what we can—and cannot—claim',
          'Resource measurements support a hypothesis; they do not measure its environmental outcome.', V+'; '+C)
rect(s, 65, 230, 550, 310); rect(s, 640, 230, 575, 310)
txt(s, 'SUPPORTED BY THE TESTS', 88, 250, 505, 42, 23, WASM, True)
txt(s, 'Less application RAM\nFaster first accepted order\nMore loaded CPU\nUsually higher p95 latency', 88, 312, 505, 205, 27)
txt(s, 'STILL TO BE DEMONSTRATED', 663, 250, 530, 42, 23, AMBER, True)
txt(s, 'Fewer physical servers\nLower kWh per accepted order\nLower lifecycle carbon\nStable long-term packed density', 663, 312, 530, 205, 27)
message(s, 'CPU-seconds and MiB are useful indicators. Neither is a carbon measurement.', MUTED)
s['notes'] = ('The proposed mechanism is consolidation of a memory-bound fleet. '
 'A system using more CPU may still enable a smaller fleet, or may instead hit a CPU bottleneck. '
 'Which outcome dominates requires real hosts, workload mixes, reservations and energy measurements. '
 'No emission factors, cloud prices or energy-per-core conversion have been assumed.')

s = slide('A practical replacement decision',
          'Choose by the bottleneck and service target, rather than runtime preference.', V+'; '+C)
table(s, ['Deployment profile', 'Assessment', 'Reason'], [
 ['Memory tight; CPU available', 'Pilot Wasm', 'Strongest density opportunity'],
 ['CPU tight; latency critical', 'Favor current Java', 'Lower CPU and p95 here'],
 ['Bursty; much idle capacity', 'Pilot Wasm', 'Smaller idle footprint; fast start'],
 ['Long-lived, high throughput', 'Investigate first', 'Memory plateau unresolved'],
], [440, 285, 425], row_h=65)
message(s, 'A targeted pilot is supported; a blanket migration claim is not.')
s['notes'] = ('These are recommendations inferred from measured trade-offs, not additional tests. '
 'Wasm reliability required a locally patched host and HTTP provider; '
 'the operational burden and maintenance of that patch must be part of adoption planning. '
 'Java allocation optimization must be retained in a fair production comparison.')

s = slide('The next proof should be fewer real machines',
          'A production-shaped consolidation trial, rather than another isolated latency benchmark.', C+'; '+V)
for i, (heading, body) in enumerate([
 ('1  Establish stability', 'Explain Wasm memory growth;\nconfirm its steady-state envelope.'),
 ('2  Pack real workloads', 'Same SLA and demand, full platform\noverhead, measured CPU headroom.'),
 ('3  Measure the outcome', 'Hosts needed, accepted orders,\nlatency, and whole-host kWh.'),
]):
    x=65+i*390
    rect(s,x,235,370,285)
    txt(s,heading,x+18,254,334,58,27,WASM,True)
    txt(s,body,x+18,333,334,160,25)
message(s, 'Success = fewer hosts for the same useful work and SLA, with lower measured energy.')
s['notes'] = ('Proposed future acceptance test, not an executed result. '
 'Include databases, brokers, operators, sidecars, storage, ingress and monitoring. '
 'Measure service completion if accepted orders are asynchronous. '
 'Use several trials and a long enough soak to resolve growth; '
 'do not aim for100%CPU if it breaks latency/headroom requirements. '
 'Measure actual host energy at equal delivered load instead of inferring it from utilization.')

s = slide('Evidence and reproducibility',
          'Keep the conclusion attached to the tested system and the retained raw results.', V+'; '+C)
txt(s, 'Frozen validation', 65, 235, 1130, 35, 25, WASM, True)
txt(s, 'monitoring/validation/RESULTS.md\n.results/20261106T204859Z/analysis.html', 65, 280, 1130, 74, 22, MUTED)
txt(s, 'Allocation, repeated capacity and long-run validation', 65, 380, 1130, 35, 25, WASM, True)
txt(s, 'monitoring/capacity/VALIDATION_RESULTS.md\n.results/validation-20261107T233519Z/analysis.html', 65, 425, 1130, 74, 22, MUTED)
message(s, 'Wasm is a credible consolidation candidate where memory—not CPU—is the constraint.')
s['notes'] = ('Frozen runtime: wasmCloud1.9.2-regack.1 and HTTP provider0.25.0-regack.1. '
 'Subscriber registration acknowledgement patches in both host/provider transports. '
 'Execution limit5s,RPC4s. Same frozen artifacts throughout validation; '
 'earlier transport failures and setup failures retained, not silently discarded. '
 'Protocol, source and artifact hashes, failed probes, restoration checks and trial reports '
 'are in the linked local evidence. Ignored .results files must be exported separately '
 'if distributing this repository; the deck itself is self-contained.')


def finish():
    for i,s in enumerate(slides,1):
        ref = 'S3' if s['source'] == S else 'S1 + S2' if '; ' in s['source'] else 'S1' if s['source'] == V else 'S2'
        txt(s, f'JAVA / WASM  ·  LOCAL BENCHMARKS  ·  08 NOV 2026  ·  SOURCE {ref}',65,675,950,24,13,MUTED)
        txt(s,f'{i:02d} / {len(slides):02d}',1115,673,100,26,15,MUTED)
    prs=Presentation(); prs.slide_width=Inches(13.333333);prs.slide_height=Inches(7.5)
    for s in slides:
        sl=prs.slides.add_slide(prs.slide_layouts[6]); sl.background.fill.solid();sl.background.fill.fore_color.rgb=RGBColor.from_string(BG)
        for e in s['items']:
            x,y,w,h=[Inches(e[k]/96) for k in ('x','y','w','h')]
            if e['kind']=='rect':
                sh=sl.shapes.add_shape(MSO_SHAPE.RECTANGLE,x,y,w,h);sh.fill.solid();sh.fill.fore_color.rgb=RGBColor.from_string(e['color']);sh.line.fill.background()
            else:
                box=sl.shapes.add_textbox(x,y,w,h);tf=box.text_frame
                tf.margin_left=tf.margin_right=tf.margin_top=tf.margin_bottom=0
                tf.word_wrap=True
                for j,line in enumerate(e['text'].split('\n')):
                    p=tf.paragraphs[0] if j==0 else tf.add_paragraph();p.text=line
                    p.font.name='Arial';p.font.size=Pt(e['size']*.75);p.font.bold=e['bold'];p.font.color.rgb=RGBColor.from_string(e['color'])
                    p.space_before=Pt(0);p.space_after=Pt(3)
        sl.notes_slide.notes_text_frame.text=s['notes']+'\n\nEvidence: '+s['source']
    prs.core_properties.title='Can Wasm help us run fewer servers?'
    prs.core_properties.subject='Sustainable computing: measured Java/Wasm trade-offs'
    prs.core_properties.author='Platform POC benchmark study'
    prs.save(OUT/'java-wasm-sustainable-computing.pptx')
    sections=[]
    for idx,s in enumerate(slides):
        elements=[]
        for e in s['items']:
            style=f"left:{e['x']}px;top:{e['y']}px;width:{e['w']}px;height:{e['h']}px;"
            if e['kind']=='rect':style+=f"background:#{e['color']};"
            else:style+=f"font-size:{e['size']}px;color:#{e['color']};font-weight:{700 if e['bold'] else 400};"
            elements.append(f'<div class="element" style="{style}">{html.escape(e.get("text",""))}</div>')
        evidence=[]
        for path in s['source'].split('; '):
            evidence.append(f'<a href="../../{html.escape(path)}">{html.escape(path)}</a>')
        sections.append(f'<section class="slide" id="s{idx+1}" aria-label="{html.escape(s["title"])}">'+''.join(elements)+
          f'<aside class="notes"><strong>Speaker notes</strong><p>{html.escape(s["notes"])}</p><p>Evidence: '+ ' · '.join(evidence)+'</p></aside></section>')
    css='''*{box-sizing:border-box}body{margin:0;background:#080e16;color:#f2f5fa;font-family:Arial,sans-serif}.slide{position:relative;width:1280px;height:720px;background:#101c2b;margin:28px auto;overflow:hidden}.element{position:absolute;white-space:pre-wrap;line-height:1.18}.notes{display:none}nav{position:fixed;bottom:8px;right:15px;z-index:9;background:#1a2b3f;padding:8px;border-radius:5px}button{font:15px Arial;background:#263f59;color:white;border:0;padding:8px;cursor:pointer;margin:2px}body.notes-on .slide{overflow:visible;margin-bottom:230px}body.notes-on .notes{display:block;position:absolute;top:728px;background:#1a2b3f;padding:18px;width:1280px;font-size:17px;line-height:1.4}a{color:#69aeff}body.present .slide{display:none;margin:0;position:fixed;left:50%;top:50%;transform:translate(-50%,-50%) scale(var(--zoom,1))}body.present .slide.active{display:block}@media print{@page{size:13.333in 7.5in;margin:0}body{background:#101c2b;print-color-adjust:exact;-webkit-print-color-adjust:exact}nav{display:none!important}.slide,body.present .slide{display:block!important;position:relative!important;left:auto!important;top:auto!important;transform:none!important;width:1280px;height:720px;margin:0!important;break-after:page;break-inside:avoid}.notes{display:none!important}}'''
    js='''let i=0;const ss=[...document.querySelectorAll('.slide')];function show(n){i=Math.max(0,Math.min(ss.length-1,n));ss.forEach((s,j)=>s.classList.toggle('active',j===i));document.documentElement.style.setProperty('--zoom',Math.min(innerWidth/1280,innerHeight/720));location.hash='s'+(i+1)}function present(){document.body.classList.toggle('present');document.body.classList.remove('notes-on');show(i)}function notes(){document.body.classList.remove('present');document.body.classList.toggle('notes-on')}onresize=()=>show(i);onkeydown=e=>{if(['ArrowRight','PageDown',' '].includes(e.key)){e.preventDefault();show(i+1)}if(['ArrowLeft','PageUp'].includes(e.key)){e.preventDefault();show(i-1)}if(e.key==='Escape')document.body.classList.remove('present');if(e.key==='p')present();if(e.key==='n')notes()};if(/^#s\\d+$/.test(location.hash))i=Number(location.hash.slice(2))-1;show(i);'''
    (OUT/'slides.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Java / Wasm · Sustainable computing</title><style>'+css+'</style>'+''.join(sections)+'<nav><button onclick="present()">Present (P)</button><button onclick="show(i-1)">←</button><button onclick="show(i+1)">→</button><button onclick="notes()">Notes (N)</button><button onclick="print()">Print / PDF</button></nav><script>'+js+'</script></html>')
    (OUT/'slide-content.json').write_text(json.dumps(slides,indent=2)+'\n')
    (OUT/'speaker-notes.txt').write_text('\n\n'.join(f'{i}. {s["title"]}\n{s["notes"]}\nEvidence: {s["source"]}' for i,s in enumerate(slides,1)))
    # Independent raster review of the shared geometry. Native PPTX text remains editable.
    regular='/System/Library/Fonts/Supplemental/Arial.ttf';bold='/System/Library/Fonts/Supplemental/Arial Bold.ttf'
    thumbs=[]; pages=[]
    for n,s in enumerate(slides,1):
        im=Image.new('RGB',(1280,720),'#'+BG);d=ImageDraw.Draw(im)
        for e in s['items']:
            x,y,w,h=[e[k] for k in ('x','y','w','h')]
            if e['kind']=='rect':d.rectangle((x,y,x+w,y+h),fill='#'+e['color']);continue
            font=ImageFont.truetype(bold if e['bold'] else regular,e['size']);lines=[]
            for line in e['text'].split('\n'):
                current=''
                for word in line.split(' '):
                    proposal=(current+' '+word).strip()
                    if current and d.textlength(proposal,font=font)>w:lines.append(current);current=word
                    else:current=proposal
                lines.append(current)
            for line in lines:
                d.text((x,y),line,font=font,fill='#'+e['color']);y+=e['size']*1.18
            if y>e['y']+h+5:raise ValueError(f'Slide {n} text exceeds box: {e["text"]}')
        im.resize((480,270)).save(OUT/f'preview-{n:02}.png')
        pages.append(im)
        thumbs.append(im.resize((480,270)))
    sheet=Image.new('RGB',(1440,math.ceil(len(thumbs)/3)*290),'#080e16')
    for n,im in enumerate(thumbs):sheet.paste(im,((n%3)*480,(n//3)*290))
    sheet.save(OUT/'overview.png')
    pages[0].save(OUT/'java-wasm-sustainable-computing.pdf',save_all=True,
                  append_images=pages[1:],resolution=96,title=prs.core_properties.title)
    sources=[V,C,S,'monitoring/validation/.results/20261106T204859Z/analysis.json',
             'monitoring/capacity/.results/validation-20261107T233519Z/analysis.json']
    manifest={'benchmark_cutoff':'2026-11-08','sources':[{'path':p,
               'sha256':hashlib.sha256((ROOT/p).read_bytes()).hexdigest()} for p in sources]}
    (OUT/'evidence-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(f'Built {len(slides)} slides in {OUT}')


if __name__=='__main__':finish()
