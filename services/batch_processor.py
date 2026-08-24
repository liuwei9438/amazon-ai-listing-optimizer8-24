from __future__ import annotations

import json
import time

from datetime import datetime

from pathlib import Path


from analyzer.product_understanding import (
    ProductUnderstandingEngine,
    UnderstandingError,
)
from core.strategy_input_builder import (
    StrategyInputBuilder,
    StrategyInputBuilderError,
)

from analyzer.model_protection import ModelProtection
from analyzer.seo_intent_engine import generate_primary_search
from analyzer.seo_keyword_engine import SEOKeywordEngine

from core.stable_title_pipeline import (
    StableTitlePipeline,
)
from understanding.identity_decision import (
    IdentityDecisionEngine,
    IdentityDecisionError,
)
from compliance.brand_protection import protect_text

from core.product_knowledge import ProductKnowledgeBuilder
from core.knowledge_normalizer import (
    KnowledgeNormalizer,
    KnowledgeNormalizerError,
)
from core.title_planner import TitlePlanner

from generator.highlight_generator import HighlightGenerator
from generator.short_title_generator import ShortTitleGenerator
from generator.bullet_generator import BulletGenerator
from generator.description_generator import DescriptionGenerator


from services.task_manager import (
    save_status,
)

from services.task_control import load_control

from services.result_storage import (
    save_profiles,
    save_failed_items,
    reconcile_task_results,
)



