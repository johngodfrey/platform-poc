# Java / Wasm: sustainable computing presentation

Open [the editable PowerPoint](java-wasm-sustainable-computing.pptx) in PowerPoint or Keynote, or open [the offline browser deck](slides.html). Both contain 18 slides and speaker notes. The PowerPoint uses editable text and chart shapes.

A [static PDF](java-wasm-sustainable-computing.pdf) is included for easy sharing. It contains rasterized slides without notes; use PowerPoint or HTML for editable or selectable text.

In the browser, click **Present** (or press P), use the arrow keys to navigate, and press Escape to return to the slide overview. **Notes** (N) reveals the supporting explanation and evidence links. **Print / PDF** exports one slide per page; enable background graphics if your browser requires it. The browser deck contains all presentation content and does not need a server or internet connection.

[overview.png](overview.png) previews every slide. [speaker-notes.txt](speaker-notes.txt) contains the full presenter script and evidence locations.

The narrative is that Wasm is a credible consolidation candidate for memory-constrained deployments with CPU headroom. These tests do not establish lower whole-server energy, carbon emissions, or actual concurrent fleet density. The deck retains the CPU cost, Java allocation improvements, mixed scale-out readiness results, dropped arrivals, and unresolved Wasm memory growth. Capacity maxima are not claimed.

Evidence labels in slide footers:

- **S1:** [Frozen validation](../../monitoring/validation/RESULTS.md), November 6–7: five footprint pairs, three density sweeps per version, five cold/cached startup pairs per version.
- **S2:** [Allocation, repeated capacity and stability](../../monitoring/capacity/VALIDATION_RESULTS.md), November 8: Java allocation control, three alternating capacity pairs and matched 30-minute 350/s runs.
- **S3:** [Earlier five-pair scale-out series](../../monitoring/scaleout/.results/repeated-20261103T234859Z/series.json): median new-pod first-order and harness capacity-ready times. This exploratory study uses a different profile and is not pooled with S1 or S2.

Detailed raw results are in ignored `.results` directories. They may need to be exported separately for another person to inspect the evidence. The slides, notes and plotted numbers remain available without them. `evidence-manifest.json` records the hashes of the local sources used when generating this delivery.

Slide 8 is explicitly a hypothetical arithmetic illustration. It uses measured application working sets and CPU consumption to demonstrate how the limiting resource changes with machine shape. It is not a safe resource-limit recommendation, a measured concurrent packing result, or a fleet forecast. Slide 7 instead uses the passing memory limits from the separate short 25/s density study. Neither projection should be extrapolated to long-lived high-rate workloads.

To regenerate locally:

```sh
python3 -m venv /tmp/platform-slides-venv
/tmp/platform-slides-venv/bin/pip install python-pptx==1.0.2 Pillow==13.0.0
/tmp/platform-slides-venv/bin/python presentations/sustainable-computing/build_deck.py
```

The preview renderer currently uses macOS Arial font paths. PowerPoint and HTML content generation uses no cluster access and does not run tests or alter deployments. The deck covers results available through November 8, 2026.
