"""Statistical analysis of multi-arm AERI evaluations (campaigns).

Pure functions over trial outcomes: no file is read, no LEVI runtime module
is imported (except ``levi.live.stats.wilson``, reused so the live page and
the reports print the same Wilson interval), nothing is cached, and every
random draw takes an explicit ``seed``. Every public function returns a
JSON-ready dict with ``schema_version``, ``method``, ``implementation``,
``references`` (keys of ``levi2/references/X3-evaluation-methods.json``,
all VERIFIED), ``exploratory`` and ``caveats``. The numbers only; wording
belongs to the report generator.

See docs/AUTOMATIC_CAMPAIGN.md ("Statistical methods").
"""

from ._core import SCHEMA_VERSION, AnalysisInputError
from .agreement import agreement, cohen_kappa, misjudgement_by_arm, rogan_gladen
from .continuous import (
    cliffs_delta,
    hodges_lehmann,
    improvement_share,
    mann_whitney,
    wilcoxon_signed_rank,
)
from .drift import (
    arm_time_interaction,
    carryover,
    drift_warning,
    mann_kendall,
    reference_drift,
)
from .failures import early_stop, failure_modes
from .multiple import benjamini_hochberg, bonferroni, cochran_q, friedman, holm
from .paired import mcnemar, newcombe_paired, paired_bootstrap, unpaired_bootstrap
from .power import min_detectable_difference, power_paired, power_table, power_unpaired
from .proportions import (
    agresti_caffo,
    boschloo_exact,
    fisher_exact,
    newcombe_independent,
    posterior_prob_greater,
    proportion,
)
from .smoothness import smoothness
from .survival import kaplan_meier, logrank, rmst

__all__ = [
    "SCHEMA_VERSION",
    "AnalysisInputError",
    "agreement",
    "agresti_caffo",
    "arm_time_interaction",
    "benjamini_hochberg",
    "bonferroni",
    "boschloo_exact",
    "carryover",
    "cliffs_delta",
    "cochran_q",
    "cohen_kappa",
    "drift_warning",
    "early_stop",
    "failure_modes",
    "fisher_exact",
    "friedman",
    "hodges_lehmann",
    "holm",
    "improvement_share",
    "kaplan_meier",
    "logrank",
    "mann_kendall",
    "mann_whitney",
    "mcnemar",
    "min_detectable_difference",
    "misjudgement_by_arm",
    "newcombe_independent",
    "newcombe_paired",
    "paired_bootstrap",
    "posterior_prob_greater",
    "power_paired",
    "power_table",
    "power_unpaired",
    "proportion",
    "reference_drift",
    "rmst",
    "rogan_gladen",
    "smoothness",
    "unpaired_bootstrap",
    "wilcoxon_signed_rank",
]
