"""Zone 6 cost-per-retrieval-query reporting (AC-006-COST-1, HLD 12D/12E, ADR-007/008).

Implements -- verbatim, not re-derived -- the formula set the mathematics-
engineer persona produced under blocking delegation AR1-007
(`ar1_assignments.json` -> `assignments[DASH-STORY-007].mathematical_delegation`)
for AC-006-COST-1:

    "AC-006-COST-1 requires a reported average cost per retrieval query
     (C_fixed_vector + C_fixed_lexical + c_var*V)/V AND a separately
     reported marginal cost c_var."

AC-006-COST-1 explicitly forbids presenting average cost alone as an
efficiency indicator, because `dAC/dV = -F/V^2 < 0` strictly whenever
`F > 0` -- a falling average cost as volume grows is mathematically
guaranteed and carries zero efficiency information on its own. This module
therefore never exposes `average_cost_per_query` without `amortization_share`
and `distortion_factor` alongside it (the delegation's own reporting
contract, its derivation Step 12).

Every quantity here is per one billing period `T` (the dimensional contract
the delegation opens with): `F` and `V` MUST be evaluated over the same `T`,
or `F / V` silently inflates by whatever ratio the two periods differ by.
This module does not itself choose `T` -- the caller's `Zone6CostInputs`
values must already share one period.

Numerical-stability guards (delegation Step 11, mandatory, not optional):
  * `V = 0` is a genuine pole of `AC(V) = F/V + c_var`, not a bug to hide.
    `per_tenant_average_cost` returns the literal descriptive string
    `"undefined (V_t = 0); fixed cost <F_t> still incurred"` for this case
    -- never `0.0`, never `float("inf")`, never a silent skip.
  * All arithmetic here runs in Python's native `float` (IEEE 754 binary64
    / float64), which the delegation names as the required precision --
    float32 is explicitly flagged as marginal once `F/V` and `c_var` differ
    by several orders of magnitude, as they do at Sprint 1 volumes.
  * Per-query costs land near 1e-5 currency units; this module's
    `*_per_million` fields exist so a 2-decimal print never silently
    rounds the whole figure to zero.
"""

from __future__ import annotations

from dataclasses import dataclass

QUERIES_PER_MILLION = 1_000_000
"""Reporting denominator (delegation Step 11(iii)): per-query costs are
~1e-5 currency units and round to $0.00 at 2 decimals; reporting per 1M
queries keeps the figure legible without requiring >=6 significant figures
on every printed value."""


@dataclass(frozen=True, slots=True)
class CVarBreakdown:
    """`c_var`'s component terms (derivation eq. 5, HLD 12E / ADR-015 grounded).

    `c_var = c_emb * (1 - h_host) * (1 - h_L1) + c_vec_q + c_lex_q + c_egress`.
    Under node-hour / self-hosted provisioning (ADR-007 Qdrant, ADR-008
    OpenSearch) `c_vec_q ~= c_lex_q ~= 0`, so `c_var` collapses to the
    embedding term alone -- see `Zone6CostReport.c_var`'s docstring for why
    a near-zero `c_var` is a correct structural finding, not a defect.
    """

    embedding_term: float
    """`c_emb * (1 - h_host) * (1 - h_L1)`."""
    vector_query_cost: float
    """`c_vec_q`. ~0 under Qdrant node-hour/self-hosted billing."""
    lexical_query_cost: float
    """`c_lex_q`. ~0 under OpenSearch node-hour/self-hosted billing."""
    egress_cost: float
    """`c_egress`. ~0 for co-located deployments."""

    @property
    def total(self) -> float:
        """`c_var` itself -- the sum of all four components (eq. 5)."""
        return (
            self.embedding_term
            + self.vector_query_cost
            + self.lexical_query_cost
            + self.egress_cost
        )


