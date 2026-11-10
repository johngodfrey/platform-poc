"""Read-only classification of retained benchmark evidence; never judge validity by speed."""
import hashlib
import html
import json
from pathlib import Path

SUITES = ('footprint','budget','density','capacity','scaleout','scalezero','startup_cache','autoscale')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''): h.update(block)
    return h.hexdigest()


def classify(path):
    """Roles describe available evidence, not whether a result favors an implementation."""
    names = {p.name for p in path.iterdir() if p.is_file()}
    documents = {}
    for name in ('report.json','series.json','summary.json'):
        if name in names:
            try: documents[name] = json.loads((path/name).read_text())
            except (ValueError, OSError): pass
    report = documents.get('report.json', documents.get('series.json', {}))
    role = 'historical_observation'
    reasons = []
    if path.name.startswith('comparison-') or report.get('source_reports') or report.get('source_directories'):
        role = 'derived_summary'; reasons.append('Links or combines underlying trials; do not count as independent measurements')
    elif 'soak' in path.name or 'preflight' in path.name or path.name.startswith('baseline-'):
        role = 'diagnostic_or_metadata'; reasons.append('Preparation or infrastructure diagnostic')
    elif not documents:
        role = 'partial_or_metadata'; reasons.append('No top-level parseable report/summary; inspect raw files and checkpoints')
    elif report.get('error') or report.get('status') in ('failed','running','interrupted'):
        role = 'partial_or_failed_attempt'; reasons.append('Contains an error or unfinished series; completed subtrials remain evidence')
    settings = report.get('settings', {})
    if role == 'historical_observation' and (settings.get('rate',report.get('rate',100)) <= 5 or settings.get('seconds',60) < 30):
        role = 'diagnostic_smoke'; reasons.append('Short or low-rate protocol; not pooled with full benchmarks')
    provenance = dict(source_hashes=bool(report.get('source_sha256') or report.get('driver_sha256') or any('driver' in n for n in names)),
                      resource_telemetry=any(p.name in ('telemetry.json','samples.jsonl','resources.jsonl') for p in path.rglob('*') if p.is_file()))
    if path.parent.parent.name == 'footprint' and 'summary.json' in documents:
        reasons.append('HTTP summary retained; CPU/memory conclusions require archived resource telemetry')
    if report.get('implementations'):
        reasons.append('Per-stage pass/fail/inconclusive outcomes remain distinct; a failed result is not an invalid experiment')
    return dict(path=str(path), role=role, reasons=reasons, provenance=provenance,
                status=report.get('status'), error=report.get('error'), settings=settings,
                restored=report.get('restored'), fresh_validation_pool=False,
                pool_reason='Historical protocol/provenance differs from the newly frozen campaign; retain as supporting or diagnostic evidence')


def inventory(root):
    entries, files = [], []
    for suite in SUITES:
        directory = root/suite/'.results'
        if not directory.exists(): continue
        for run in sorted(directory.iterdir()):
            if run.is_dir(): entries.append(classify(run))
        for p in sorted(directory.rglob('*')):
            if p.is_file() and not p.is_symlink():
                files.append(dict(path=str(p.relative_to(root.parent)), bytes=p.stat().st_size, sha256=digest(p)))
    for row in entries: row['path'] = str(Path(row['path']).relative_to(root.parent))
    return dict(runs=entries, retained_files=files,
                policy='Original files are untouched. Roles may overlap in evidential value. Negative outcomes are retained; derived reports are not independent trials.')


def page(data):
    esc = lambda s: html.escape(str(s))
    rows = ''.join('<tr>'+''.join('<td>'+esc(x)+'</td>' for x in (r['path'],r['role'],'; '.join(r['reasons']),r['provenance']))+'</tr>' for r in data['runs'])
    return '<!doctype html><meta charset="utf-8"><title>Historical evidence</title><style>body{font:16px system-ui;margin:30px}td,th{padding:10px;border:1px solid #ccc}table{border-collapse:collapse}</style><h1>Retained historical evidence</h1><p>'+esc(data['policy'])+'</p><p>All historical runs stay outside the new validation pool. This is a provenance decision, not exclusion of unfavorable results.</p><table><tr><th>Run</th><th>Role</th><th>Interpretation</th><th>Archived evidence</th></tr>'+rows+'</table>'
