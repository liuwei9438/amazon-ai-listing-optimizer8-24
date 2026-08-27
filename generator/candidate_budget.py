from __future__ import annotations


class CandidateBudgetEngine:
    """
    Candidate Budget Engine V1

    职责：
    只负责候选文本的字符预算选择。

    不负责：
    - 理解产品
    - 判断候选价值
    - 修改事实
    - 创建缩写
    - 重写文本
    - 调整候选顺序

    Strategy 决定：
        text
        short_text
        priority
        required
        candidate ordering

    Budget Engine 只决定：
        full text 是否放得下
        ↓
        如果放不下，short_text 是否放得下
        ↓
        返回结果
    """

    @staticmethod
    def normalize_text(
        value,
    ) -> str:
        """
        仅做结构级文本清理。

        不改变：
        - 大小写
        - 型号
        - 数字
        - 连字符
        - 技术规格
        """

        if value is None:
            return ""

        return " ".join(
            str(value)
            .strip()
            .split()
        )
    @staticmethod
    def compress_identity(
        text,
        max_length=75,
    ) -> str:
        """
        Identity 最终安全兜底。

        只用于：
        required=True 的 IDENTITY。

        不改变事实，
        不重新生成文本。

        只按完整单词截断。
        """

        text = (
            CandidateBudgetEngine
            .normalize_text(
                text
            )
        )


        if not text:
            return ""


        if len(text) <= max_length:
            return text


        words = text.split()

        result = []

        length = 0


        for word in words:

            next_length = (
                length
                +
                len(word)
                +
                1
            )


            if next_length > max_length:
                break


            result.append(
                word
            )

            length = next_length


        return " ".join(
            result
        ).strip()

    @staticmethod
    def build_title(
        parts,
    ) -> str:
        """
        根据已有标题 parts 构建当前标题。
        """

        if not isinstance(
            parts,
            list,
        ):
            return ""

        cleaned = []

        for part in parts:

            text = (
                CandidateBudgetEngine
                .normalize_text(
                    part
                )
            )

            if text:
                cleaned.append(
                    text
                )

        return " ".join(
            cleaned
        )


    @staticmethod
    def calculate_length(
        parts,
        new_text="",
    ) -> int:
        """
        计算加入 new_text 后的标题长度。
        """

        working_parts = []

        if isinstance(
            parts,
            list,
        ):

            working_parts.extend(
                parts
            )


        new_text = (
            CandidateBudgetEngine
            .normalize_text(
                new_text
            )
        )


        if new_text:

            working_parts.append(
                new_text
            )


        title = (
            CandidateBudgetEngine
            .build_title(
                working_parts
            )
        )


        return len(
            title
        )


    @staticmethod
    def fits(
        parts,
        text,
        max_length=75,
    ) -> bool:
        """
        判断 text 加入以后是否仍在预算范围内。
        """

        text = (
            CandidateBudgetEngine
            .normalize_text(
                text
            )
        )


        if not text:

            return False


        return (
            CandidateBudgetEngine
            .calculate_length(
                parts,
                text,
            )
            <=
            max_length
        )


    @staticmethod
    def is_exact_duplicate(
        parts,
        text,
    ) -> bool:
        """
        只做完全文本重复检测。

        不做语义推断。
        """

        text = (
            CandidateBudgetEngine
            .normalize_text(
                text
            )
        )


        if not text:

            return True


        key = text.casefold()


        if not isinstance(
            parts,
            list,
        ):

            return False


        for part in parts:

            existing = (
                CandidateBudgetEngine
                .normalize_text(
                    part
                )
            )


            if (
                existing
                and
                existing.casefold()
                ==
                key
            ):

                return True


        return False


    @staticmethod
    def choose_candidate_text(
        parts,
        candidate,
        max_length=75,
    ) -> dict:
        """
        在 full text 和 short_text 之间做预算选择。

        顺序固定：

        1. 尝试 text
        2. text 放不下时尝试 short_text
        3. 两者都放不下则 rejected

        注意：
        这里不会创建 short_text。
        short_text 必须由 Strategy 提供。
        """

        if not isinstance(
            candidate,
            dict,
        ):

            return {
                "accepted":
                    False,

                "selected_text":
                    "",

                "source":
                    "",

                "reason":
                    "invalid_candidate",

                "character_count_after":
                    CandidateBudgetEngine
                    .calculate_length(
                        parts
                    ),
            }


        text = (
            CandidateBudgetEngine
            .normalize_text(
                candidate.get(
                    "text",
                    "",
                )
            )
        )


        short_text = (
            CandidateBudgetEngine
            .normalize_text(
                candidate.get(
                    "short_text",
                    "",
                )
            )
        )


        current_length = (
            CandidateBudgetEngine
            .calculate_length(
                parts
            )
        )


        if not text:

            return {
                "accepted":
                    False,

                "selected_text":
                    "",

                "source":
                    "",

                "reason":
                    "empty_text",

                "character_count_after":
                    current_length,
            }


        # =============================================
        # 精确重复
        # =============================================

        if (
            CandidateBudgetEngine
            .is_exact_duplicate(
                parts,
                text,
            )
        ):

            return {
                "accepted":
                    False,

                "selected_text":
                    "",

                "source":
                    "",

                "reason":
                    "exact_duplicate",

                "character_count_after":
                    current_length,
            }


        # =============================================
        # 第一选择：完整 text
        # =============================================

        if (
            CandidateBudgetEngine
            .fits(
                parts,
                text,
                max_length=max_length,
            )
        ):

            return {
                "accepted":
                    True,

                "selected_text":
                    text,

                "source":
                    "text",

                "reason":
                    "accepted_full_text",

                "character_count_after":
                    CandidateBudgetEngine
                    .calculate_length(
                        parts,
                        text,
                    ),
            }


        # =============================================
        # 第二选择：short_text
        # =============================================

        if short_text:

            # short_text 与 text 完全一样，
            # 没有再次测试的意义。
            if (
                short_text.casefold()
                !=
                text.casefold()
            ):

                # short_text 本身不能与已有标题重复
                if not (
                    CandidateBudgetEngine
                    .is_exact_duplicate(
                        parts,
                        short_text,
                    )
                ):

                    if (
                        CandidateBudgetEngine
                        .fits(
                            parts,
                            short_text,
                            max_length=max_length,
                        )
                    ):

                        return {
                            "accepted":
                                True,

                            "selected_text":
                                short_text,

                            "source":
                                "short_text",

                            "reason":
                                "accepted_short_text",

                            "character_count_after":
                                CandidateBudgetEngine
                                .calculate_length(
                                    parts,
                                    short_text,
                                ),
                        }
        # =============================================
        # 第三选择：
        #
        # Required Identity 安全兜底
        #
        # 防止：
        #
        # IDENTITY text >75
        # short_text为空
        #
        # 导致整个标题为空。
        #
        # 这里只允许 Identity 使用。
        # =============================================


        candidate_type = str(
            candidate.get(
                "type",
                "",
            )
            or
            ""
        ).upper()


        required = candidate.get(
            "required",
            False,
        )


        if (
            candidate_type
            ==
            "IDENTITY"

            and

            required
            is True
        ):

            fallback_text = (
                CandidateBudgetEngine
                .compress_identity(
                    text,
                    max_length,
                )
            )


            if fallback_text:

                if (
                    CandidateBudgetEngine
                    .fits(
                        parts,
                        fallback_text,
                        max_length=max_length,
                    )
                ):

                    return {

                        "accepted":
                            True,


                        "selected_text":
                            fallback_text,


                        "source":
                            "identity_fallback",


                        "reason":
                            "accepted_identity_fallback",


                        "character_count_after":
                            CandidateBudgetEngine
                            .calculate_length(
                                parts,
                                fallback_text,
                            ),
                    }

        # =============================================
        # 两个版本都放不下
        # =============================================

        return {
            "accepted":
                False,

            "selected_text":
                "",

            "source":
                "",

            "reason":
                "character_budget",

            "current_length":
                current_length,

            "text_length":
                len(text),

            "short_text_length":
                (
                    len(short_text)
                    if short_text
                    else 0
                ),

            "character_count_after":
                current_length,
        }