@dataclass(frozen=True, slots=True)
class Zone6CostInputs:
    """One billing period `T`'s worth of Zone 6 cost inputs (delegation Step 12 shape).

    Every field not independently measured should be flagged `[ASSUMED]` by
    the caller in whatever report wraps this input, per HLD 12D's own
    `[ASSUMED]`-per-OAQ-13 convention -- this module does not track
    provenance of its own inputs, only computes from them.

    Attributes:
        period_hours: `T`, in hours. The delegation recommends 730 h (one
            month) and requires `F` and `V` to share this same period.
        total_assemble_requests: `Q`, total `/v1/context/assemble` requests
            in `T`.
        l2_cache_hit_rate: `h_L2` in `[0, 1]`, the assembled-context cache
            hit rate (HLD 12E, Redis, TTL 5s +/-10%).
        fixed_cost_vector: `C_fixed_vector`, provisioned Qdrant cost for `T`.
        fixed_cost_lexical: `C_fixed_lexical`, provisioned OpenSearch cost
            for `T`.
        embedding_cost_per_call: `c_emb`, currency per query-embedding call
            (ADR-015 adapter (b), HTTP API).
        host_supplied_embedding_rate: `h_host` in `[0, 1]`, fraction of
            requests carrying the optional `query_embedding` field.
        l1_cache_hit_rate: `h_L1` in `[0, 1]`, the in-process query-
            embedding LRU hit rate (HLD 12E assumes 0.40).
        vector_query_cost: `c_vec_q`, per-request Qdrant charge. `0.0` under
            node-hour/self-hosted billing (the ADR-007 default).
        lexical_query_cost: `c_lex_q`, per-request OpenSearch charge. `0.0`
            under node-hour/self-hosted billing (the ADR-008 default).
        egress_cost: `c_egress`, attributable cross-AZ/cross-vendor egress
            per query. `0.0` for co-located deployments.
    """

    period_hours: float
    total_assemble_requests: float
    l2_cache_hit_rate: float
    fixed_cost_vector: float
    fixed_cost_lexical: float
    embedding_cost_per_call: float
    host_supplied_embedding_rate: float
    l1_cache_hit_rate: float
    vector_query_cost: float = 0.0
    lexical_query_cost: float = 0.0
    egress_cost: float = 0.0

    def __post_init__(self) -> None:
        """Enforce the dimensional/domain preconditions the derivation states.

        Raises:
            ValueError: If `period_hours` or `total_assemble_requests` is
                not positive, or any rate (`l2_cache_hit_rate`,
                `host_supplied_embedding_rate`, `l1_cache_hit_rate`) is
                outside `[0, 1]`.
        """
        if self.period_hours <= 0:
            raise ValueError(
                f"Zone6CostInputs.period_hours must be positive, got {self.period_hours}"
            )
        if self.total_assemble_requests < 0:
            raise ValueError(
                "Zone6CostInputs.total_assemble_requests must be non-negative, "
                f"got {self.total_assemble_requests}"
            )
        for name, value in (
            ("l2_cache_hit_rate", self.l2_cache_hit_rate),
            ("host_supplied_embedding_rate", self.host_supplied_embedding_rate),
            ("l1_cache_hit_rate", self.l1_cache_hit_rate),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"Zone6CostInputs.{name} must be in [0, 1], got {value}")

    @property
    def zone6_queries(self) -> float:
        """`V = Q * (1 - h_L2)` (derivation eq. 4): the Zone 6 unit-cost denominator.

        An L2 assembled-context cache hit returns without touching Qdrant
        or OpenSearch, so it generates no Zone 6 cost and must not appear
        in the denominator of a Zone 6 unit-cost figure.
        """
        return self.total_assemble_requests * (1.0 - self.l2_cache_hit_rate)

    @property
    def fixed_cost(self) -> float:
        """`F = C_fixed_vector + C_fixed_lexical`."""
        return self.fixed_cost_vector + self.fixed_cost_lexical

    @property
    def c_var_breakdown(self) -> CVarBreakdown:
        """`c_var`'s four components (eq. 5), individually and summed via `.total`."""
        embedding_term = (
            self.embedding_cost_per_call
            * (1.0 - self.host_supplied_embedding_rate)
            * (1.0 - self.l1_cache_hit_rate)
        )
        return CVarBreakdown(
            embedding_term=embedding_term,
            vector_query_cost=self.vector_query_cost,
            lexical_query_cost=self.lexical_query_cost,
            egress_cost=self.egress_cost,
        )


@dataclass(frozen=True, slots=True)
class AttributionEffects:
    """The exact three-way decomposition of `AC_2 - AC_1` (derivation eq. 15).

    An algebraic identity, not an approximation: `volume_effect +
    capacity_effect + efficiency_effect == AC_2 - AC_1` exactly, for all
    admissible inputs. Reporting this alongside a period-over-period AC
    change is how AC-006-COST-1 permits reporting AC at all: the reader can
    no longer mistake a volume effect (guaranteed by `dAC/dV < 0`, zero
    efficiency content) for a genuine efficiency gain, because the two are
    in separate fields.
    """

    volume_effect: float
    """`F_1 * (1/V_2 - 1/V_1)`. Pure amortization; zero efficiency content."""
    capacity_effect: float
    """`(F_2 - F_1) / V_2`. A provisioning decision, not an efficiency signal."""
    efficiency_effect: float
    """`c_var_2 - c_var_1`. The ONLY genuine per-query efficiency signal."""

    @property
    def total_delta_ac(self) -> float:
        """The three effects summed -- must equal `AC_2 - AC_1` exactly."""
        return self.volume_effect + self.capacity_effect + self.efficiency_effect


