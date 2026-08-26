from __future__ import annotations

from typing import Any

from core.title_fact_resolver import (
    TitleFactResolver,
)

from analyzer.title_priority_planner import (
    TitlePriorityPlanner,
)

from analyzer.title_priority_planner_generator import (
    TitlePriorityPlannerGenerator,
    TitlePriorityPlannerError,
)

from generator.title_budget_composer import (
    TitleBudgetComposer,
)

from generator.title_deterministic_repair import (
    TitleDeterministicRepair,
)

from generator.title_post_repair_completion import (
    TitlePostRepairCompletion,
)

from generator.title_quality_gate import (
    TitleQualityGate,
)

from generator.title_fact_traceability_gate import (
    TitleFactTraceabilityGate,
)


from generator.title_final_validator import (
    TitleFinalValidator,
)

from analyzer.title_required_core_repair import (
    TitleRequiredCoreRepair,
    RequiredCoreRepairError,
)


class StableTitlePipeline:
    """
    Stable Title Pipeline V1.0

    Frozen execution order:

    Profile
    -> Fact Resolver
    -> AI Priority Planner
    -> Deterministic Priority Planner
    -> Deterministic Budget Composer
    -> Deterministic Final Validator

    No downstream stage may reinterpret source facts.
    """

    VERSION = "stable-title-pipeline-v1.2-traceability-closure"

    @staticmethod
    def _target_language(profile: dict) -> str:
        if not isinstance(profile, dict):
            return "English"

        return (
            profile.get(
                "target_language"
            )
            or
            profile.get(
                "language"
            )
            or
            (
                profile.get(
                    "title_strategy_input",
                    {}
                ).get(
                    "target_language"
                )
                if isinstance(
                    profile.get(
                        "title_strategy_input",
                        {}
                    ),
                    dict
                )
                else
                None
            )
            or
            "English"
        )

    @staticmethod
    def run(
        profile: dict,
        api_key: str,
        model: str = "gpt-4.1-mini",
        use_ai_planner: bool = True,
    ) -> dict:

        if not isinstance(profile, dict):
            raise ValueError(
                "StableTitlePipeline profile must be a dictionary"
            )

        target_language = (
            StableTitlePipeline
            ._target_language(
                profile
            )
        )

        # 1. Facts only.
        resolved = (
            TitleFactResolver
            .resolve(
                profile
            )
        )

        # 2. AI may only rank approved facts and suggest safe short_text.
        if use_ai_planner:

            try:
                ai_plan = (
                    TitlePriorityPlannerGenerator
                    .generate(
                        resolved_facts=resolved,
                        api_key=api_key,
                        model=model,
                        target_language=target_language,
                    )
                )

                ai_planner_status = "success"

            except TitlePriorityPlannerError as exc:
                # Fail soft only for AI ranking:
                # facts remain intact and deterministic priorities remain available.
                ai_plan = {
                    "fact_priorities":
                        []
                }

                ai_planner_status = (
                    f"fallback:{exc}"
                )

        else:
            ai_plan = {
                "fact_priorities":
                    []
            }

            ai_planner_status = (
                "disabled"
            )

        # 3. Combine frozen base priority + AI value/short expressions.
        plan = (
            TitlePriorityPlanner
            .build(
                resolved_facts=resolved,
                ai_plan=ai_plan,
                target_language=target_language,
            )
        )

        # 4. Program owns character budget.
        composed = (
            TitleBudgetComposer
            .compose(
                plan
            )
        )

        # Rare overflow repair. Normal successful rows never enter this path.
        # If the immutable required core cannot fit, request ONE stricter
        # identity-only compression using an exact identity character budget.
        # The repaired expression must pass deterministic token/subset safety
        # checks, then the ordinary planner/composer are rerun unchanged.
        required_core_repair = {}

        if (
            use_ai_planner
            and composed.get(
                "status"
            )
            ==
            "TITLE_BUDGET_CONFLICT"
        ):
            try:
                required_core_repair = (
                    TitleRequiredCoreRepair
                    .generate(
                        plan=plan,
                        api_key=api_key,
                        model=model,
                        target_language=target_language,
                        max_length=TitleBudgetComposer.MAX_LENGTH,
                    )
                )

                repaired_fact_id = (
                    required_core_repair
                    .get(
                        "fact_id",
                        "",
                    )
                )

                # Merge only the repaired short_text into the existing AI
                # priority plan. Scores/order/reasons remain untouched.
                repaired_ai_plan = {
                    **ai_plan,
                    "fact_priorities": [
                        (
                            {
                                **item,
                                "short_text": (
                                    required_core_repair
                                    .get(
                                        "short_text",
                                        "",
                                    )
                                ),
                                "reason": (
                                    item.get(
                                        "reason",
                                        "",
                                    )
                                    +
                                    " | required-core overflow repair"
                                ).strip(
                                    " |"
                                ),
                            }
                            if (
                                isinstance(
                                    item,
                                    dict,
                                )
                                and
                                item.get(
                                    "fact_id"
                                )
                                ==
                                repaired_fact_id
                            )
                            else
                            item
                        )
                        for item
                        in (
                            ai_plan.get(
                                "fact_priorities",
                                [],
                            )
                            or
                            []
                        )
                    ],
                }

                plan = (
                    TitlePriorityPlanner
                    .build(
                        resolved_facts=resolved,
                        ai_plan=repaired_ai_plan,
                        target_language=target_language,
                    )
                )

                composed = (
                    TitleBudgetComposer
                    .compose(
                        plan
                    )
                )

                ai_plan = repaired_ai_plan
                ai_planner_status = (
                    ai_planner_status
                    +
                    "+required_core_repair"
                )

            except RequiredCoreRepairError as exc:
                required_core_repair = {
                    "status": "failed",
                    "error": str(exc),
                }

        # 5. Deterministic presentation cleanup.
        #
        # This stage may only delete exact duplication / known noise or merge
        # repeated compatibility syntax. It never invents or reinterpret facts.
        deterministic_repair = (
            TitleDeterministicRepair
            .repair(
                composed.get(
                    "title",
                    "",
                ),
                target_language,
            )
        )

        # 6. Refill factual budget freed by cleanup.
        #
        # Only already-approved plan facts may be added back.  Every candidate
        # is checked by the Quality Gate before acceptance.
        post_repair_completion = (
            TitlePostRepairCompletion
            .complete(
                title=deterministic_repair.get(
                    "title",
                    "",
                ),
                plan=plan,
                composed=composed,
                target_language=target_language,
            )
        )

        final_title = (
            post_repair_completion
            .get(
                "title",
                deterministic_repair.get(
                    "title",
                    "",
                ),
            )
        )

        final_used_facts = list(
            composed.get(
                "used_facts",
                [],
            )
            or
            []
        )

        plan_facts_by_id = {
            item.get(
                "fact_id"
            ):
            item
            for item
            in (
                plan.get(
                    "facts",
                    [],
                )
                or
                []
            )
            if isinstance(
                item,
                dict,
            )
        }

        for added in (
            post_repair_completion
            .get(
                "added_facts",
                [],
            )
            or
            []
        ):
            if not isinstance(
                added,
                dict,
            ):
                continue

            fact_id = added.get(
                "fact_id"
            )

            base_fact = (
                plan_facts_by_id
                .get(
                    fact_id
                )
            )

            if not isinstance(
                base_fact,
                dict,
            ):
                continue

            final_used_facts.append({
                **base_fact,
                "selected_text":
                    added.get(
                        "selected_text",
                        "",
                    ),
                "selected_source":
                    added.get(
                        "selected_source",
                        "",
                    ),
            })

        if (
            post_repair_completion
            .get(
                "status"
            )
            ==
            "SOURCE_FACTS_INSUFFICIENT"
        ):
            final_composition_status = (
                "SOURCE_FACTS_INSUFFICIENT"
            )

        elif (
            len(
                final_title
            )
            >=
            TitleBudgetComposer.MIN_TARGET
        ):
            final_composition_status = (
                "READY"
            )

        else:
            final_composition_status = (
                composed.get(
                    "status",
                    "",
                )
            )

        final_composed = {
            **composed,
            "title":
                final_title,
            "status":
                final_composition_status,
            "used_facts":
                final_used_facts,
            "character_count":
                len(
                    final_title
                ),
            "budget_remaining":
                max(
                    0,
                    TitleBudgetComposer.MAX_LENGTH
                    -
                    len(
                        final_title
                    ),
                ),
        }

        # 7. Presentation quality gate.
        quality_validation = (
            TitleQualityGate
            .validate(
                final_title,
                target_language,
            )
        )

        # 8. Final critical-fact provenance gate.
        fact_traceability = (
            TitleFactTraceabilityGate
            .validate(
                profile=profile,
                composed=final_composed,
            )
        )

        # 9. Program owns final factual PASS/FAIL.
        validation = (
            TitleFinalValidator
            .validate(
                composed=final_composed,
                resolved=resolved,
                target_language=target_language,
            )
        )

        status = (
            "PASS"
            if (
                validation.get(
                    "status"
                )
                ==
                "PASS"
                and
                quality_validation.get(
                    "status"
                )
                ==
                "PASS"
                and
                fact_traceability.get(
                    "status"
                )
                ==
                "PASS"
            )
            else
            final_composed.get(
                "status",
                "FAIL",
            )
        )

        if (
            (
                validation.get(
                    "status"
                )
                !=
                "PASS"
                or
                quality_validation.get(
                    "status"
                )
                !=
                "PASS"
                or
                fact_traceability.get(
                    "status"
                )
                !=
                "PASS"
            )
            and
            status
            ==
            "READY"
        ):
            status = (
                "FINAL_VALIDATION_FAILED"
            )

        return {
            "pipeline_version":
                StableTitlePipeline.VERSION,

            "status":
                status,

            "title":
                validation.get(
                    "title",
                    ""
                ),

            "character_count":
                validation.get(
                    "character_count",
                    0
                ),

            "target_language":
                target_language,

            "fact_resolution":
                resolved,

            "ai_priority_plan":
                ai_plan,

            "ai_planner_status":
                ai_planner_status,

            "priority_plan":
                plan,

            "composition":
                final_composed,

            "pre_repair_composition":
                composed,

            "deterministic_repair":
                deterministic_repair,

            "post_repair_completion":
                post_repair_completion,

            "quality_validation":
                quality_validation,

            "fact_traceability":
                fact_traceability,

            "required_core_repair":
                required_core_repair,

            "validation":
                validation,
        }
