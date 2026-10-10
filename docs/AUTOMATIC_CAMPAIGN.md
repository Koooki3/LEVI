# Multi-arm evaluation campaigns (AERI)

[中文](AUTOMATIC_CAMPAIGN.zh-CN.md)

**Status: library only.** This page will describe how the automatic
evaluation pipeline (AERI) compares several policies (arms) on one task.
Only the statistical methods exist so far: `levi/automatic/analysis/`, pure
functions that no command, page or API calls yet. The campaign plan,
schedule, report and page are later work and get their own sections here.

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