@dataclass(frozen=True, slots=True)
class Zone6CostReport:
    """AC-006-COST-1's required output shape (delegation Step 12 reporting contract).

    `average_cost_per_query` is NEVER emitted without `amortization_share`
    and `distortion_factor` beside it (delegation Step 12: "AC is never
    emitted without phi(V) and AC/MC beside it") -- this dataclass enforces
    that by construction: there is no way to build one without the other.
    """

    period_hours: float
    zone6_queries: float
    fixed_cost: float
    c_var: CVarBreakdown
    average_cost_per_query: float
    """`AC(V) = F/V + c_var` (eq. 7). NEVER presented alone -- see the class
    docstring and `amortization_share`/`distortion_factor` below."""
    marginal_cost_per_query: float
    """`MC(V) = c_var` (eq. 8), valid on the current provisioned-capacity
    segment only (delegation Step 5: `MC` jumps by `DeltaC_node` at a
    capacity breakpoint; this field is the segment-interior value)."""
    amortization_share: float
    """`phi(V) = F / (F + c_var*V)` (eq. 11-13): the fraction of
    `average_cost_per_query` that is pure fixed-capacity amortization, not
    per-query efficiency. `1.0` means AC carries zero efficiency
    information; `0.0` means AC has converged onto MC."""
    crossover_volume: float
    """`V_half = F / c_var` (eq. 12): the volume at which `phi = 0.5`."""
    distortion_factor: float
    """`AC(V)/MC(V) = 1 + V_half/V` (eq. 14): the factor by which AC alone
    overstates the true incremental cost of one more retrieval query."""
    attribution: AttributionEffects | None = None
    """The eq. 15 decomposition vs. a prior period, or `None` when no prior
    period was supplied (there is nothing to attribute a change against)."""

    @property
    def average_cost_per_million_queries(self) -> float:
        """`average_cost_per_query * 1_000_000` (delegation Step 11(iii))."""
        return self.average_cost_per_query * QUERIES_PER_MILLION

    @property
    def marginal_cost_per_million_queries(self) -> float:
        """`marginal_cost_per_query * 1_000_000` (delegation Step 11(iii))."""
        return self.marginal_cost_per_query * QUERIES_PER_MILLION


def compute_zone6_cost_report(
    inputs: Zone6CostInputs, prior: Zone6CostInputs | None = None
) -> Zone6CostReport:
    """Compute AC-006-COST-1's required average+marginal cost report (eqs. 6-15).

    Args:
        inputs: This period's cost inputs.
        prior: The immediately preceding period's inputs, if reporting a
            period-over-period change. When supplied, the returned report's
            `attribution` field carries the exact eq. 15 decomposition.

    Returns:
        A `Zone6CostReport` carrying `average_cost_per_query` and
        `marginal_cost_per_query` as two separate fields (never one without
        the other), plus `amortization_share` and `distortion_factor`
        (delegation Step 12's mandatory accompaniments to AC) and, when
        `prior` is given, the `attribution` decomposition.

    Raises:
        ValueError: If `inputs.zone6_queries` is exactly zero -- AC(V) has
            a genuine pole there (delegation Step 11(i)); this function
            requires a positive aggregate volume. A per-tenant report with
            a possibly-zero `V_t` uses `per_tenant_average_cost` instead,
            which returns the descriptive undefined-state string rather
            than raising.
    """
    v_queries = inputs.zone6_queries
    if v_queries <= 0.0:
        raise ValueError(
            "compute_zone6_cost_report requires zone6_queries > 0 "
            f"(got {v_queries}); AC(V) has a pole at V=0 -- see "
            "per_tenant_average_cost for the V_t=0 reporting contract"
        )

    fixed_cost = inputs.fixed_cost
    c_var_breakdown = inputs.c_var_breakdown
    c_var = c_var_breakdown.total

    average_cost = fixed_cost / v_queries + c_var
    marginal_cost = c_var
    amortization_share = fixed_cost / (fixed_cost + c_var * v_queries)
    crossover_volume = fixed_cost / c_var if c_var > 0.0 else float("inf")
    distortion_factor = average_cost / marginal_cost if marginal_cost > 0.0 else float("inf")

    attribution = (
        _attribution_effects(prior, inputs) if prior is not None else None
    )

    return Zone6CostReport(
        period_hours=inputs.period_hours,
        zone6_queries=v_queries,
        fixed_cost=fixed_cost,
        c_var=c_var_breakdown,
        average_cost_per_query=average_cost,
        marginal_cost_per_query=marginal_cost,
        amortization_share=amortization_share,
        crossover_volume=crossover_volume,
        distortion_factor=distortion_factor,
        attribution=attribution,
    )


