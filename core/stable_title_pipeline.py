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

from generator.title_final_validator import (
    TitleFinalValidator,
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

    VERSION = "stable-title-pipeline-v1.0"

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

        # 5. Program owns final PASS/FAIL.
        validation = (
            TitleFinalValidator
            .validate(
                composed=composed,
                resolved=resolved,
                target_language=target_language,
            )
        )

        status = (
            "PASS"
            if validation.get(
                "status"
            )
            ==
            "PASS"
            else
            composed.get(
                "status",
                "FAIL",
            )
        )

        if (
            validation.get(
                "status"
            )
            !=
            "PASS"
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
                composed,

            "validation":
                validation,
        }
