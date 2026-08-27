from __future__ import annotations

import re
from typing import Any, Dict, List


class ContentRankingError(Exception):
    """
    Content Ranking 构建失败。
    """
    pass


class ContentRankingEngine:
    """
    V2.5 Content Ranking Engine V1.0

    职责：
    1. 读取 AI Title Strategy
    2. 将 AI 已经完成的价值判断标准化
    3. 建立统一 ranked_items
    4. 计算字符成本
    5. 做通用语义去重
    6. 不重新判断具体产品的重要性

    核心原则：

    AI 决定：
        什么重要

    Ranking Engine 决定：
        如何把 AI 的判断变成稳定数据结构

    Generator 决定：
        在字符预算内如何执行

    本模块禁止加入任何针对具体产品的硬编码规则。
    """


    # =====================================================
    # Tier
    # =====================================================

    TIER_S = "S"
    TIER_A = "A"
    TIER_B = "B"
    TIER_C = "C"
    TIER_D = "D"


    TIER_SCORE = {
        "S": 100,
        "A": 90,
        "B": 70,
        "C": 40,
        "D": 10,
    }


    TIER_ORDER = {
        "S": 0,
        "A": 1,
        "B": 2,
        "C": 3,
        "D": 4,
    }


    # =====================================================
    # Public API
    # =====================================================

    @staticmethod
    def build(
        profile: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        根据 profile 构建统一 Content Ranking。

        当前 V1.0 的主要决策来源：

            profile["title_strategy"]

        Product Knowledge 只用于补充上下文，
        不允许覆盖 AI Title Strategy 已经做出的价值判断。
        """

        if not isinstance(profile, dict):

            raise ContentRankingError(
                "profile must be a dictionary"
            )


        title_strategy = (
            profile.get(
                "title_strategy",
                {}
            )
        )


        if not isinstance(
            title_strategy,
            dict,
        ):

            title_strategy = {}


        product_knowledge = (
            profile.get(
                "product_knowledge",
                {}
            )
        )


        if not isinstance(
            product_knowledge,
            dict,
        ):

            product_knowledge = {}


        ranked_items: List[Dict[str, Any]] = []

        excluded_items: List[Dict[str, Any]] = []


        # =================================================
        # 1. Core Product
        #
        # AI已经判断出的核心产品身份。
        # 永远属于最高优先级 S。
        # =================================================

        core_product = (
            ContentRankingEngine.clean_text(
                title_strategy.get(
                    "core_product",
                    ""
                )
            )
        )


        if core_product:

            ContentRankingEngine.add_candidate(
                ranked_items=ranked_items,
                text=core_product,
                tier=ContentRankingEngine.TIER_S,
                item_type="identity",
                source="title_strategy.core_product",
                source_rank=0,
                reason="core_product",
            )


        # =================================================
        # 2. Must Include
        #
        # AI明确判断应该进入标题的重要信息。
        #
        # 不在这里重新分析具体属性。
        # =================================================

        must_include = (
            ContentRankingEngine.clean_list(
                title_strategy.get(
                    "must_include",
                    []
                )
            )
        )


        for index, text in enumerate(
            must_include
        ):

            ContentRankingEngine.add_candidate(
                ranked_items=ranked_items,
                text=text,
                tier=ContentRankingEngine.TIER_A,
                item_type="must_include",
                source="title_strategy.must_include",
                source_rank=index,
                reason="must_include",
            )


        # =================================================
        # 3. Compatibility Priority
        #
        # 这是 AI Strategy 已经明确提高优先级的信息。
        #
        # 不判断品牌是谁。
        # 不判断产品类别。
        # =================================================

        compatibility_priority = (
            ContentRankingEngine.clean_list(
                title_strategy.get(
                    "compatibility_priority",
                    []
                )
            )
        )


        for index, text in enumerate(
            compatibility_priority
        ):

            ContentRankingEngine.add_candidate(
                ranked_items=ranked_items,
                text=text,
                tier=ContentRankingEngine.TIER_A,
                item_type="compatibility",
                source="title_strategy.compatibility_priority",
                source_rank=index,
                reason="compatibility_priority",
            )


        # =================================================
        # 4. Model Priority
        #
        # AI已经决定这些型号具有标题价值。
        # =================================================

        model_priority = (
            ContentRankingEngine.clean_list(
                title_strategy.get(
                    "model_priority",
                    []
                )
            )
        )


        for index, text in enumerate(
            model_priority
        ):

            ContentRankingEngine.add_candidate(
                ranked_items=ranked_items,
                text=text,
                tier=ContentRankingEngine.TIER_A,
                item_type="identifier",
                source="title_strategy.model_priority",
                source_rank=index,
                reason="model_priority",
            )


        # =================================================
        # 5. Optional Include
        #
        # AI认为有价值，但字符不够时可以舍弃。
        #
        # B级。
        # =================================================

        optional_include = (
            ContentRankingEngine.clean_list(
                title_strategy.get(
                    "optional_include",
                    []
                )
            )
        )


        for index, text in enumerate(
            optional_include
        ):

            ContentRankingEngine.add_candidate(
                ranked_items=ranked_items,
                text=text,
                tier=ContentRankingEngine.TIER_B,
                item_type="optional",
                source="title_strategy.optional_include",
                source_rank=index,
                reason="optional_include",
            )


        # =================================================
        # 6. Exclude
        #
        # AI已经明确判断不应该消耗标题空间。
        #
        # 保存下来供：
        # Title
        # Bullet
        # Description
        # Highlight
        # 等模块参考。
        #
        # 不进入正常候选池。
        # =================================================

        exclude = (
            ContentRankingEngine.clean_list(
                title_strategy.get(
                    "exclude",
                    []
                )
            )
        )


        for index, text in enumerate(
            exclude
        ):

            excluded_items.append(
                ContentRankingEngine.create_item(
                    text=text,
                    tier=ContentRankingEngine.TIER_D,
                    item_type="excluded",
                    source="title_strategy.exclude",
                    source_rank=index,
                    reason="excluded_by_strategy",
                    allowed_for_title=False,
                )
            )


        # =================================================
        # 7. 通用去重
        #
        # 只删除真正重复的信息。
        #
        # 例如：
        #
        # core:
        # Washing Machine Start Button
        #
        # must:
        # Start Button
        #
        # 后者已经完全包含在前者中。
        #
        # 这里不是产品规则，
        # 是通用文本去重。
        # =================================================

        ranked_items = (
            ContentRankingEngine.deduplicate_items(
                ranked_items
            )
        )


        # =================================================
        # 8. 排序
        #
        # 首先 Tier
        # 然后保持 AI 原来的列表顺序。
        # =================================================

        ranked_items.sort(
            key=lambda item: (
                ContentRankingEngine.TIER_ORDER.get(
                    item.get(
                        "tier",
                        "D"
                    ),
                    99,
                ),
                item.get(
                    "source_priority",
                    99,
                ),
                item.get(
                    "source_rank",
                    999,
                ),
            )
        )


        # =================================================
        # 9. 给最终 Ranking 编号
        # =================================================

        for index, item in enumerate(
            ranked_items
        ):

            item[
                "rank"
            ] = index + 1


        # =================================================
        # 10. Metadata
        # =================================================

        tier_counts = {
            "S": 0,
            "A": 0,
            "B": 0,
            "C": 0,
            "D": 0,
        }


        for item in ranked_items:

            tier = item.get(
                "tier",
                "D"
            )

            if tier in tier_counts:

                tier_counts[tier] += 1


        return {

            "schema_version":
                "1.0",

            "ranking":
                ranked_items,

            "excluded":
                excluded_items,

            "tier_counts":
                tier_counts,

            "total_candidates":
                len(ranked_items),

            "title_strategy_available":
                bool(title_strategy),

            "strategy_reasoning":
                ContentRankingEngine.clean_text(
                    title_strategy.get(
                        "reasoning",
                        ""
                    )
                ),

            "title_length_strategy":
                ContentRankingEngine.clean_text(
                    title_strategy.get(
                        "title_length_strategy",
                        ""
                    )
                ),

            "priority_order":
                ContentRankingEngine.clean_list(
                    title_strategy.get(
                        "priority_order",
                        []
                    )
                ),

            "title_structure":
                ContentRankingEngine.clean_list(
                    title_strategy.get(
                        "title_structure",
                        []
                    )
                ),

        }


    # =====================================================
    # Candidate
    # =====================================================

    @staticmethod
    def add_candidate(
        ranked_items: List[Dict[str, Any]],
        text: str,
        tier: str,
        item_type: str,
        source: str,
        source_rank: int,
        reason: str,
    ) -> None:

        text = (
            ContentRankingEngine.clean_text(
                text
            )
        )


        if not text:

            return


        source_priority = (
            ContentRankingEngine.get_source_priority(
                source
            )
        )


        item = (
            ContentRankingEngine.create_item(
                text=text,
                tier=tier,
                item_type=item_type,
                source=source,
                source_rank=source_rank,
                source_priority=source_priority,
                reason=reason,
                allowed_for_title=True,
            )
        )


        ranked_items.append(
            item
        )


    # =====================================================
    # Create Item
    # =====================================================

    @staticmethod
    def create_item(
        text: str,
        tier: str,
        item_type: str,
        source: str,
        source_rank: int,
        reason: str,
        allowed_for_title: bool,
        source_priority: int = 99,
    ) -> Dict[str, Any]:

        clean_text = (
            ContentRankingEngine.clean_text(
                text
            )
        )


        return {

            "id":
                ContentRankingEngine.make_id(
                    clean_text
                ),

            "text":
                clean_text,

            "type":
                item_type,

            "tier":
                tier,

            "score":
                ContentRankingEngine.TIER_SCORE.get(
                    tier,
                    0,
                ),

            # 当前文本本身字符数
            "cost":
                len(clean_text),

            # 真正加入已有标题时，
            # 通常还需要一个空格。
            "join_cost":
                len(clean_text) + 1,

            "source":
                source,

            "source_priority":
                source_priority,

            "source_rank":
                source_rank,

            "reason":
                reason,

            "allowed_for_title":
                allowed_for_title,

        }


    # =====================================================
    # Source Priority
    #
    # 这不是产品价值判断。
    #
    # 只是当 Tier 相同时，
    # 决定不同 Strategy 字段的稳定排列。
    # =====================================================

    @staticmethod
    def get_source_priority(
        source: str,
    ) -> int:

        priorities = {

            "title_strategy.core_product":
                0,

            "title_strategy.must_include":
                10,

            "title_strategy.compatibility_priority":
                20,

            "title_strategy.model_priority":
                30,

            "title_strategy.optional_include":
                40,

            "title_strategy.exclude":
                90,

        }


        return priorities.get(
            source,
            99,
        )


    # =====================================================
    # Deduplicate
    # =====================================================

    @staticmethod
    def deduplicate_items(
        items: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        通用语义去重。

        先按照价值顺序检查，
        保留更高优先级内容。

        只处理明显重复，
        不做激进语义推断。
        """

        ordered_items = sorted(
            items,
            key=lambda item: (
                ContentRankingEngine.TIER_ORDER.get(
                    item.get(
                        "tier",
                        "D"
                    ),
                    99,
                ),
                item.get(
                    "source_priority",
                    99,
                ),
                item.get(
                    "source_rank",
                    999,
                ),
            )
        )


        result: List[
            Dict[str, Any]
        ] = []


        for item in ordered_items:

            text = (
                ContentRankingEngine.clean_text(
                    item.get(
                        "text",
                        ""
                    )
                )
            )


            if not text:

                continue


            duplicate = False


            for existing in result:

                existing_text = (
                    ContentRankingEngine.clean_text(
                        existing.get(
                            "text",
                            ""
                        )
                    )
                )


                if (
                    ContentRankingEngine.is_duplicate_text(
                        text,
                        existing_text,
                    )
                ):

                    duplicate = True

                    break


            if not duplicate:

                result.append(
                    item
                )


        return result


    # =====================================================
    # Duplicate Text
    # =====================================================

    @staticmethod
    def is_duplicate_text(
        new_text: str,
        existing_text: str,
    ) -> bool:

        new_normalized = (
            ContentRankingEngine.normalize_text(
                new_text
            )
        )


        existing_normalized = (
            ContentRankingEngine.normalize_text(
                existing_text
            )
        )


        if (
            not new_normalized
            or
            not existing_normalized
        ):

            return False


        # 完全相同
        if (
            new_normalized
            ==
            existing_normalized
        ):

            return True


        new_words = (
            ContentRankingEngine.extract_words(
                new_text
            )
        )


        existing_words = (
            ContentRankingEngine.extract_words(
                existing_text
            )
        )


        if (
            not new_words
            or
            not existing_words
        ):

            return False


        # 新信息完全包含于已有高优先级信息
        #
        # 例如：
        #
        # Existing:
        # Washing Machine Start Button
        #
        # New:
        # Start Button
        if (
            new_words
            <=
            existing_words
        ):

            return True


        return False


    # =====================================================
    # Normalize
    # =====================================================

    @staticmethod
    def normalize_text(
        value: Any,
    ) -> str:

        text = (
            ContentRankingEngine.clean_text(
                value
            )
            .lower()
        )


        text = re.sub(
            r"[^a-z0-9]+",
            " ",
            text,
        )


        text = re.sub(
            r"\s+",
            " ",
            text,
        )


        return text.strip()


    # =====================================================
    # Extract Words
    # =====================================================

    @staticmethod
    def extract_words(
        value: Any,
    ) -> set[str]:

        normalized = (
            ContentRankingEngine.normalize_text(
                value
            )
        )


        if not normalized:

            return set()


        return {
            word
            for word in normalized.split()
            if word
        }


    # =====================================================
    # Clean Text
    # =====================================================

    @staticmethod
    def clean_text(
        value: Any,
    ) -> str:

        if value is None:

            return ""


        text = str(
            value
        ).strip()


        if not text:

            return ""


        text = re.sub(
            r"\s+",
            " ",
            text,
        )


        return text.strip()


    # =====================================================
    # Clean List
    # =====================================================

    @staticmethod
    def clean_list(
        value: Any,
    ) -> List[str]:

        if value is None:

            return []


        if isinstance(
            value,
            str,
        ):

            value = [
                value
            ]


        if not isinstance(
            value,
            (
                list,
                tuple,
                set,
            ),
        ):

            return []


        result: List[str] = []


        for item in value:

            text = (
                ContentRankingEngine.clean_text(
                    item
                )
            )


            if not text:

                continue


            if text not in result:

                result.append(
                    text
                )


        return result


    # =====================================================
    # Stable ID
    # =====================================================

    @staticmethod
    def make_id(
        text: str,
    ) -> str:

        normalized = (
            ContentRankingEngine.normalize_text(
                text
            )
        )


        if not normalized:

            return ""


        identifier = (
            normalized
            .replace(
                " ",
                "_",
            )
        )


        return identifier[:80]