def _attribution_effects(
    prior: Zone6CostInputs, current: Zone6CostInputs
) -> AttributionEffects:
    """Compute the eq. 15 volume/capacity/efficiency decomposition of `Delta_AC`."""
    f_1, f_2 = prior.fixed_cost, current.fixed_cost
    v_1, v_2 = prior.zone6_queries, current.zone6_queries
    c_var_1, c_var_2 = prior.c_var_breakdown.total, current.c_var_breakdown.total

    if v_1 <= 0.0 or v_2 <= 0.0:
        raise ValueError(
            "attribution requires both periods' zone6_queries to be positive "
            f"(got prior={v_1}, current={v_2})"
        )

    volume_effect = (
        f_1 * (v_1 - v_2) / (v_1 * v_2)
        if v_1 != v_2
        else 0.0
    )
    capacity_effect = (f_2 - f_1) / v_2
    efficiency_effect = c_var_2 - c_var_1
    return AttributionEffects(
        volume_effect=volume_effect,
        capacity_effect=capacity_effect,
        efficiency_effect=efficiency_effect,
    )


def per_tenant_average_cost(
    tenant_fixed_cost: float, c_var: float, tenant_zone6_queries: float
) -> float | str:
    """`AC_t(V_t) = f_t / V_t + c_var` (eq. 18), with the mandatory `V_t = 0` guard.

    Args:
        tenant_fixed_cost: This tenant's attributable fixed-cost share
            (`f_vec_tenant + f_lex_tenant + share_t * f_cluster`, eq. 18).
        c_var: The (tenant-independent) marginal cost per query.
        tenant_zone6_queries: `V_t`, this tenant's Zone 6 query volume for
            the period.

    Returns:
        The float average cost per query, OR -- when `tenant_zone6_queries`
        is exactly zero -- the literal descriptive string
        `"undefined (V_t = 0); fixed cost <tenant_fixed_cost> still
        incurred"` (delegation Step 11(i)): an idle tenant's average cost is
        genuinely undefined while its fixed cost is genuinely still being
        spent, and reporting `0.0` would misreport it as a zero-cost tenant.

    Raises:
        ValueError: If `tenant_zone6_queries` is negative.
    """
    if tenant_zone6_queries < 0.0:
        raise ValueError(
            "per_tenant_average_cost requires tenant_zone6_queries >= 0, "
            f"got {tenant_zone6_queries}"
        )
    if tenant_zone6_queries == 0.0:
        return f"undefined (V_t = 0); fixed cost {tenant_fixed_cost} still incurred"
    return tenant_fixed_cost / tenant_zone6_queries + c_var


def degraded_c_var(
    inputs: Zone6CostInputs, vector_breaker_open_rate: float, lexical_breaker_open_rate: float
) -> float:
    """`c_var_eff` under HLD 8.4 circuit-breaker degradation (derivation eq. 9/19).

    When the Qdrant breaker is open, RRF degrades to lexical-only; when the
    OpenSearch breaker is open, to vector-only (HLD 8.4). `F` does NOT fall
    during degradation -- the failed node stays provisioned and billed --
    so an outage simultaneously RAISES `AC` (same `F`, fewer successful
    queries) and LOWERS `MC` (one surface not queried); this function
    computes only the latter effect on `c_var`, the caller composes it into
    `AC`/`MC` via `compute_zone6_cost_report` with an adjusted `inputs` if
    needed.

    Args:
        inputs: The period's cost inputs (the embedding term is unaffected
            by either breaker state -- embedding happens before fan-out).
        vector_breaker_open_rate: `p_v` in `[0, 1]`, the fraction of queries
            served vector-breaker-open (lexical-only).
        lexical_breaker_open_rate: `p_l` in `[0, 1]`, the fraction of
            queries served lexical-breaker-open (vector-only).

    Returns:
        `c_var_eff` (eq. 19).

    Raises:
        ValueError: If either rate is outside `[0, 1]`, or their sum
            exceeds `1.0` (a query cannot be served in more than one
            breaker state).
    """
    for name, value in (
        ("vector_breaker_open_rate", vector_breaker_open_rate),
        ("lexical_breaker_open_rate", lexical_breaker_open_rate),
    ):
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"degraded_c_var requires {name} in [0, 1], got {value}")
    if vector_breaker_open_rate + lexical_breaker_open_rate > 1.0:
        raise ValueError(
            "degraded_c_var requires vector_breaker_open_rate + "
            "lexical_breaker_open_rate <= 1.0, got "
            f"{vector_breaker_open_rate + lexical_breaker_open_rate}"
        )

    p_v, p_l = vector_breaker_open_rate, lexical_breaker_open_rate
    p_both_up = 1.0 - p_v - p_l
    embedding_term = inputs.c_var_breakdown.embedding_term
    index_term = (
        p_both_up * (inputs.vector_query_cost + inputs.lexical_query_cost)
        + p_v * inputs.lexical_query_cost
        + p_l * inputs.vector_query_cost
    )
    return embedding_term + index_term + inputs.egress_cost
