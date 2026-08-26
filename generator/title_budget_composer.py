from __future__ import annotations
import re
from typing import Any

from core.specification_dominance import SpecificationDominance


class TitleBudgetComposer:
    """
    Stable Title Pipeline V1.0: deterministic 75-char budget.
    Uses only full_text or AI-supplied complete short_text.
    """

    VERSION = "stable-v1.8-relationship-and-spec-fallback"
    MIN_TARGET = 61
    MAX_LENGTH = 75

    @staticmethod
    def _clean(v: Any) -> str:
        return re.sub(r"\s+", " ", str(v or "")).strip()

    @staticmethod
    def _join(parts: list[str]) -> str:
        return TitleBudgetComposer._clean(" ".join(x for x in parts if x))

    @staticmethod
    def _already_used(
        fact: dict,
        text: str,
        parts: list[str],
    ) -> bool:
        """
        Exact semantic duplicate guard.

        Important:
        MODEL / PART_NUMBER / COMPATIBILITY_MODEL values remain distinct facts
        even when one happens to be a substring of another identifier.

        Example:
        WP6-2022030
        202203
        6-2022030

        These may all carry search value and must not be dropped merely because
        Python substring matching says one appears inside another.
        """

        text = TitleBudgetComposer._clean(text)
        if not text:
            return True

        fact_type = TitleBudgetComposer._clean(
            fact.get("type")
        ).upper()

        if fact_type in {
            "MODEL",
            "PART_NUMBER",
            "COMPATIBILITY_MODEL",
        }:
            return any(
                TitleBudgetComposer._clean(part).casefold()
                ==
                text.casefold()
                for part in parts
            )

        return (
            text.casefold()
            in
            TitleBudgetComposer._join(parts).casefold()
        )

    @staticmethod
    def _blocked_by_selected_dominant(fact: dict, used: list[dict]) -> bool:
        parent_id = TitleBudgetComposer._clean(fact.get("dominated_by_fact_id", ""))
        if not parent_id:
            return False
        return parent_id in {
            TitleBudgetComposer._clean(item.get("fact_id", ""))
            for item in used
            if isinstance(item, dict)
        }

    @staticmethod
    def _variant(fact: dict, parts: list[str]) -> str:
        full = TitleBudgetComposer._clean(fact.get("full_text"))
        short = TitleBudgetComposer._clean(fact.get("short_text"))

        for text in [full, short]:
            if not text:
                continue

            if TitleBudgetComposer._already_used(
                fact,
                text,
                parts,
            ):
                continue

            if (
                len(
                    TitleBudgetComposer._join(
                        parts + [text]
                    )
                )
                <=
                TitleBudgetComposer.MAX_LENGTH
            ):
                return text

        return ""

    @staticmethod
    def _render_selected(selected: list[dict]) -> str:
        quantity=[x for x in selected if x.get("type")=="QUANTITY"]
        rest=[x for x in selected if x.get("type")!="QUANTITY"]
        rest.sort(key=lambda item:(int(item.get("order_index",99)),int(item.get("selection_rank",99))))

        relationship_brands=[]
        for item in rest:
            if TitleBudgetComposer._clean(item.get("type")).upper() in {"MODEL","PART_NUMBER","COMPATIBILITY_MODEL"}:
                rb=TitleBudgetComposer._clean(item.get("relationship_brand",""))
                if rb and rb.casefold() not in {x.casefold() for x in relationship_brands}:
                    relationship_brands.append(rb)

        if len(relationship_brands)<2:
            return TitleBudgetComposer._join([x.get("selected_text","") for x in quantity+rest])

        compatibility_items=[x for x in rest if TitleBudgetComposer._clean(x.get("type")).upper()=="COMPATIBILITY_BRAND"]
        first_brand=TitleBudgetComposer._clean(compatibility_items[0].get("text","")) if compatibility_items else ""
        related={TitleBudgetComposer._clean(x.get("relationship_brand","")).casefold() for x in rest if TitleBudgetComposer._clean(x.get("relationship_brand",""))}
        rendered=[]; rendered_rel=set()
        for item in quantity+rest:
            typ=TitleBudgetComposer._clean(item.get("type","")).upper()
            selected_text=TitleBudgetComposer._clean(item.get("selected_text",""))
            if not selected_text: continue
            if typ=="COMPATIBILITY_BRAND":
                brand=TitleBudgetComposer._clean(item.get("text",""))
                if first_brand and brand.casefold()!=first_brand.casefold() and brand.casefold() in related:
                    continue
                rendered.append(selected_text); continue
            if typ in {"MODEL","PART_NUMBER","COMPATIBILITY_MODEL"}:
                rb=TitleBudgetComposer._clean(item.get("relationship_brand",""))
                if rb:
                    key=rb.casefold()
                    if first_brand and key==first_brand.casefold():
                        rendered.append(selected_text); rendered_rel.add(key); continue
                    if key not in rendered_rel:
                        rendered.append(", "+rb+" "+selected_text); rendered_rel.add(key)
                    else:
                        rendered.append(selected_text)
                    continue
            rendered.append(selected_text)
        title=" ".join(rendered)
        title=re.sub(r"\s+,",",",title)
        title=re.sub(r",\s+",", ",title)
        return TitleBudgetComposer._clean(title)

    @staticmethod
    def _required_core_solution(
        required: list[dict],
    ) -> tuple[list[dict], list[dict]]:
        """
        Global required-core variant search.

        A previous greedy algorithm could choose a long full Identity first,
        then discover that Compatibility + Primary Model no longer fit.

        This searches all full_text/short_text combinations for the small
        required set and chooses a valid <=75 solution.

        Preference:
        1. all required facts must survive
        2. fewer short_text substitutions
        3. higher total retained text length
        """

        variant_sets = []

        for fact in required:
            full = TitleBudgetComposer._clean(
                fact.get(
                    "full_text",
                    "",
                )
            )

            short = TitleBudgetComposer._clean(
                fact.get(
                    "short_text",
                    "",
                )
            )

            variants = []

            if full:
                variants.append(
                    (
                        full,
                        "full_text",
                        0,
                    )
                )

            if (
                short
                and
                short.casefold()
                !=
                full.casefold()
            ):
                variants.append(
                    (
                        short,
                        "short_text",
                        1,
                    )
                )

            if not variants:
                return [], [
                    {
                        **fact,
                        "reason":
                            "REQUIRED_FACT_HAS_NO_EXPRESSION",
                    }
                ]

            variant_sets.append(
                (
                    fact,
                    variants,
                )
            )

        best = None

        def search(
            index,
            selected,
            short_count,
        ):
            nonlocal best

            if index >= len(
                variant_sets
            ):
                title = (
                    TitleBudgetComposer
                    ._render_selected(
                        selected
                    )
                )

                if len(title) > 75:
                    return

                score = (
                    short_count,
                    -len(title),
                )

                if (
                    best is None
                    or
                    score
                    <
                    best[
                        "score"
                    ]
                ):
                    best = {
                        "score":
                            score,
                        "selected":
                            [
                                dict(item)
                                for item
                                in selected
                            ],
                    }

                return

            fact, variants = (
                variant_sets[
                    index
                ]
            )

            for (
                expression,
                source_name,
                short_penalty,
            ) in variants:

                item = {
                    **fact,
                    "selected_text":
                        expression,
                    "selected_source":
                        source_name,
                }

                search(
                    index + 1,
                    selected + [item],
                    short_count
                    +
                    short_penalty,
                )

        search(
            0,
            [],
            0,
        )

        if best is None:
            return [], [
                {
                    **fact,
                    "reason":
                        "REQUIRED_CORE_OVERFLOW",
                }
                for fact
                in required
            ]

        return (
            best[
                "selected"
            ],
            [],
        )


    @staticmethod
    def _best_optional_completion(
        base_used: list[dict],
        optional: list[dict],
    ) -> tuple[list[dict], bool]:
        """
        Global bounded completion search for the <=75 character budget.

        Why this exists:
        a greedy optional-fact pass can select one earlier fact that blocks a
        later combination which would safely reach the 61-character target.

        Rules:
        - required facts are frozen in base_used;
        - every added expression must be a complete full_text or AI-provided
          complete short_text;
        - no semantic cropping or invented filler;
        - hard max remains 75;
        - if any factual combination can reach 61, prefer the highest-value
          factual combination;
        - otherwise return the longest safe factual title and report that
          the 61 target is not feasible from available facts.
        """

        if not isinstance(base_used, list):
            base_used = []

        if not isinstance(optional, list):
            optional = []

        base_title = TitleBudgetComposer._render_selected(base_used)

        if len(base_title) > TitleBudgetComposer.MAX_LENGTH:
            return list(base_used), False

        # Beam/DP states.  Because title length is capped at 75, keeping a
        # small number of semantically different states per length is enough
        # to avoid the old greedy dead-end without exponential growth.
        states = [{
            "used": list(base_used),
            "value": 0.0,
            "short_count": 0,
        }]

        for fact in optional:
            if not isinstance(fact, dict):
                continue

            full_text = TitleBudgetComposer._clean(
                fact.get("full_text", "")
            )
            short_text = TitleBudgetComposer._clean(
                fact.get("short_text", "")
            )

            variants = []

            # Prefer the explicit AI compact form for optional facts, but
            # preserve both complete expressions as searchable alternatives.
            if short_text:
                variants.append((short_text, "short_text", 1))

            if full_text and full_text.casefold() != short_text.casefold():
                variants.append((full_text, "full_text", 0))

            if not variants:
                continue

            next_states = list(states)  # skipping this optional fact is legal

            for state in states:
                current_used = state["used"]
                current_parts = [
                    item.get("selected_text", "")
                    for item in current_used
                ]

                for expression, source_name, short_penalty in variants:
                    if TitleBudgetComposer._already_used(
                        fact,
                        expression,
                        current_parts,
                    ):
                        continue

                    item = {
                        **fact,
                        "selected_text": expression,
                        "selected_source": source_name,
                    }

                    candidate_used = current_used + [item]
                    candidate_title = TitleBudgetComposer._render_selected(
                        candidate_used
                    )

                    if len(candidate_title) > TitleBudgetComposer.MAX_LENGTH:
                        continue

                    next_states.append({
                        "used": candidate_used,
                        "value": (
                            float(state.get("value", 0.0))
                            + float(fact.get("value_score", 0.0) or 0.0)
                        ),
                        "short_count": (
                            int(state.get("short_count", 0))
                            + short_penalty
                        ),
                    })

            # Keep several states for each exact title length because different
            # selected facts can affect later duplicate checks.
            by_length: dict[int, list[dict]] = {}

            for state in next_states:
                title = TitleBudgetComposer._render_selected(state["used"])
                length = len(title)

                by_length.setdefault(length, []).append(state)

            pruned = []

            for length, bucket in by_length.items():
                bucket.sort(
                    key=lambda s: (
                        -float(s.get("value", 0.0)),
                        int(s.get("short_count", 0)),
                        -len(s.get("used", [])),
                    )
                )
                pruned.extend(bucket[:8])

            # Global cap is defensive; 75 possible lengths * 8 states = 600.
            states = sorted(
                pruned,
                key=lambda s: (
                    -float(s.get("value", 0.0)),
                    int(s.get("short_count", 0)),
                    -len(TitleBudgetComposer._render_selected(s["used"])),
                ),
            )[:600]

        if not states:
            return list(base_used), False

        target_states = [
            state
            for state in states
            if (
                TitleBudgetComposer.MIN_TARGET
                <= len(
                    TitleBudgetComposer._render_selected(
                        state["used"]
                    )
                )
                <= TitleBudgetComposer.MAX_LENGTH
            )
        ]

        if target_states:
            # Quality first once the target band is reachable.
            target_states.sort(
                key=lambda s: (
                    -float(s.get("value", 0.0)),
                    int(s.get("short_count", 0)),
                    -len(TitleBudgetComposer._render_selected(s["used"])),
                )
            )
            return list(target_states[0]["used"]), True

        # No factual combination can reach 61.  Return the longest safe title;
        # this proves the short title is source-fact-limited rather than a
        # composition bug.
        states.sort(
            key=lambda s: (
                -len(TitleBudgetComposer._render_selected(s["used"])),
                -float(s.get("value", 0.0)),
                int(s.get("short_count", 0)),
            )
        )

        return list(states[0]["used"]), False


    @staticmethod
    def _promote_high_value_facts(
        used: list[dict],
        rejected: list[dict],
    ) -> tuple[list[dict], list[dict]]:
        """
        Conservative post-selection promotion.

        A rejected high-value identifier may replace strictly lower-priority
        optional content if that is the only reason it cannot fit.

        Allowed promoted types:
        - MODEL
        - PART_NUMBER
        - COMPATIBILITY_MODEL
        - optional COMPATIBILITY_BRAND

        Never:
        - remove a required fact
        - replace an equal/higher-priority fact
        - invent/crop an identifier
        - exceed 75 chars
        - push an already >=61 title below the target band
        """
        if not isinstance(used, list):
            used = []

        if not isinstance(rejected, list):
            rejected = []

        original_title = TitleBudgetComposer._render_selected(
            used
        )
        preserve_min = (
            len(original_title)
            >=
            TitleBudgetComposer.MIN_TARGET
        )

        promotable_types = {
            "MODEL",
            "PART_NUMBER",
            "COMPATIBILITY_MODEL",
            "COMPATIBILITY_BRAND",
        }

        candidate_rejected = [
            fact
            for fact in rejected
            if (
                isinstance(fact, dict)
                and fact.get("reason") == "CHARACTER_BUDGET"
                and TitleBudgetComposer._clean(
                    fact.get("type")
                ).upper()
                in promotable_types
            )
        ]

        candidate_rejected.sort(
            key=lambda fact: (
                int(
                    fact.get(
                        "selection_rank",
                        99,
                    )
                ),
                -float(
                    fact.get(
                        "value_score",
                        0,
                    )
                ),
                int(
                    fact.get(
                        "order_index",
                        999,
                    )
                ),
            )
        )

        current_used = list(used)
        current_rejected = list(rejected)

        for high in candidate_rejected:
            high_rank = int(
                high.get(
                    "selection_rank",
                    99,
                )
            )

            high_text = (
                TitleBudgetComposer._clean(
                    high.get(
                        "short_text",
                        "",
                    )
                )
                or
                TitleBudgetComposer._clean(
                    high.get(
                        "full_text",
                        "",
                    )
                )
            )

            if not high_text:
                continue

            parts = [
                item.get(
                    "selected_text",
                    "",
                )
                for item in current_used
            ]

            if TitleBudgetComposer._already_used(
                high,
                high_text,
                parts,
            ):
                continue

            promoted_item = {
                **high,
                "selected_text": high_text,
                "selected_source": (
                    "short_text"
                    if TitleBudgetComposer._clean(
                        high.get(
                            "short_text",
                            "",
                        )
                    )
                    else
                    "full_text"
                ),
            }

            direct = (
                current_used
                +
                [promoted_item]
            )

            direct_title = (
                TitleBudgetComposer
                ._render_selected(
                    direct
                )
            )

            if (
                len(direct_title)
                <=
                TitleBudgetComposer.MAX_LENGTH
            ):
                current_used = direct
                current_rejected = [
                    item
                    for item in current_rejected
                    if item.get("fact_id")
                    != high.get("fact_id")
                ]
                continue

            removable = [
                item
                for item in current_used
                if (
                    not item.get(
                        "required"
                    )
                    and
                    int(
                        item.get(
                            "selection_rank",
                            99,
                        )
                    )
                    >
                    high_rank
                )
            ]

            # Lowest-value / lowest-priority content is removed first.
            removable.sort(
                key=lambda item: (
                    -int(
                        item.get(
                            "selection_rank",
                            99,
                        )
                    ),
                    float(
                        item.get(
                            "value_score",
                            0,
                        )
                    ),
                    -len(
                        TitleBudgetComposer._clean(
                            item.get(
                                "selected_text",
                                "",
                            )
                        )
                    ),
                )
            )

            trial_used = list(
                current_used
            )
            removed = []

            for low in removable:
                trial_used = [
                    item
                    for item in trial_used
                    if item.get(
                        "fact_id"
                    )
                    !=
                    low.get(
                        "fact_id"
                    )
                ]
                removed.append(
                    low
                )

                trial_with_high = (
                    trial_used
                    +
                    [promoted_item]
                )

                trial_title = (
                    TitleBudgetComposer
                    ._render_selected(
                        trial_with_high
                    )
                )

                if (
                    len(trial_title)
                    <=
                    TitleBudgetComposer.MAX_LENGTH
                    and
                    (
                        not preserve_min
                        or
                        len(trial_title)
                        >=
                        TitleBudgetComposer.MIN_TARGET
                    )
                ):
                    current_used = (
                        trial_with_high
                    )

                    removed_ids = {
                        item.get(
                            "fact_id"
                        )
                        for item in removed
                    }

                    current_rejected = [
                        item
                        for item in current_rejected
                        if item.get(
                            "fact_id"
                        )
                        !=
                        high.get(
                            "fact_id"
                        )
                    ]

                    current_rejected.extend([
                        {
                            **item,
                            "reason":
                                "LOWER_VALUE_REPLACED_BY_HIGH_VALUE_FACT",
                        }
                        for item in removed
                        if item.get(
                            "fact_id"
                        )
                        not in {
                            x.get("fact_id")
                            for x in current_rejected
                        }
                    ])

                    break

        return (
            current_used,
            current_rejected,
        )

    @staticmethod
    def compose(plan: dict) -> dict:
        facts = plan.get(
            "facts",
            [],
        )

        if not isinstance(
            facts,
            list,
        ):
            facts = []

        facts, specification_dominance_audit = (
            SpecificationDominance.filter_dominated_facts(facts)
        )

        required = [
            fact
            for fact in facts
            if (
                isinstance(
                    fact,
                    dict,
                )
                and
                fact.get(
                    "required"
                )
            )
        ]

        optional = [
            fact
            for fact in facts
            if (
                isinstance(
                    fact,
                    dict,
                )
                and
                not fact.get(
                    "required"
                )
            )
        ]

        used, rejected = (
            TitleBudgetComposer
            ._required_core_solution(
                required
            )
        )

        if rejected:
            return {
                "version":
                    TitleBudgetComposer.VERSION,
                "title":
                    "",
                "status":
                    "TITLE_BUDGET_CONFLICT",
                "used_facts":
                    [],
                "rejected_facts":
                    rejected,
                "character_count":
                    0,
                "budget_remaining":
                    75,
            }

        # Optional facts are selected by frozen content priority/value,
        # but later rendered by language-aware order_index.
        optional.sort(
            key=lambda fact: (
                int(
                    fact.get(
                        "selection_rank",
                        99,
                    )
                ),
                -float(
                    fact.get(
                        "value_score",
                        0,
                    )
                ),
                len(
                    TitleBudgetComposer
                    ._clean(
                        fact.get(
                            "full_text",
                            "",
                        )
                    )
                ),
            )
        )

        for fact in optional:

            if TitleBudgetComposer._blocked_by_selected_dominant(fact, used):
                rejected.append({
                    **fact,
                    "reason": "DOMINATED_SPECIFICATION_ALREADY_USED",
                })
                continue

            short_text = (
                TitleBudgetComposer
                ._clean(
                    fact.get(
                        "short_text",
                        "",
                    )
                )
            )

            full_text = (
                TitleBudgetComposer
                ._clean(
                    fact.get(
                        "full_text",
                        "",
                    )
                )
            )

            # For optional facts, an explicit AI short_text is a deliberate
            # natural-language expression designed for tight budget. Prefer it.
            variants = []

            if short_text:
                variants.append(
                    (
                        short_text,
                        "short_text",
                    )
                )

            if full_text:
                variants.append(
                    (
                        full_text,
                        "full_text",
                    )
                )

            selected_item = None

            for (
                expression,
                source_name,
            ) in variants:

                if not expression:
                    continue

                current_parts = [
                    item.get(
                        "selected_text",
                        "",
                    )
                    for item in used
                ]

                if (
                    TitleBudgetComposer
                    ._already_used(
                        fact,
                        expression,
                        current_parts,
                    )
                ):
                    continue

                candidate_used = (
                    used
                    +
                    [
                        {
                            **fact,
                            "selected_text":
                                expression,
                            "selected_source":
                                source_name,
                        }
                    ]
                )

                candidate_title = (
                    TitleBudgetComposer
                    ._render_selected(
                        candidate_used
                    )
                )

                if (
                    len(
                        candidate_title
                    )
                    <=
                    TitleBudgetComposer
                    .MAX_LENGTH
                ):
                    selected_item = (
                        candidate_used[
                            -1
                        ]
                    )
                    break

            if selected_item:
                used.append(
                    selected_item
                )
            else:
                rejected.append(
                    {
                        **fact,
                        "reason":
                            "CHARACTER_BUDGET",
                    }
                )

        # High-value coverage pass:
        # a model/part number may replace strictly lower-priority optional
        # content, but never required or equal/higher-priority facts.
        used, rejected = (
            TitleBudgetComposer
            ._promote_high_value_facts(
                used,
                rejected,
            )
        )

        title = (
            TitleBudgetComposer
            ._render_selected(
                used
            )
        )

        # If the full required core leaves the title just under 61 while
        # optional facts cannot fit, retry with the shortest SAFE AI-provided
        # required expressions. This can free budget for higher-value context
        # without semantic cropping.
        if (
            len(title)
            <
            TitleBudgetComposer.MIN_TARGET
        ):

            compact_required = []

            compact_possible = False

            for fact in required:

                full_text = (
                    TitleBudgetComposer
                    ._clean(
                        fact.get(
                            "full_text",
                            "",
                        )
                    )
                )

                short_text = (
                    TitleBudgetComposer
                    ._clean(
                        fact.get(
                            "short_text",
                            "",
                        )
                    )
                )

                if (
                    short_text
                    and
                    short_text.casefold()
                    !=
                    full_text.casefold()
                ):
                    expression = short_text
                    source_name = "short_text"
                    compact_possible = True
                else:
                    expression = full_text
                    source_name = "full_text"

                compact_required.append(
                    {
                        **fact,
                        "selected_text":
                            expression,
                        "selected_source":
                            source_name,
                    }
                )

            if compact_possible:

                compact_title = (
                    TitleBudgetComposer
                    ._render_selected(
                        compact_required
                    )
                )

                if (
                    compact_title
                    and
                    len(
                        compact_title
                    )
                    <=
                    75
                ):
                    retry_used = list(
                        compact_required
                    )

                    retry_rejected = []

                    for fact in optional:

                        short_text = (
                            TitleBudgetComposer
                            ._clean(
                                fact.get(
                                    "short_text",
                                    "",
                                )
                            )
                        )

                        full_text = (
                            TitleBudgetComposer
                            ._clean(
                                fact.get(
                                    "full_text",
                                    "",
                                )
                            )
                        )

                        variants = []

                        if short_text:
                            variants.append(
                                (
                                    short_text,
                                    "short_text",
                                )
                            )

                        if full_text:
                            variants.append(
                                (
                                    full_text,
                                    "full_text",
                                )
                            )

                        selected_item = None

                        for (
                            expression,
                            source_name,
                        ) in variants:

                            if not expression:
                                continue

                            current_parts = [
                                item.get(
                                    "selected_text",
                                    "",
                                )
                                for item
                                in retry_used
                            ]

                            if (
                                TitleBudgetComposer
                                ._already_used(
                                    fact,
                                    expression,
                                    current_parts,
                                )
                            ):
                                continue

                            candidate_used = (
                                retry_used
                                +
                                [
                                    {
                                        **fact,
                                        "selected_text":
                                            expression,
                                        "selected_source":
                                            source_name,
                                    }
                                ]
                            )

                            candidate_title = (
                                TitleBudgetComposer
                                ._render_selected(
                                    candidate_used
                                )
                            )

                            if (
                                len(
                                    candidate_title
                                )
                                <=
                                75
                            ):
                                selected_item = (
                                    candidate_used[
                                        -1
                                    ]
                                )
                                break

                        if selected_item:
                            retry_used.append(
                                selected_item
                            )
                        else:
                            retry_rejected.append(
                                {
                                    **fact,
                                    "reason":
                                        "CHARACTER_BUDGET",
                                }
                            )

                    retry_title = (
                        TitleBudgetComposer
                        ._render_selected(
                            retry_used
                        )
                    )

                    if (
                        len(
                            retry_title
                        )
                        >
                        len(
                            title
                        )
                        or
                        (
                            len(
                                retry_title
                            )
                            >=
                            61
                            and
                            len(
                                title
                            )
                            <
                            61
                        )
                    ):
                        used = retry_used
                        rejected = retry_rejected
                        title = retry_title

        # Final deterministic global completion pass.
        #
        # The earlier greedy pass is retained for stable normal behavior.
        # Only when it still lands below 61 do we search the bounded factual
        # combination space.  This distinguishes:
        #   A) a real composition miss (a >=61 factual combination exists)
        #   B) source facts genuinely insufficient to reach 61 safely.
        target_feasible = len(title) >= TitleBudgetComposer.MIN_TARGET

        if len(title) < TitleBudgetComposer.MIN_TARGET:
            global_used, target_feasible = (
                TitleBudgetComposer
                ._best_optional_completion(
                    base_used=(
                        TitleBudgetComposer
                        ._required_core_solution(
                            required
                        )[0]
                    ),
                    optional=optional,
                )
            )

            global_title = (
                TitleBudgetComposer
                ._render_selected(
                    global_used
                )
            )

            if (
                global_title
                and
                len(global_title)
                <=
                TitleBudgetComposer.MAX_LENGTH
            ):
                used = global_used
                title = global_title

                selected_ids = {
                    item.get("fact_id")
                    for item in used
                    if isinstance(item, dict)
                }

                rejected = [
                    {
                        **fact,
                        "reason": "CHARACTER_BUDGET",
                    }
                    for fact in optional
                    if (
                        isinstance(fact, dict)
                        and
                        fact.get("fact_id")
                        not in selected_ids
                    )
                ]

        if len(title) > 75:
            # Structural fail-safe. This should be unreachable.
            return {
                "version":
                    TitleBudgetComposer.VERSION,
                "title":
                    "",
                "status":
                    "TITLE_BUDGET_CONFLICT",
                "used_facts":
                    used,
                "rejected_facts":
                    rejected,
                "character_count":
                    0,
                "budget_remaining":
                    75,
            }

        if len(title) >= 61:
            status = "READY"

        else:
            # If global completion proved that no complete, source-supported
            # fact combination can reach 61 within 75 characters, the short
            # title is valid and source-limited rather than a pipeline failure.
            status = (
                "TITLE_COMPOSITION_FAILED"
                if target_feasible
                else
                "SOURCE_FACTS_INSUFFICIENT"
            )

        return {
            "version":
                TitleBudgetComposer.VERSION,
            "title":
                title,
            "status":
                status,
            "used_facts":
                used,
            "rejected_facts":
                rejected,
            "character_count":
                len(title),
            "budget_remaining":
                max(
                    0,
                    75
                    -
                    len(title),
                ),
        }