def process_batch(
    records,
    task_id,
    api_key,
    model="gpt-4.1-mini",
    options=None,
):


    if options is None:
        options = {}


    """
    批量处理产品

    输入:
        records:
        ProductRecord列表

        task_id:
        当前任务ID

    输出:
        profiles
    """


    # =====================
    # 基础初始化
    # =====================

    total = len(records)


    save_status(
        task_id,
        {
            "status": "processing",
            "message": "正在初始化AI理解引擎",
            "completed": 0,
            "total": total,
            "updated_at":
                datetime.now().isoformat(),
        }
    )


    print(
        "CREATE PRODUCT UNDERSTANDING ENGINE"
    )


    engine = ProductUnderstandingEngine(
        api_key=api_key,
        model=model,
    )


    print(
        "ENGINE READY"
    )


    profiles = []

    success = 0

    failed = 0

    failed_items = []



    enable_title = options.get(
        "title",
        True
    )


    enable_short_title = options.get(
        "short_title",
        True
    )


    enable_highlight = options.get(
        "highlight",
        True
    )


    enable_bullet = options.get(
        "bullet",
        True
    )


    enable_description = options.get(
        "description",
        True
    )


    enable_seo = options.get(
        "seo",
        True
    )



    # =====================
    # 循环处理产品
    # =====================


    for index, record in enumerate(records):


        # =====================
        # 检查任务控制状态
        # =====================
    
        action = load_control(task_id)

        # Pause keeps the worker alive.  The old implementation returned here,
        # which killed the worker and made the later "resume" button ineffective.
        pause_status_written = False
        while action == "pause":
            if not pause_status_written:
                save_status(
                    task_id,
                    {
                        "task_id": task_id,
                        "status": "paused",
                        "message": "任务已暂停",
                        "completed": index,
                        "total": total,
                    }
                )
                pause_status_written = True

            time.sleep(0.5)
            action = load_control(task_id)

        if action == "cancel":
            save_status(
                task_id,
                {
                    "task_id": task_id,
                    "status": "cancelled",
                    "message": "任务已取消",
                    "completed": index,
                    "total": total,
                    "success": success,
                    "failed": failed,
                }
            )
            return profiles
    
    
    
        # 原来的状态更新保持
    
        save_status(
            task_id,
            {
                "task_id": task_id,
                "status": "processing",
                "message": f"正在处理第 {index + 1}/{total} 个产品",
                "completed": index,
                "total": total,
            }
        )

        print(
            f"PROCESS PRODUCT {index + 1}/{total}"
        )


        try:


            product_start = time.time()


            timing = {}


            # =====================
            # Product Understanding
            # =====================


            start = time.time()

            save_status(
                task_id,
                {
                    "status": "processing",
                    "message": f"第 {index+1}/{total} 个产品：AI商品理解中",
                    "completed": index,
                    "total": total,
                }
            )
            profile = engine.analyze(
                record
            )
            save_status(
                task_id,
                {
                    "status": "processing",
                    "message": f"第 {index+1}/{total} 个产品：AI商品理解",
                    "completed": index,
                    "total": total,
                }
            )

            timing["understanding"] = round(
                time.time() - start,
                2
            )
            # =====================
            # Product Knowledge
            # =====================


            start = time.time()


            product_knowledge = (
                ProductKnowledgeBuilder.build(
                    profile
                )
            )
            
            
            timing["knowledge"] = round(
                time.time() - start,
                2
            )
            
            
            profile[
                "product_knowledge"
            ] = product_knowledge
            
            # =====================
            # Identity Decision
            # =====================

            identity_start = time.time()


            save_status(
                task_id,
                {
                    "status": "processing",
                    "message":
                        f"第 {index+1}/{total} 个产品：统一产品身份中",
                    "completed": index,
                    "total": total,
                }
            )


            try:

                identity_decision = (
                    IdentityDecisionEngine.generate(
                        profile=profile,
                        api_key=api_key,
                        model=model,
                    )
                )


                if not isinstance(
                    identity_decision,
                    dict,
                ):

                    identity_decision = {}


                profile[
                    "identity_decision"
                ] = identity_decision


                profile[
                    "identity_decision_error"
                ] = ""


            except Exception as identity_exc:

                # 当前阶段 Identity Decision
                # 只是新增验证模块。
                #
                # 即使失败，
                # 也不能让整个产品优化失败。
                #
                # Title Strategy 暂时继续使用旧流程。

                identity_decision = {}


                profile[
                    "identity_decision"
                ] = {}


                profile[
                    "identity_decision_error"
                ] = str(
                    identity_exc
                )


                print(
                    "IDENTITY DECISION FAILED:",
                    identity_exc,
                )


            timing[
                "identity_decision"
            ] = round(
                time.time() - identity_start,
                2
            )
            # =====================
            # Knowledge Normalization
            # =====================

            normalize_start = time.time()


            try:

                normalized_knowledge = (
                    KnowledgeNormalizer.normalize(
                        profile
                    )
                )


                if not isinstance(
                    normalized_knowledge,
                    dict,
                ):

                    normalized_knowledge = {}


                profile[
                    "normalized_knowledge"
                ] = normalized_knowledge


                profile[
                    "knowledge_normalization_error"
                ] = ""


            except Exception as normalize_exc:

                # Normalization失败不能影响整个产品。
                #
                # Raw Knowledge和Identity Decision仍然保留。

                normalized_knowledge = {}


                profile[
                    "normalized_knowledge"
                ] = {}


                profile[
                    "knowledge_normalization_error"
                ] = str(
                    normalize_exc
                )


                print(
                    "KNOWLEDGE NORMALIZATION FAILED:",
                    normalize_exc,
                )


            timing[
                "knowledge_normalization"
            ] = round(
                time.time() - normalize_start,
                2
            )
            # =====================
            # Title Strategy Input
            # =====================

            strategy_input_start = time.time()


            try:

                strategy_input = (
                    StrategyInputBuilder.build(
                        profile
                    )
                )


                profile[
                    "title_strategy_input"
                ] = strategy_input


                profile[
                    "title_strategy_input_error"
                ] = ""


            except Exception as input_exc:

                profile[
                    "title_strategy_input"
                ] = {}


                profile[
                    "title_strategy_input_error"
                ] = str(
                    input_exc
                )


                print(
                    "TITLE STRATEGY INPUT BUILD FAILED:",
                    input_exc,
                )


            timing[
                "title_strategy_input"
            ] = round(
                time.time()
                - strategy_input_start,
                2
            )
            # =====================
            # Stable Title Pipeline
            # title planning is executed later, after all current profile
            # information has been prepared.
            #
            # IMPORTANT:
            # The old TitleStrategyGenerator is intentionally disabled here.
            # StableTitlePipeline owns:
            # Fact Resolver -> AI Priority Planner -> Budget Composer ->
            # Final Validator.
            # =====================

            profile[
                "title_strategy"
            ] = {}

            profile[
                "title_strategy_error"
            ] = ""

            # =====================
            # Legacy Title Plan
            # fallback only
            # =====================
            
            title_plan = TitlePlanner.plan(
                product_knowledge
            )
            
            
            profile[
                "title_plan"
            ] = title_plan



            # =====================
            # SEO
            # =====================


            start = time.time()


            if enable_seo:


                seo_intent = generate_primary_search(
                    profile
                )


                profile[
                    "seo_intent"
                ] = seo_intent



                seo_keywords = SEOKeywordEngine.generate(
                    profile
                )


                profile[
                    "seo"
                ] = seo_keywords


            else:


                seo_intent = {}


                profile[
                    "seo_intent"
                ] = {}


                profile[
                    "seo"
                ] = {}



            # =====================
            # Compliance
            # =====================


            detected_brands = (
                profile
                .get(
                    "brand_info",
                    {}
                )
                .get(
                    "detected_brands",
                    []
                )
            )


            primary_search = (
                seo_intent
                .get(
                    "primary_search",
                    []
                )
            )


            primary_text = (

                primary_search[0]

                if primary_search

                else ""

            )


            profile[
                "compliance_result"
            ] = protect_text(
                primary_text,
                detected_brands=detected_brands,
            )



            # =====================
            # Highlight
            # =====================


            start = time.time()


            if enable_highlight:


                highlight_result = (
                    HighlightGenerator.generate(
                        profile
                    )
                )


            else:


                highlight_result = {}



            profile[
                "highlight_result"
            ] = highlight_result



            # =====================
            # Short Title
            # =====================


            if enable_short_title:


                short_title_result = (
                    ShortTitleGenerator.generate(
                        profile
                    )
                )


            else:


                short_title_result = {}



            profile[
                "short_title_result"
            ] = short_title_result



            # =====================
            # Stable Title Pipeline V1.0
            # =====================

            start = time.time()

            if enable_title:

                save_status(
                    task_id,
                    {
                        "status": "processing",
                        "message":
                            f"第 {index+1}/{total} 个产品：Stable 标题生成",
                        "completed": index,
                        "total": total,
                    }
                )

                stable_title_result = (
                    StableTitlePipeline.run(
                        profile=profile,
                        api_key=api_key,
                        model=model,
                        use_ai_planner=True,
                    )
                )

                profile[
                    "stable_title_pipeline"
                ] = stable_title_result

                if (
                    stable_title_result.get(
                        "status"
                    )
                    !=
                    "PASS"
                ):
                    raise RuntimeError(
                        "Stable title validation failed: "
                        +
                        str(
                            stable_title_result.get(
                                "status",
                                "UNKNOWN"
                            )
                        )
                        +
                        " | "
                        +
                        str(
                            stable_title_result.get(
                                "validation",
                                {}
                            ).get(
                                "errors",
                                []
                            )
                        )
                    )

                title_result = {
                    "title":
                        stable_title_result.get(
                            "title",
                            ""
                        ),

                    "character_count":
                        stable_title_result.get(
                            "character_count",
                            0
                        ),

                    "validation":
                        stable_title_result.get(
                            "validation",
                            {}
                        ),

                    "generator_version":
                        stable_title_result.get(
                            "pipeline_version",
                            "stable-title-pipeline-v1.0"
                        ),

                    "solver": {
                        "status":
                            "resolved",
                        "stable_pipeline":
                            True,
                        "ai_planner_status":
                            stable_title_result.get(
                                "ai_planner_status",
                                ""
                            ),
                    },
                }

            else:

                stable_title_result = {}
                title_result = {}

            timing[
                "title"
            ] = round(
                time.time()
                -
                start,
                2
            )

            models = (
                ModelProtection
                .extract_models(
                    profile
                )
            )


            if enable_title:

                # Stable pipeline already validates model preservation and
                # range-compression rules. Do not mutate the title after its
                # final validator has passed.
                profile[
                    "generated_title"
                ] = title_result


            else:


                profile[
                    "generated_title"
                ] = {}



            if enable_short_title:


                profile[
                    "short_title_result"
                ] = ModelProtection.protect_result(
                    short_title_result,
                    models,
                )


            else:


                profile[
                    "short_title_result"
                ] = {}



            # =====================
            # Bullet
            # =====================


            if enable_bullet:

                save_status(
                    task_id,
                    {
                        "status": "processing",
                        "message": f"第 {index+1}/{total} 个产品：生成五点",
                        "completed": index,
                        "total": total,
                    }
                )
                bullet_result = (
                    BulletGenerator.generate(
                        profile,
                        highlight_result,
                    )
                )


            else:


                bullet_result = {}



            profile[
                "bullet_result"
            ] = (
                ModelProtection.protect_result(
                    bullet_result,
                    models,
                )
                if enable_bullet
                else {}
            )



            # =====================
            # Description
            # =====================


            if enable_description:
                save_status(
                    task_id,
                    {
                        "status": "processing",
                        "message": f"第 {index+1}/{total} 个产品：生成详情",
                        "completed": index,
                        "total": total,
                    }
                )

                description_result = (
                    DescriptionGenerator.generate(
                        profile,
                        highlight_result,
                    )
                )


            else:


                description_result = {}



            profile[
                "description_result"
            ] = (
                ModelProtection.protect_result(
                    description_result,
                    models,
                )
                if enable_description
                else {}
            )



            profile[
                "performance"
            ] = timing



            profiles.append(
                profile
            )


            save_profiles(
                task_id,
                profiles
            )


            save_status(
                task_id,
                {
                    "task_id": task_id,
                    "status": "running",
                    "message": f"已完成第 {index + 1}/{total} 个产品",
                    "completed": len(profiles),
                    "total": total,
                }
            )


            success += 1
        except Exception as exc:


            failed += 1


            failed_items.append(
                {
                    "index": index,
                    "source_row_index": getattr(record, "row_number", None),
                    "sku": getattr(record, "sku", ""),
                    "title": getattr(record, "title", ""),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
            )


            save_failed_items(
                task_id,
                failed_items
            )


            print(
                f"{record.sku} failed:",
                exc
            )



        # =====================
        # 更新任务状态
        # =====================


        save_status(
            task_id,
            {
                "task_id": task_id,
                "status": "running",
                "message": f"第 {index + 1}/{total} 个产品处理结束",
                "total": total,
                "completed": index + 1,
                "success": success,
                "failed": failed,
            }
        )



    # =====================
    # 全部完成保存
    # =====================


    save_profiles(
        task_id,
        profiles
    )


    save_failed_items(
        task_id,
        failed_items
    )


    # Final invariant: a completed task must account for every input record.
    # If an item was somehow skipped, persist it as an explicit failure rather
    # than allowing a misleading 49/50 completed task.
    reconciliation = reconcile_task_results(
        task_id,
        records,
        unresolved_error="商品未产生成功或失败结果，已由任务闭环检查记录为失败",
    )


    save_status(
        task_id,
        {
            "task_id": task_id,
            "status": "completed",
            "message": "任务完成",
            "total": total,
            "completed": reconciliation["completed"],
            "success": reconciliation["success"],
            "failed": reconciliation["failed"],
        }
    )


    return reconciliation["profiles"]
