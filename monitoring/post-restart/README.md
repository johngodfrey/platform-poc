# Post-restart benchmark campaign

This campaign repeats the benchmark families using frozen copies of the existing drivers. Its journal and every new raw output live under `.results/<campaign>/`, separate from all earlier suite result directories. Every study is identified as **post-restart** by membership in that campaign; the top-level HTML report links the individual raw reports. Existing data is not pooled or overwritten.

The scheduled studies are the frozen footprint/density/cold-cache validation, standalone five-minute orders per platform, fixed-budget capacity, five burst scale-out pairs, five scale-from-zero pairs, CPU- and latency-triggered automatic scaling, and allocation sensitivity/repeated saturation/30-minute stability. If the capacity matrix skips a long trial, its predeclared common-step fallback is attempted once. Troubleshooting endpoints and intentionally broken historical configurations are not benchmark families and are not reintroduced.

Defaults and commands are captured in `protocol.json`. Source snapshots, the inventory of historical file hashes, private restoration state and live configuration hashes are retained in the campaign. Source copies preserve measurement settings; the frozen-validation inventory adapter points its source/git checks to the original repository. No runtime images or transport settings are upgraded.

Open `.results/ACTIVE.json` to find the current campaign, then its `report.html` to follow progress. Execution completion and performance-gate pass/fail are distinct. Failed attempts are retained. A new independent study runs only after application templates, replica counts, image identities, configuration hashes and functional order checks confirm restoration.

The campaign needs mains power and an open lid. A continuous sleep inhibitor covers the complete campaign, including gaps between child studies. Only one implementation is loaded at a time. Resource, replica and placement changes made by individual drivers use their saved restoration checkpoints.

Do not rerun `run.py` against an existing campaign journal: it deliberately refuses to overwrite it. Interrupted studies require inspection of their checkpoint before a separately recorded continuation.
