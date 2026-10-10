# Multi-arm evaluation campaigns (AERI)

[中文](AUTOMATIC_CAMPAIGN.zh-CN.md)

**Status: library only.** This page will describe how the automatic
evaluation pipeline (AERI) compares several policies (arms) on one task.
Two parts exist so far, and no command, page or API calls them yet: the
statistical methods (`levi/automatic/analysis/`, pure functions) and the
figure writers that draw their results for the web page and for a paper
(see [Figures](#figures)). The campaign plan, schedule, report and page are
later work and get their own sections here.

## Statistical methods

`levi.automatic.analysis` turns trial outcomes into numbers: point
estimates, intervals, p-values and diagnostics. It writes no sentences; the
report generator decides how to word them.

**Guarantees.**

- Standard library and numpy only; no file is read and no other LEVI module
  is imported, except `levi.live.stats.wilson`, so the live page and the
  reports print the same Wilson interval.
- Every random draw takes an explicit `seed` and its own generator (PCG64);
  there is no cache or module state. With the **same numpy version** the
  same input gives the same output, bit for bit, in any process and from any
  number of threads (tested). Across numpy versions the random streams of
  `Generator` methods may change (numpy does not promise them stable; this
  project's `uv.lock` resolves numpy 2.4.6 for Python below 3.12 and 2.5.3
  above), so bootstrap and permutation results then agree only within Monte
  Carlo error (tested with different streams). Every result records
  `numpy_version`, `bit_generator` and `algorithm_version`.
- Every result function returns a JSON-ready dict with `schema_version`
  (`levi.aeri.analysis.v1`), `method`, `implementation`
  (`levi.automatic.analysis.<module>@<version>`), `references` (keys of the
  citation table `levi/automatic/analysis/references.py`), `exploratory`
  and `caveats` (each with a stable `code`). The exceptions are the numeric
  helpers `power_paired`, `power_unpaired` and `min_detectable_difference`,
  which return a bare number for planning code.
- Missing values (`None`, NaN) are dropped and counted in the result.
  Infinite values, impossible counts and bad options raise
  `AnalysisInputError`. An empty arm gives `available: false` and no numbers
  (no p-value either).
- Work is bounded: at most 10^5 bootstrap resamples or permutations; large
  samples are resampled in chunks, or through category counts when the data
  take few values (100 000 pairs take well under a second).

**Exploratory or not.** The decision uses planned values only, never the
outcomes. A comparison of success rates is confirmatory (`exploratory:
false`) only when the caller passes the difference the study was designed
to detect (`design_difference`) and, optionally, the planned baseline
success rate (`design_baseline`), both fixed before the first trial, and
the power at that difference reaches 80 % with the number of trials
used: exact power by enumeration up to 200 trials per arm, a normal
approximation above that (slightly optimistic, see the sample-size row
below). Without a planned baseline the least favourable baseline on a 0.05
grid decides. Each result returns `power_basis` (`basis: "planned"`, the
planned values, the smallest power found and the baseline that gave it,
`least_favourable_baseline`) and the smallest difference
its sample size can detect at the planned baseline, or at 0.5 (the design
table's baseline) when none was planned.

Why not the observed success rate: power computed from the observed data
(post-hoc power) is a function of the observed p-value and adds nothing to
it, and it would let one pre-registered design turn confirmatory just
because the results came out extreme. The same planned values and the same
number of trials therefore always give the same `exploratory`, whatever
the outcomes (tested).

Results with no power model (continuous metrics, survival, omnibus tests,
diagnostics) are always exploratory. Multiplicity adjustments (`holm`,
`bonferroni`) are confirmatory only when every test in the family was
(pass each test's flag as `exploratory=[...]`); Benjamini–Hochberg is a
screen and always exploratory. A single test's `exploratory: false` does
not account for multiplicity. Report conclusions also depend on
pre-registration, label basis and drift checks, which the report generator
applies.

### Methods

Defaults: 95 % intervals, two-sided alpha 0.05. "Exact" means computed from
the full discrete distribution, not from a normal approximation.

| Question | Function | Method | Use when | Limits |
| --- | --- | --- | --- | --- |
| One arm's success rate | `proportion` | Wilson score interval (primary) and Clopper–Pearson exact interval | Always | Clopper–Pearson is conservative (coverage at least 95 %, often more) |
| Two arms on the same layout cards | `mcnemar` | McNemar exact conditional test, its mid-p version and the asymptotic statistic | Paired trials (same card, same round) | Uses only discordant pairs; the asymptotic value is unreliable below 10 of them; above 2000 discordant pairs the exact sums run in log space |
| | `newcombe_paired` | Newcombe's paired score interval (method 10) for p_B − p_A | Paired trials | Built from Wilson intervals; not an exact interval |
| | `paired_bootstrap` | Bootstrap of the mean (or median) difference, resampling pairs; percentile, or BCa on request | Paired binary or continuous outcomes | Liberal in small samples (see below); a sample whose differences all take one value gives a zero-width interval, flagged `degenerate_bootstrap`: use McNemar and Newcombe instead; BCa needs a jackknife |
| Two independent arms | `fisher_exact` | Fisher's exact test (two-sided by summing tables no more likely than the observed one) | Unpaired trials (reset-policy mode, deviated cards) | Conditional on both margins; conservative; above 4000 trials the sums run in log space |
| | `boschloo_exact` | Boschloo's unconditional test (Fisher's p-value as statistic, maximised over the common rate on a grid with local refinement) | Unpaired trials, more power than Fisher | The maximum is numerical; refused above 300 trials per arm |
| | `newcombe_independent`, `agresti_caffo` | Newcombe's hybrid score interval (method 10); Agresti–Caffo add-two interval for comparison | Unpaired trials | Approximate intervals |
| | `posterior_prob_greater` | P(p_B > p_A) under uniform Beta(1, 1) priors, computed exactly | Descriptive only | Not a test |
| More than two arms | `cochran_q`, `friedman` | Cochran's Q (binary) and Friedman's rank test (continuous) per block; p-value by permuting arms within blocks | Every arm ran once per block | Omnibus only: says whether arms differ, not which |
| Several comparisons | `holm` | Holm's step-down adjustment | The pre-registered primary pairwise family | Controls the family-wise error under any dependence |
| | `bonferroni` | Bonferroni adjustment | A single threshold (letter displays) | Never more powerful than Holm |
| | `benjamini_hochberg` | Benjamini–Hochberg false discovery rate | Exploratory secondary metrics | Proven for independent tests; metrics on the same trials are dependent, so it is a screen |
| Continuous metrics | `wilcoxon_signed_rank` | Wilcoxon signed-rank test; exact up to 50 non-zero pairs (ties included), normal approximation beyond | Paired completion time, steps | Zero differences are dropped |
| | `mann_whitney` | Mann–Whitney rank-sum test; exact up to 60 values | Unpaired metrics | Tests stochastic ordering, not means |
| | `hodges_lehmann` | Hodges–Lehmann shift (median of Walsh averages or of cross differences) with a percentile bootstrap interval | Effect size of a shift | Interval skipped above 400 values; estimate refused above about 4.5 million averages |
| | `cliffs_delta`, `improvement_share` | Cliff's delta (unpaired); share of pairs in which B is better (paired) | Ordinal effect sizes | Ignore magnitude |
| | `smoothness` | SPARC (spectral arc length of the speed) and velocity-based log dimensionless jerk | Movement smoothness from end-effector positions | Depend on the sampling rate and on where the movement is cut: compare only like with like |
| Time to success | `kaplan_meier`, `logrank`, `rmst` | Kaplan–Meier curve (Greenwood variance, log(−log) interval); log-rank test for k arms; restricted mean survival time up to a common horizon with a bootstrap interval for the difference | Episodes that fail or stop are right-censored | Fault and operator stops are competing events, treated here as censoring; a horizon beyond an arm's last observation carries its curve flat and is flagged `tau_beyond_follow_up`. Greenwood's variance and the tie-corrected Mann–Kendall variance are standard formulas without a separate citation |
| Failure modes | `failure_modes` | Counts by `stop_reason` and post-hoc failure class, each with a Wilson interval | Every report | Descriptive; no test |
| Early termination | `early_stop` | False early stop rate from failed, complete control episodes (Wilson); paired across arms by layout slot with McNemar | Arms with early stop enabled | Pairs by (slot, round); controls without a match are counted in `unpaired`, and two controls of one arm with the same key are refused. `unavailable` without such control episodes; treated episodes give only a lower bound |
| Verdict versus person | `agreement`, `cohen_kappa`, `misjudgement_by_arm`, `rogan_gladen` | Confusion matrix, agreement rate (Wilson), false and missed success rates (live-page definitions), Cohen's kappa; permutation test of a different error rate between arms; Rogan–Gladen correction | Every report with human labels | Kappa depends on the success rate (read it beside the agreement rate); Rogan–Gladen is a sensitivity analysis |
| Sample size | `power_table`, `min_detectable_difference` | Exact power by enumeration: Fisher (unpaired) and McNemar with within-pair correlation (paired); smallest detectable difference on a 0.01 grid for n = 10…100 | Planning, and the caveat on every comparison | Above 200 trials per arm a normal approximation (Connor's formula for pairs) replaces enumeration; it can understate the detectable difference by about 0.01 (optimistic) and says so in `power_approximate` |
| Drift and order | `reference_drift`, `arm_time_interaction`, `carryover`, `drift_warning` | Mann's trend test on the reference arm's per-round rate plus first- versus second-half difference; permutation test of the B − A difference between halves; success by the previous arm; one flag when any p < 0.05 | Campaigns with a reference arm and several rounds | Diagnostics: they find problems and never adjust results |

**Bootstrap coverage.** Measured with this library: 1000 simulated data
sets of 30 pairs each, 2000 resamples per data set, seed 20261010 (Monte
Carlo standard error about 0.007). The nominal 95 % percentile interval
covered the true mean difference in 0.93 of data sets for normal
differences N(0.4, 1), 0.91 for exponential differences Exp(1), and 0.95
for binary pairs with independent outcomes A ~ Bernoulli(0.5),
B ~ Bernoulli(0.65); BCa covered 0.94, 0.92 and 0.94. For binary pairs the
coverage depends on the success rates: an independent re-check over other
rates (0.5/0.5, 0.2/0.4, 0.7/0.9, 0.5/0.6) found 0.93 to 0.95. Each
bootstrap result repeats this in `coverage_note`; below 30 pairs it adds
the `small_sample` caveat.

**Planning numbers.** With 80 % power at two-sided alpha 0.05 and a
baseline success rate of 0.5, the smallest detectable difference is 0.42
(unpaired) and 0.44 (paired, no within-pair correlation) at 20 trials per
arm, 0.36 and 0.37 at 30, and 0.29 and 0.29 at 50 (`power_table`; the tests
recompute each number by brute-force enumeration and by simulation). Arms of
20–30 trials resolve only large differences.

### References

All entries were checked against their DOI or arXiv record (2026-10-10).

- Agarwal et al. 2021, "Deep Reinforcement Learning at the Edge of the Statistical Precipice", NeurIPS 2021, arXiv:2108.13264.
- Agresti and Caffo 2000, "Simple and Effective Confidence Intervals for Proportions and Differences of Proportions Result from Adding Two Successes and Two Failures", The American Statistician 54(4), doi:10.1080/00031305.2000.10474560.
- Balasubramanian, Melendez-Calderon and Roby-Brami 2015, "On the analysis of movement smoothness", Journal of NeuroEngineering and Rehabilitation 12:112, doi:10.1186/s12984-015-0090-9.
- Benjamini and Hochberg 1995, "Controlling the False Discovery Rate", JRSS B 57(1), doi:10.1111/j.2517-6161.1995.tb02031.x.
- Boschloo 1970, "Raised conditional level of significance for the 2 x 2-table when testing the equality of two probabilities", Statistica Neerlandica 24(1), doi:10.1111/j.1467-9574.1970.tb00104.x.
- Brown, Cai and DasGupta 2001, "Interval Estimation for a Binomial Proportion", Statistical Science 16(2), doi:10.1214/ss/1009213286.
- Cliff 1993, "Dominance statistics: Ordinal analyses to answer ordinal questions", Psychological Bulletin 114(3), doi:10.1037/0033-2909.114.3.494.
- Clopper and Pearson 1934, "The Use of Confidence or Fiducial Limits Illustrated in the Case of the Binomial", Biometrika 26(4), doi:10.1093/biomet/26.4.404.
- Cochran 1950, "The Comparison of Percentages in Matched Samples", Biometrika 37(3-4), doi:10.1093/biomet/37.3-4.256.
- Cohen 1960, "A Coefficient of Agreement for Nominal Scales", Educational and Psychological Measurement 20(1), doi:10.1177/001316446002000104.
- Connor 1987, "Sample Size for Testing Differences in Proportions for the Paired-Sample Design", Biometrics 43(1), doi:10.2307/2531961.
- Dunn 1961, "Multiple Comparisons among Means", JASA 56(293), doi:10.1080/01621459.1961.10482090.
- Efron 1979, "Bootstrap Methods: Another Look at the Jackknife", Annals of Statistics 7(1), doi:10.1214/aos/1176344552.
- Efron 1987, "Better Bootstrap Confidence Intervals", JASA 82(397), doi:10.1080/01621459.1987.10478410.
- Fagerland, Lydersen and Laake 2013, "The McNemar test for binary matched-pairs data: mid-p and asymptotic are better than exact conditional", BMC Medical Research Methodology 13:91, doi:10.1186/1471-2288-13-91.
- Feinstein and Cicchetti 1990, "High agreement but low Kappa: I. the problems of two paradoxes", Journal of Clinical Epidemiology 43(6), doi:10.1016/0895-4356(90)90158-L.
- Fisher 1922, "On the Interpretation of chi-squared from Contingency Tables, and the Calculation of P", JRSS 85(1), doi:10.2307/2340521.
- Friedman 1937, "The Use of Ranks to Avoid the Assumption of Normality Implicit in the Analysis of Variance", JASA 32(200), doi:10.1080/01621459.1937.10503522.
- Hodges and Lehmann 1963, "Estimates of Location Based on Rank Tests", Annals of Mathematical Statistics 34(2), doi:10.1214/aoms/1177704172.
- Holm 1979, "A Simple Sequentially Rejective Multiple Test Procedure", Scandinavian Journal of Statistics 6(2):65-70, https://www.jstor.org/stable/4615733.
- Kaplan and Meier 1958, "Nonparametric Estimation from Incomplete Observations", JASA 53(282), doi:10.1080/01621459.1958.10501452.
- Kress-Gazit et al. 2024, "Robot Learning as an Empirical Science: Best Practices for Policy Evaluation", arXiv:2409.09491.
- Mann 1945, "Nonparametric Tests Against Trend", Econometrica 13(3), doi:10.2307/1907187.
- Mann and Whitney 1947, "On a Test of Whether one of Two Random Variables is Stochastically Larger than the Other", Annals of Mathematical Statistics 18(1), doi:10.1214/aoms/1177730491.
- Mantel 1966, "Evaluation of survival data and two new rank order statistics arising in its consideration", Cancer Chemotherapy Reports 50(3):163-170, https://pubmed.ncbi.nlm.nih.gov/5910392/.
- McNemar 1947, "Note on the Sampling Error of the Difference Between Correlated Proportions or Percentages", Psychometrika 12(2), doi:10.1007/BF02295996.
- Newcombe 1998a, "Interval estimation for the difference between independent proportions: comparison of eleven methods", Statistics in Medicine 17(8), doi:10.1002/(SICI)1097-0258(19980430)17:8<873::AID-SIM779>3.0.CO;2-I.
- Newcombe 1998b, "Improved confidence intervals for the difference between binomial proportions based on paired data", Statistics in Medicine 17(22), doi:10.1002/(SICI)1097-0258(19981130)17:22<2635::AID-SIM954>3.0.CO;2-C.
- Rogan and Gladen 1978, "Estimating prevalence from the results of a screening test", American Journal of Epidemiology 107(1), doi:10.1093/oxfordjournals.aje.a112510.
- Royston and Parmar 2013, "Restricted mean survival time: an alternative to the hazard ratio for the design and analysis of randomized trials with a time-to-event outcome", BMC Medical Research Methodology 13:152, doi:10.1186/1471-2288-13-152.
- Wilcoxon 1945, "Individual Comparisons by Ranking Methods", Biometrics Bulletin 1(6), doi:10.2307/3001968.
- Williams 1949, "Experimental Designs Balanced for the Estimation of Residual Effects of Treatments", Australian Journal of Scientific Research A 2(2), doi:10.1071/CH9490149.
- Wilson 1927, "Probable Inference, the Law of Succession, and Statistical Inference", JASA 22(158), doi:10.1080/01621459.1927.10502953.

## Figures

A figure is a `FigureSpec`: a plain, versioned description (schema
`levi.aeri.figure_spec.v1`) with no drawing code in it. The analysis code
produces one spec per figure; the web page maps the same JSON onto Recharts,
and the two writers here turn it into SVG and PDF:

| Function | Output |
| --- | --- |
| `svgplot.render_svg(spec, lang=None, embed_spec=False, width=640)` | SVG text (UTF-8) |
| `pdfplot.render_pdf(spec, lang=None, width=640)` | One-page vector PDF (bytes) |
| `levi.automatic.figure_files.write_figure(spec, path_stem, formats=("svg", "pdf"), lang=None, embed_spec=False, strict=False)` | `<path_stem>.svg` and/or `.pdf` on disk; returns one record per format for a report manifest |
| `figspec.table(spec)`, `table_csv`, `table_html` | The accessible table: every number in the figure |

The analysis package never touches files (it only returns text and bytes);
`write_figure` lives outside it. It renders every format first, so a figure
that cannot be drawn writes nothing, then writes each file through a
temporary file in the same folder, `fsync`, rename and `fsync` of the folder:
a crash leaves the old file or the new one, never half a file. Each record
has `path`, `bytes` and `sha256`; the PDF record adds the substitution report
described under "Language limits". The folder must exist.

Pure standard library: no matplotlib, no Pillow, nothing to install. Output is
deterministic: no clock, no random ids, no `/ID` or dates in the PDF, so the
same spec gives the same bytes and two campaign reports can be compared with
`diff`.

### Figure kinds

| `kind` | Meaning | Axes |
| --- | --- | --- |
| `grouped_bar` | Success rate per arm with an interval | category x, linear y |
| `forest` | Paired differences, one row per comparison, reference line at 0 | linear x, category y |
| `step_curve` | Time to success: a staircase per arm, optional band | linear x, linear y |
| `stacked_bar` | Failure modes stacked per arm | category x, linear y |
| `early_stop` | Early-stop saving and error rate; one to four panels | category x, linear y |
| `drift_lines` | Per-round rate per arm with intervals; the reference arm is heavier | linear or category x, linear y |
| `confusion_matrix` | Judge agreement: one matrix per panel, each cell shaded by its share of the row and labelled with count and share | category x and y |

A `Point` is `x`, `y`, an optional interval `lo`/`hi` on the value axis (`y`;
`x` in a forest plot) and an optional short `label` such as `8/20`. Titles,
summaries, axis labels, series names and notes can be given in English and
Chinese (`{"en": ..., "zh-CN": ...}`); a missing language falls back to
English. `validate()` refuses what cannot be drawn truthfully: unknown kinds,
non-finite numbers, `lo` above `hi`, a category index out of range, a slot
given twice, a series that goes backwards in x, negative stacked values,
control characters in text, duplicate series names in one panel, and
intervals on a stacked bar (it does not support them). Drawing refuses
**data outside a fixed axis range**: a value is never clipped silently.
A matrix cell carries its count in `Point.value`; `value` is for
`confusion_matrix` only.

### Intervals are never lost quietly

* On a step curve a point's interval holds, like its value, from the point
  to the next point; consecutive points with intervals share one band, and
  the band reaches the next point even when that point has no interval. A
  point with an interval and nothing to its right (the curve's last point,
  a one-point curve, or a next point at the same x) gets an error bar, so
  the end of the curve shows its interval too. A point without an interval
  breaks the band. All bands are drawn first, then all curves, so a band
  never hides another arm's curve.
* A point that lacks an interval in a series that has intervals elsewhere is
  marked `interval unavailable` in the table, and the figure says how many
  points that is.
* **At x = 0 a step curve with no interval means no uncertainty**: the
  Kaplan-Meier result has no row at t = 0, so the adapter adds the start
  point (0, 0) without an interval (see "Interface with the analysis
  library"). The value is its own interval, the table says
  `no uncertainty at 0`, and nothing is reported as missing.
* After drawing, `validate_render(spec, scene)` measures every interval of
  the spec on the page: an error bar must span it, and a band must have
  width at that point and span it there (within 0.01 pt). A missing mark, a
  band of zero width or an error bar collapsed to a point raises
  `ValueError`, unless the interval is a single value (for example the
  Kaplan-Meier interval [0, 0] once every episode succeeded). `layout()`
  calls it, so a drawing function that loses an interval cannot produce a
  figure.
* An estimate outside its interval (legitimate for a bootstrap percentile or
  BCa interval, or a median) is kept, flagged `estimate outside interval` in
  the table and counted under the figure. `warnings(spec)` lists these and
  the unavailable items as codes such as
  `estimate_outside_interval:panel0/series1/point0`.
* A group with no data is a series with `unavailable=True` and no points: it
  keeps its legend entry (`Arm D (unavailable)`), gets a table row and a count
  under the figure, and draws nothing.

### Several panels

All panels share one legend, built from the union of their series; a series
keeps its colour, marker, line and hatch by **name**, whatever its position in
a panel.

### Reading a figure without colour

* The palette has eight colours that stay at least 15 CIELAB units apart under
  protan, deutan and tritan simulation, ordered so that neighbours also differ
  in grayscale (tested).
* Every series also has its own marker shape, line style and, for bars, a
  hatch, so a black-and-white copy still reads. The reference arm
  (`emphasis=True`) is drawn heavier.
* Arm names are always printed (legend, tick labels), never left to colour.
* Axis ticks use 1, 2 or 5 times a power of ten; a constant series gets a
  readable window.
* Colour is guaranteed apart for every pair, and grayscale only for
  neighbouring colours in the palette order; the hatch, marker and line style
  carry the rest.
* In `drift_lines` the arms are shifted by up to 3 pt sideways so their
  intervals do not overprint; on a numeric x axis that is a drawing offset,
  not a change of the data.
* The SVG carries `<title>` and `<desc>` (the title and the one-sentence
  summary) and, with `embed_spec=True`, the spec itself in `<metadata>`.
  Put `table_html(spec)` next to the figure in a page so that screen-reader
  users get the numbers.

### Language limits

SVG text is live text in a generic font family (a CJK family is added when the
text needs one). The PDF uses the standard Helvetica and Helvetica-Bold fonts
(nothing is embedded; every reader has them), so it is **Latin only**, and
nothing is replaced silently. For each text that Windows-1252 cannot show, the
PDF uses, in this order: a form in another language that fits (English); a
transliteration of statistics symbols (`Δ` to `Delta`, `−` to `-`, `α` to
`alpha`, `≥` to `>=`, ...); `?` marks, or `[n/a]` when nothing readable is
left. A series name with nothing readable left becomes `Series 1`,
`Series 2`, ... (its legend position) instead, so the arms can still be told
apart. `pdfplot.render_pdf_report(spec, lang, strict=False)` returns the
bytes and `substitutions`, a list of `{"text", "to", "reason"}` records
(`transliterated`, `fallback_en`, `unencodable`, `numbered`), plus the
language the title is really in (also written to the PDF `/Lang`); its
`manifest()` gives `{"pdf": "ok" | "lossy(n)", ...}` for a report manifest.
`strict=True` raises `LossyTextError` instead. `render_pdf` returns only the
bytes, so a report generator uses `render_pdf_report` or `write_figure`
(whose PDF record carries the same report) and writes the substitutions into
its manifest. Write Chinese figures as SVG, or give every text an English
form.

Titles, summaries and notes are cut to 300, 600 and 400 characters (3, 4 and 6
lines each), legend names to 60, panel titles to 80, reference-line labels
to 40, point labels to 40 and axis labels to 100 characters; `Scene.truncated`
names what was cut. The table keeps the full text. Layout time
is linear in the text length.
There is no PNG writer; PNG export stays in the web page (the browser draws the
SVG), and a command-line PNG is made only if a converter such as
`rsvg-convert` is on the PATH.

### Checking the PDF

`pdfplot.verify_pdf(data)` re-reads a file with its own small parser: header,
cross-reference offsets, trailer, page tree, stream length, fonts and end
marker, and returns the page size and every string shown. The tests also run
`pdftotext` and `pdfinfo` (poppler) when they are installed. The reader also tokenises the page
content: only the operators this writer emits, with the right operand counts,
`q`/`Q` and `BT`/`ET` balanced, text only inside `BT`/`ET` and only declared
fonts; a dangling object reference is a `PdfError`. A human should open one
generated PDF in a normal viewer once per release to check how it looks: the
tests prove the structure and the text, not the visual layout.

Golden SVG files live in `tests/automatic/analysis/test_fig_golden/`; after an
intended change, regenerate them with `LEVI_UPDATE_GOLDEN=1` and read the diff.

### Interface with the analysis library

The figure side only needs plain numbers. The report generator (T-CP-06)
writes the adapter from the analysis results to `FigureSpec`;
`tests/automatic/analysis/test_figadapter.py` is a runnable example that
calls the library and maps its real output. The keys differ by function:

| Analysis result | Keys | Figure |
| --- | --- | --- |
| `proportion(k, n)` | `rate`, `wilson.low`, `wilson.high`, `k`, `n`; `available: false` with `rate` and `wilson` set to `None` when `n = 0` | `grouped_bar`: `Point(0, rate, wilson.low, wilson.high, "k/n")` |
| `paired_bootstrap`, `unpaired_bootstrap` | `estimate`, `low`, `high`; `None` and `available: false` without pairs | `forest`: `Point(estimate, row, low, high)` |
| `newcombe_paired` | `difference`, `low`, `high` | `forest`: `Point(difference, row, low, high)` |
| `kaplan_meier` | `steps[]`, one row per event time: `time`, `survival`, `incidence` (= 1 - S), `low`, `high` (on S(t)); no row at t = 0; `available: false` and no steps without episodes | `step_curve`: `Point(0, 0)` without an interval, then `Point(time, incidence, 1 - high, 1 - low)` per step |

* A group with no data (`available: false`) becomes a series with
  `unavailable=True`; it is never dropped. In a forest plot an unavailable
  comparison keeps its row without a point and gets a note.
* Kaplan-Meier intervals are on S(t); the time-to-success figure shows
  1 - S(t), so the interval flips to `[1 - high, 1 - low]`. Between events
  0 < S < 1 always has an interval; once S reaches 0 the interval is
  [0, 0] and the figure shows [1, 1] as a single value. The library has no
  row at t = 0: the adapter adds (0, 0) without an interval (see above).
* Pass a bootstrap interval as it is. If it excludes the point estimate the
  figure warns and keeps it; do not clip or re-centre it.
* Rates are fractions in 0..1 (use `fmt="percent"`); counts go in the point
  `label` (`8/20`) or, for a matrix, in `value`.

## Plan and schedule

**Status: library only** (`levi/automatic/campaign/spec.py`,
`schedule.py`). No command, API or page calls it yet; the commands come with
T-CP-08.

A campaign job file is an ordinary `levi.aeri.job.v1` file (the shared
settings: task, termination, reset, recording) plus a `campaign` block. It
is read with the same strict YAML subset as the job file, which has no lists
of mappings, so the arms are a mapping keyed by arm id (`A` to `H`, at most
eight):

```yaml
campaign:
  id: c20261010-eggplant        # letters, digits, _ and -; names files and runs
  robot: fr3                    # one campaign per robot at a time
  trials_per_arm: 30
  schedule:
    kind: counterbalanced_segments
    segment_trials: 5
    seed: 7
  layouts:
    source: card_set            # or none (reset policy mode)
    file: layouts.yaml          # relative to the job file
  pairing:                      # checkpoint folder name pattern -> config
    recap_cfg_*: pi05_fr3_all_state_cfg
    pi05_fr3_all_step*: pi05_fr3_all_state
  stop_rules:
    consecutive_faults: 2
    unplanned_interventions_per_arm: 5
  arms:
    A:
      role: reference           # at most one
      policy_forward:
        checkpoint_dir: /abs/path/checkpoints/pi05_fr3_all_step49999
        config: pi05_fr3_all_state
        port: 8000
      checkpoint:
        sha256_status: recorded # verified | recorded | none
        manifest_sha256: <64 hex>
    B:
      policy_forward:
        checkpoint_dir: /abs/path/checkpoints/recap_cfg_r2_best_step14300_jax
        config: pi05_fr3_all_state_cfg
        cfg_scale: 1.0
        port: 8000
```

Other optional keys: `primary` (`comparison: [B, A]`, `alpha`,
`label_basis`, `preregistered`; `sequential: step` is reserved and refused),
`control`, `blinding` (`operator: arm_codes` shows X1, X2... instead of arm
letters), `treatment_includes_reset`, and per arm `policy_reset`,
`versions` and `group` (the rollout group; default: the checkpoint folder
name).

The design draft (X3 §1.1) writes the arms as a list (`- id: A`); a job
file writes them as the mapping above, and the cards likewise. A list is
refused with a message that says so.

**Refused plans** (each with its code):

| Code | When |
| --- | --- |
| `E_CAMPAIGN_SCHEMA` | unknown key, wrong type, fewer than two arms, an arm id outside A–H |
| `E_CAMPAIGN_JOB` | the shared settings are not a valid job file |
| `E_CAMPAIGN_ARMS` | two reference arms; two arms in one rollout group; `primary.comparison` not two arms of the campaign; a per-arm `policy_reset` without `treatment_includes_reset: true` (or with `human_assisted`) |
| `E_CAMPAIGN_PAIRING` | no `pairing` table; a checkpoint folder that matches no pattern, or patterns of different configs; a config other than the one its pattern names (a CFG checkpoint served without its CFG config samples without CFG and does not say so) |
| `E_CAMPAIGN_CHECKPOINT` | `sha256_status` `verified` or `recorded` without `manifest_sha256`, or `none` with one |
| `E_CAMPAIGN_SCHEDULE` | `randomized_blocks` with segments longer than one trial; `sequential: step` |
| `E_CAMPAIGN_LAYOUTS` | cards with a reset policy (it sets the scene; write `source: none`), no cards with `human_assisted`, fewer cards than a round needs, a bad card file, `per_round` other than the segment size |
| `E_CAMPAIGN_SETTINGS_DIFFER` | two child jobs differ in a shared setting (below) |
| `E_CAMPAIGN_EXISTS` | a file of the campaign already exists with other content: plan a changed campaign under a new id |

The pairing table lives in the configuration, not in the code: it maps
checkpoint folder names (shell patterns) to configs, so a new policy family
needs a new line, not a release.

**Layout cards** (`layouts.yaml`, mapping keyed by card id):

```yaml
schema_version: levi.aeri.layouts.v1
cards:
  c01:
    description: plate left, cup right
    reference_image: refs/c01.jpg   # shown as an overlay; never opened by the backend
    predicates:
      object_at_source: true
    params:
      x_cm: 10
```

**Expansion.** `spec.plan_campaign(job, job_root=...)` writes one ordinary
job file per segment, `<job_root>/campaigns/<id>/<id>__<arm>__s<NN>.yaml`,
and then `campaign.plan.json`. Each child is the shared settings with its
run id, its trial count, its arm's recording group, the shared forward
(and reset) folder and absolute paths. Files are written once (temporary
file, fsync, `link`, directory fsync): planning the same file again is a
no-op, a changed file under the same id is refused. Each child is then
planned by the launch core's planner (until `launch.plan` exists:
`levi.automatic.cli.load_job`) for its `plan_sha256`.

- `settings_sha256` is the sha256 of a child job with the keys that may
  differ between arms left out: `experiment.name` (the run id),
  `experiment.episodes` (derived from the schedule), `policies` and
  `recording.group`. The Initial State Contract file is named by the
  sha256 of its bytes, read for each child. Every child must give the same
  digest; otherwise `E_CAMPAIGN_SETTINGS_DIFFER` names the first differing
  key.
- `campaign_sha256` covers the normalised campaign block, the cards and the
  card file's sha256, the whole schedule, each child's file sha256 and
  `plan_sha256`, and `settings_sha256`. `spec.read_plan` checks a plan
  against it; `spec.verify_children` checks that no child file (or, with a
  planner, no file it reads) changed since.

**Schedules** (`schedule.build`). A round lays out a set of slots (cards,
or `slot01`, `slot02`... without cards) and every arm runs them in one
segment, in the same order:

| `kind` | Arm order per round | Default segment | Conclusion level |
| --- | --- | --- | --- |
| `counterbalanced_segments` (default) | a row of a Williams design: over a full cycle of rows each arm directly follows each other arm equally often (two arms: AB, BA) | 5 | confirmatory eligible |
| `randomized_blocks` | seeded random order; a block is one card | 1 (fixed) | confirmatory eligible |
| `latin_square` | a row of a cyclic Latin square: each arm in each position once per cycle | 5 | confirmatory eligible |
| `interleaved` | always A, B, ... | 1 | exploratory only |
| `blocked` | all segments of A, then all of B, ... | 5 | exploratory only |

"Confirmatory eligible" is necessary, not sufficient: the report generator
adds the other conditions (preregistration, drift checks). It also needs
whole cycles: the schedule's `balanced_cycles` is true only when the number
of rounds is a multiple of the design's rows (a Williams design has k rows
for an even number of arms k, 2k for an odd one; a Latin square k);
otherwise positions and carryover are not balanced and the level is
`exploratory`. The balance holds within rounds: the last arm of one round
and the first arm of the next are not balanced (two arms may run back to
back across rounds, which keeps the policy). Cards are dealt
from a seeded deck, distinct within a round and used about equally often.
Consecutive segments of the same arm keep the running policy, so the
schedule's `switches` counts real policy starts. Every random choice is a
sort by `sha256(seed, purpose, item)`: the same seed gives the same schedule
in any process and on any machine (tested with different hash seeds).

## State machine and recovery

**Status: library only** (`levi/automatic/campaign/journal.py`,
`conductor.py`, `switch.py`). The launch core (`launch.py`), the command
channel and the session writer plug in through the interfaces below.

```
DRAFT -> PLANNED -> { SEGMENT_PREPARE -> POLICY_STOP -> POLICY_START -> POLICY_READY
      -> ENV_CONFIRM (a person) -> ARM_RUNNING (child run) -> SEGMENT_SEALED } x segments
      -> ANALYZING -> REPORTED
aside: PAUSED (segment boundary only), WAIT_HUMAN, FAULT_LOCKED, ABORTED (operator only)
```

The campaign's files are in `$LEVI_AERI_HOME/campaigns/<id>/`
(`LEVI_AERI_HOME` defaults to `~/.levi-aeri`): `journal.jsonl`, `plan.json`
(the plan, checked against the header's `campaign_sha256` on every open),
`state.json` (derived, never read back) and `torn/`.

**The journal.** Each line is a `levi.aeri.campaign_event.v1` message with
the run journal's transaction protocol (prepared, synced before any side
effect -> acknowledged -> committed or aborted), hash-chained, one writer
(`flock` on the folder). The contract sits in its own registry
(`aeri.CAMPAIGN_SCHEMAS`, minor 0) so the run header and the five messages
are unchanged; its snapshot is
`docs/architecture/aeri/v1/campaign_event.schema.json`, and
`aeri.check_campaign_against_base` compares it with the base branch
(`levi dev check-contracts` does not call it yet). The idempotency key of a
transaction is `<id>:s<NN>:<state entered>` (`<id>:<state>` outside a
segment). The replay refuses a child launch without an operator's command, a move
the table does not allow, a leave of
`WAIT_HUMAN`/`FAULT_LOCKED`/`PAUSED` or an `ABORTED` without an operator's
command, a recovery that does anything but wait, `ANALYZING` before every
segment is sealed, and a pause anywhere but at a boundary.

| Step | What it does | After a crash |
| --- | --- | --- |
| `SEGMENT_PREPARE` | waits until the robot is free (no run holds it, no other live session) | waits for a person |
| `POLICY_STOP` | writes `waiting_reset` to the C2 session, stops every policy unit of the campaign, waits until nobody listens on the policy port; skipped when the same arm keeps serving | `FAULT_LOCKED` (the stop may or may not have happened) |
| `POLICY_START` | `systemd-run --user --unit=levi-policy-<id>-<arm>` from the launch recipe | `FAULT_LOCKED` |
| `POLICY_READY` | passive check only (below); not ready within 600 s: `WAIT_HUMAN` | waits for a person |
| `ENV_CONFIRM` | asks the operator: arm still, cards laid out | asks again (new question) |
| `ARM_RUNNING` | checks the child file and its plan again, then launches the child run (the one non-idempotent action, only under an operator's command, with the expected `plan_sha256` for the launcher to check), then watches it | `FAULT_LOCKED` if the launch was dangling, otherwise `WAIT_HUMAN`; never launched again by itself |
| `SEGMENT_SEALED` | records the child's counts (fewer complete episodes than the segment holds: `WAIT_HUMAN`, `segment_short`, until a person resumes with `accept_short_segment: true`); applies the stop rules and a pending pause | waits for a person |

**A person's answers** (`Confirmations`): each question carries a random
nonce, and an answer counts only for that question with that nonce and an
unused command id (others are dropped and noted). A scene confirmation
needs `arm_still` and `layout_ready`. From a wait, `resume` goes to the
next safe place: the next segment to prepare; the child run to watch when
it was started (or may have been); `ANALYZING` when every segment is
sealed. `relaunch` prepares the segment again only when the launch was
never acknowledged and the run is not there; a run acknowledged as started
is never started again. `abort` ends the campaign. A wait caused by a stop
rule needs `override_stop_rule: true`.

**Stop rules** (`campaign.stop_rules`): `consecutive_faults` child faults or
crashes with no segment sealed in between (a segment recovered and sealed
resets the count; the same child faulting again after a resume counts
again), or an arm whose unplanned
interventions pass `unplanned_interventions_per_arm`, make the campaign
wait for a person. It never skips an arm. A child run's own planned wait
(for example a person resetting the scene) is not a fault.

**One campaign per robot.** The conductor holds
`$LEVI_AERI_HOME/campaign-<robot>.lock` (`flock`) while it lives; a second
campaign on the same robot is refused with `E_BUSY` and the holder.

**Policy switch** (`switch.SystemdPolicyHost`). Readiness is read, never
asked: the unit is active and every process listening on the arm's port
belongs to the unit's cgroup and runs the arm's config (as a whole word: the
plain config is a prefix of the CFG one) and checkpoint, read from
`/proc/net/tcp{,6}`, `/proc/<pid>/fd`, `cgroup` and `cmdline`. The backend
never connects to the policy port; the real handshake is the child run's
PREFLIGHT. A listener that is not the campaign's (a policy server started
by hand), whose owner cannot be read, or that runs another config locks the
campaign (`FAULT_LOCKED`). Without a launch recipe nothing is started.

**Recovery.** `Conductor.attach(id, job_dir)` takes the robot lock, opens
the journal (a corrupt one is refused, `E_CORRUPT`, and nothing is
written), checks the child files (the conductor checks the segment's file
and plan once more right before each launch: a change is `FAULT_LOCKED`,
`plan_changed`), aborts a dangling transaction and moves
to `FAULT_LOCKED` (it had a side effect) or `WAIT_HUMAN`. A campaign that
was waiting for a person, asking one (`ENV_CONFIRM`) or analysing stays
there. Nothing moves until a person answers. The tests kill a conductor
just before and just after every journal line of a whole campaign and check
that each recovery stops at a person and that every child run is started
exactly once.

**Interfaces for the integration:** `PolicyHost` (`stop`, `start`,
`passive_ready`), `RunLauncher` (`launch(job_path, run_id,
expected_plan_sha256)`, `status`, `robot_busy`),
`Confirmations` (`ask`, `take`, `withdraw`, `pause_requested`),
`SessionWriter` (`waiting_reset`, `release`) and `ChildPlanner` (`plan`).

**Not yet:** commands and pages (T-CP-08, T-CP-09), the trial ledger and
reruns `<id>__<arm>__s<NN>r2` (T-CP-05), the real launch recipes (T-SU),
the report (T-CP-06).

## Trial ledger

`levi.automatic.campaign.ledger` lists every forward episode a campaign ran,
one row per episode, in `ledger.jsonl` (schema
`levi.aeri.campaign_trial.v1`). The campaign stores no truth of its own:
the ledger is derived again from the child runs each time, and the same
inputs give the same bytes (`write_ledger` writes nothing when the file
already holds them, and otherwise writes through a temporary file, `fsync`
and rename).

**Inputs.** A `CampaignLayout` (campaign id, segments in campaign order,
each with its arm, round, layout cards in slot order and the run ids that
ran it: the first run, then reruns) and the episodes of each run:

| Source | Function | Reads |
| --- | --- | --- |
| AERI child run | `read_aeri_run(run_dir, max_steps=None)` | `manifest.json`, the run journal (each episode's committed result) and `labels/`, without a lock and without writing; a corrupt journal is refused |
| Legacy evaluation client | `guided.legacy_fact(...)` (see [Guided legacy-client campaign](#guided-legacy-client-campaign)) | the rollout's `metadata.json` |

**Rows.** `trial_id = <campaign>:<round>:<card>:<arm>`, `slot` (the card's
position in its segment), `segment`, `run_id`, `episode_id`, `order_index`,
`started_at`/`ended_at`, `layout_fidelity` (`attested`, `verified`,
`deviated` with its reason), `preceded_by_arm` (the arm of the segment
before), `rerun_of` (the segment's first run, for rows of a rerun), plus
steps, stop reason, how the episode ended (`budget`, `early_stop`,
`operator_stop`) and the early-stop control fields.

**Which card an episode had.**

| Case | Card | Paired? |
| --- | --- | --- |
| The run manifest names it (`episodes[*].campaign.card`) or a person confirmed it | as given | yes |
| An AERI episode without one | the segment's next unfinished card (the run asked for it) | yes |
| A legacy-client episode not yet confirmed | none; `candidate_card` is the next unfinished card | **no** |
| A card outside the segment, a card already held, an episode beyond the last card | flagged in `card_problem` | no |

Discarded (the operator's `d`, or a `discarded` label) and incomplete
episodes stay in the ledger and are counted, hold no card (the operator
places the same card again) and have no outcome. `counts(ledger, layout)`
gives per arm: valid, discarded, incomplete, deviated, rows from reruns,
unconfirmed, card problems, pairable, planned and missing slots.

**Label bases.** The four kinds of label stay apart per episode
(autonomous verdict, post-hoc verdict, operator label, adjudicated label).
`label_value(entry, basis)` reads one of five bases: `autonomous_verdict`,
`posthoc_verdict`, `operator_label`, `adjudicated_ground_truth`,
`adjudicated_then_operator`. An undecided or missing verdict and a
`discarded` or `unclear` operator label give no value. The last two bases
are what `metrics.LabelStore.truth` reads; its default is unchanged.

## Report artefacts

`levi.automatic.campaign.report` turns the ledger and one label basis into
a report. `analyse(ledger, info, basis, layout=None, seed=None)` returns
every number in one JSON document (schema `levi.aeri.campaign_report.v1`);
`write_report(report_root, ledger, info, basis, campaign_state=..., ...)`
writes it out once the campaign has reached `ANALYZING` or `REPORTED`:

```
<report_root>/<basis>/
  summary.en.md  summary.zh-CN.md
  tables/   success  pairwise  continuous  failure_modes  agreement  power (.csv and .tex); drift.csv
  figures/  f1-success  f2-differences  f3-time-to-success  f4-failure-modes
            f5-early-stop  f6-drift  f7-agreement (.svg, .zh-CN.svg, .pdf, .json FigureSpec)
  data/     trials.parquet  trials.csv  labels.csv  analysis.json
  manifest.json
```

Each basis has its own folder, and writing one never changes another. The
folder is built beside the old one under a per-basis lock
(`.<basis>.lock`) and swapped in whole: a reader sees the old report or the
new one, and a failed write leaves the old one as it was. A writer killed
halfway leaves a `.<basis>.tmp-*` folder that the next write of that basis
removes; one killed between moving the old folder aside and moving the new
one in leaves no `<basis>` folder for that moment, and the next write puts
the old one back before it starts. The same ledger, labels, campaign information and seed give the
same bytes (the manifest's `generated_at` aside; pass `now` to fix it).
`.tex` tables use `tabular` and `\hline` only; `fmt` formats every printed
number. A CSV text cell that a spreadsheet would run as a formula starts
with an apostrophe.

**What is analysed.** Per arm: the success rate with Wilson and
Clopper–Pearson intervals, and label coverage. Per pair of arms (the
pre-registered comparison first, B minus A): McNemar's tests, Newcombe's
paired interval and a paired bootstrap on trials with the same card in
the same round; Fisher's test and Newcombe's independent interval when a
reset policy sets the scene (`layout_source: none`). Holm adjusts the
family; three or more arms add Cochran's Q. Steps on pairs that both
succeeded (Wilcoxon, Hodges–Lehmann); time to success (Kaplan–Meier,
log-rank, RMST up to the step cap); failure modes; early termination (the
detector is judged against people: adjudicated, else operator); drift
(reference arm trend, arm by time, carryover); the automatic verdict
against the operator per arm and per way the episode ended, with a test of
a judge error rate that differs between arms; the power table. A
sensitivity analysis without deviated trials is added when there are any.

**Blind operator labels (CL14).** The judge agreement, the confusion
matrices (F7) and the test of a judge error rate that differs between arms
use each episode's first operator label, the one written before the
automatic verdict was revealed (`operator_blind` in the ledger): an
operator who changes a label after seeing the verdict must not raise the
agreement. Success rates keep the current label (corrections of slips
count). When labels were changed after the reveal, the report counts them
per arm, repeats the success rates and the primary comparison with the
first labels only (a sensitivity analysis), lowers a declared `full`
operator blinding to `partial`, and makes the conclusion exploratory if
the first labels lead to a different conclusion. The legacy client writes
`eval.operator_outcome` once, at the key press, so its label is blind.

**Label basis and names.** The summary opens with the basis block:
basis, label coverage per arm (a warning above 10 percentage points
between arms), operator blinding, layout control and deviated trials,
reset mode and scene check. Rates are named by basis and the generator
refuses anything else (`check_naming`):

| Basis | Name of the rate |
| --- | --- |
| `autonomous_verdict`, `posthoc_verdict` | automatic-verdict success rate (unreviewed) |
| `operator_label` | success rate (operator label) |
| `adjudicated_ground_truth` | ground-truth success rate (the only basis that may say ground truth) |
| `adjudicated_then_operator` | success rate (adjudicated where available, else operator), with how many labels came from each |

**Conclusion level.** Confirmatory only when every condition holds:
pre-registered primary analysis; the planned n reached by what the primary
analysis used (pairs in a paired design, labelled trials per arm
otherwise); no peeks; a basis a person reviewed (operator or adjudicated);
no drift warning, and drift checks that actually ran (a reference arm and
enough rounds: a check that could not run is no pass); a schedule other
than `blocked` or `interleaved`, both as planned and as the ledger shows
it (`observed_schedule`: fewer than two rounds or each arm's segments one
after another is blocked, the same arm order in every round is
interleaved); the same step budget in every arm; label coverage within 10
percentage points between arms; the same conclusion with the blind
operator labels; and the library's planned-power rule (the planned
difference detectable at 80 %). Only the pre-registered (primary)
comparison can be confirmatory, and only when Holm rejects it; the other
comparisons are always exploratory. Otherwise every conclusion sentence is
marked exploratory, and the summary lists the conditions not met. An interval that holds zero says the data
cannot tell the arms apart and gives the design's detectable difference;
it never says the arms are alike. Words that claim more than an interval
("significantly outperforms", "proves", "state-of-the-art" and their
Chinese counterparts) are refused outside a confirmatory sentence
(`check_wording`; a test scans every template branch). The text comes from
`templates/` (`sentences.json`, `summary.<lang>.md`); no language model is
called, and every number in it is formatted from `analysis.json` (tested).
Post-hoc power is never reported. Tables of differences and times carry a
`basis` column, and the time-to-success figure names its basis.

**Running campaigns.** `campaign_state` is required. Before `ANALYZING`, or
with `blinded=True`, the folder holds progress only: `progress.json`
(schema `levi.aeri.campaign_progress.v1`: valid, discarded, incomplete,
deviated, rerun and missing counts, label coverage per arm), a progress
summary in both languages and the manifest. No analysis.json, table,
figure, rate, comparison, drift result or conclusion level exists until the
campaign reaches analysis, so nothing can be read early through a file
route or a download.

**Privacy.** Every free text is scrubbed before the analysis, so
analysis.json, tables, figures and summaries come from the same clean
values: the task, each arm's configuration, versions and unverified items,
post-hoc failure modes and layout reasons lose paths, e-mail addresses, IP
and MAC addresses, URLs and serial-like numbers (9 to 14 digits). Digests
that are not 64 hex digits are dropped. `manifest.json` lists the campaign and settings digests,
each child run's plan digest, state, LEVI commit and modes, each arm's
checkpoint name (never its path), configuration, digest status and
versions, the seed and schedule, peeks, deviated trials, the methods with
their references, every file's size and SHA-256, PDF text substitutions,
and `png: skipped(no converter)`. Names and e-mail addresses are never
written. Of the site facts (`site`, and the campaign's `extra`) only a
fixed list is written (`SITE_FIELDS`: GPU, driver, kernel, openpi version,
vLLM model, settings digests, judge spec, initial-state contract, feature
switches, Python and numpy), each scrubbed; everything, people's names and
addresses excepted, only with `include_site_details=True`.

## Guided legacy-client campaign

Until a real AERI robot adapter exists, a campaign can run on the legacy
evaluation client: for each segment a person copies a command, runs it, and
the campaign collects what the client wrote. `levi.automatic.campaign.guided`
is the data side of this (no command, page or API calls it yet).

**Commands.** `base_command(guide_text)` takes the dual-label evaluation
command from the operator guide (`setup.md` §6.3, first code block), read
with the parser of the setup recipes. It must carry `--levi-mode dual` and
each of `--eval-num`, `--rollout-group` and `--eval-note` once.
`render(base, eval_num=, rollout_group=, eval_note=)` replaces only those
values and checks that every other character is the guide's own. The task
instruction is no per-segment parameter: it is a shared setting of the
campaign (`task.prompt`, part of `settings_sha256`), given once as
`base_command(guide_text, prompt=...)`, saved with the command
(`to_dict()`) and used by every segment; without it the guide's own
`--prompt` stays. `segment_commands(base, layout, groups)`
gives one command per segment: `--eval-num` its card count,
`--rollout-group` the arm's checkpoint full name, `--eval-note
"<campaign id> s<NN> <code>"` with the arm's code (`X1`, `X2`, ...; the
arm's own name never appears in the note). Values that would end the
quotes, expand or escape (`"`, `\`, `$`, `` ` ``, `!`, control
characters) are refused.

**Drift.** The guide holds machine-specific values (camera serials, the
home pose), so the repository keeps no copy of it. A campaign saves the
command it was planned with (`BaseCommand.to_dict()`, with its SHA-256);
`check_drift(saved, guide_text)` returns a unified diff when the guide's
command changed (a section or block that is gone counts as drift), and
`render_checked` refuses with that diff (`GuideDrift`): every segment of a
campaign runs the same command. A test renders the maintainer's own guide
when it is found above the checkout and fails with the difference when it
no longer fits.

**Collecting.** `scan_rollouts(root, group, task_folder)` lists a task
folder's rollouts, read only (`metadata.json` and whether `.complete`
exists). `collect_segment(records, layout, segment, confirmations=,
posthoc=)` keeps the dual-label rollouts whose `eval.eval_note` names this
campaign and segment, groups them by the client's `run_id` (first run
first, then reruns, by start time) and returns `runs`, the episodes,
`pending` (valid episodes without a confirmed card, each with its candidate
card), `ignored` (this segment's rollouts that are not dual-label runs) and
the segment's ledger rows and counts. A segment whose rollouts name two arm
codes is refused. `bind_runs(layout, collected)` puts the run ids into the
layout for the ledger. Per episode (`legacy_fact`): the operator label
(`eval.operator_outcome`), the online judgement as the autonomous verdict
(`eval.agent_label`: success, failure, undecided, or none when it timed
out or failed), the background review's verdict when the caller passes it,
how the episode ended (`eval.ended_by`: `budget` or the operator's key),
steps and the step cap. Discarded and aborted rollouts, and a rollout
without `.complete`, are kept and counted, never paired.

**Cards need a person.** Matching episodes to cards by their order is
error-prone, so an episode holds a card only once a person confirmed it:
`CardConfirmations(path).add(episode_key, card, by=<opaque id>)` appends a
line (`levi.aeri.campaign_card.v1`, synced, under a lock); the last line
per episode wins, `card=None` withdraws one, and the earlier lines stay on
file. An unconfirmed episode never enters a paired analysis.

**Agreement.** `segment_agreement(facts)` compares the automatic label with
the operator's over all valid episodes and apart for those that ran the
full step budget (`budget`) and those the operator's key ended
(`operator_stop`). Only the full-budget agreement carries over to
unattended runs; the report keeps the strata apart as well.
