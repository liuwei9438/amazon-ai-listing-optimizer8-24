from __future__ import annotations

import json
from io import BytesIO
from typing import Any

import pandas as pd


class ListingExporter:
    """
    Amazon AI Listing Optimizer

    Listing Exporter V2.4.4 Row-Mapped Stable

    核心原则：
    1. 原始 Excel 是唯一行结构来源
    2. 不按照 profiles 当前顺序与 Excel 强行拼接
    3. 优先通过 source_identity.source_row_index 定位原始行
    4. SKU 只作为辅助校验，不作为主要定位依据
    5. 一个 Profile 只能写入它自己的原始行
    6. 不重新排序原始数据
    7. 不删除原始行
    8. 未生成的模块保持空白
    """

    AI_COLUMNS = [
        "AI Title",
        "AI Short Title",
        "AI Highlights",
        "AI Short Highlights",
        "AI Bullet Points",
        "AI Description",
    ]

    # =====================================================
    # 通用安全函数
    # =====================================================

    @staticmethod
    def safe_value(value: Any) -> str:
        """
        将 AI 返回值安全转换为 Excel 可写文本。
        """

        if value is None:
            return ""

        if isinstance(value, list):

            values = []

            for item in value:

                if item is None:
                    continue

                if isinstance(item, dict):

                    text = (
                        item.get("text")
                        or item.get("value")
                        or item.get("content")
                        or ""
                    )

                    if text:
                        values.append(str(text))

                else:

                    text = str(item).strip()

                    if text:
                        values.append(text)

            return "\n".join(values)

        if isinstance(value, dict):

            return json.dumps(
                value,
                ensure_ascii=False,
            )

        return str(value)


    # =====================================================
    # 安全获取嵌套字段
    # =====================================================

    @staticmethod
    def get_dict(
        value: Any,
    ) -> dict:

        if isinstance(
            value,
            dict,
        ):
            return value

        return {}


    @staticmethod
    def get_list(
        value: Any,
    ) -> list:

        if isinstance(
            value,
            list,
        ):
            return value

        return []


    # =====================================================
    # 获取生成结果
    # =====================================================

    @classmethod
    def get_generated(
        cls,
        profile: dict,
    ) -> dict:

        if not isinstance(
            profile,
            dict,
        ):
            return {
                column: ""
                for column in cls.AI_COLUMNS
            }


        # =================================================
        # Title
        # =================================================

        generated_title = cls.get_dict(
            profile.get(
                "generated_title"
            )
        )

        title = (
            generated_title
            .get(
                "title",
                "",
            )
        )


        # =================================================
        # Short Title
        # =================================================

        short_title_result = cls.get_dict(
            profile.get(
                "short_title_result"
            )
        )

        short_title = (
            short_title_result.get(
                "short_title",
                "",
            )
            or
            short_title_result.get(
                "title",
                "",
            )
            or
            short_title_result.get(
                "text",
                "",
            )
        )


        # =================================================
        # Highlight
        # =================================================

        highlight_result = profile.get(
            "highlight_result",
            {},
        )

        highlights = []
        short_highlights = []


        if isinstance(
            highlight_result,
            dict,
        ):

            highlights = (
                highlight_result.get(
                    "highlights",
                    [],
                )
                or
                highlight_result.get(
                    "highlight",
                    [],
                )
                or
                []
            )

            short_highlights = (
                highlight_result.get(
                    "short_highlights",
                    [],
                )
                or
                []
            )


        elif isinstance(
            highlight_result,
            list,
        ):

            highlights = highlight_result

            short_highlights = (
                highlight_result[:3]
            )


        # =================================================
        # Bullet Points
        # =================================================

        bullet_result = cls.get_dict(
            profile.get(
                "bullet_result"
            )
        )

        bullets = (
            bullet_result.get(
                "bullets",
                [],
            )
            or
            bullet_result.get(
                "bullet_points",
                [],
            )
            or
            bullet_result.get(
                "points",
                [],
            )
            or
            []
        )


        # =================================================
        # Description
        # =================================================

        description_result = cls.get_dict(
            profile.get(
                "description_result"
            )
        )

        description = (
            description_result.get(
                "description",
                "",
            )
            or
            description_result.get(
                "text",
                "",
            )
            or
            description_result.get(
                "content",
                "",
            )
        )


        return {

            "AI Title":
                cls.safe_value(
                    title
                ),

            "AI Short Title":
                cls.safe_value(
                    short_title
                ),

            "AI Highlights":
                cls.safe_value(
                    highlights
                ),

            "AI Short Highlights":
                cls.safe_value(
                    short_highlights
                ),

            "AI Bullet Points":
                cls.safe_value(
                    bullets
                ),

            "AI Description":
                cls.safe_value(
                    description
                ),

        }


    # =====================================================
    # 获取 Profile 原始身份
    # =====================================================

    @staticmethod
    def get_source_identity(
        profile: dict,
    ) -> dict:

        if not isinstance(
            profile,
            dict,
        ):
            return {}

        source_identity = profile.get(
            "source_identity",
            {},
        )

        if not isinstance(
            source_identity,
            dict,
        ):
            return {}

        return source_identity


    # =====================================================
    # 获取 source_row_index
    # =====================================================

    @classmethod
    def get_source_row_index(
        cls,
        profile: dict,
    ):

        source_identity = (
            cls.get_source_identity(
                profile
            )
        )

        row_index = source_identity.get(
            "source_row_index"
        )


        if row_index is None:

            return None


        try:

            return int(
                row_index
            )

        except (
            TypeError,
            ValueError,
        ):

            return None


    # =====================================================
    # 获取 SKU
    # =====================================================

    @classmethod
    def find_sku(
        cls,
        profile: dict,
    ) -> str:

        if not isinstance(
            profile,
            dict,
        ):
            return ""


        source_identity = (
            cls.get_source_identity(
                profile
            )
        )


        sku = (
            source_identity.get(
                "sku",
                "",
            )
            or
            profile.get(
                "sku",
                "",
            )
            or
            cls.get_dict(
                profile.get(
                    "product"
                )
            ).get(
                "sku",
                "",
            )
        )


        if sku is None:
            return ""


        return str(
            sku
        ).strip()


    # =====================================================
    # 检测 SKU 列
    # =====================================================

    @staticmethod
    def find_dataframe_sku_column(
        dataframe: pd.DataFrame,
    ):

        candidates = [

            "SKU",
            "sku",
            "Sku",

            "Seller SKU",
            "seller_sku",

            "商家SKU",
            "商品SKU",
            "产品SKU",
        ]


        for column in candidates:

            if column in dataframe.columns:
                return column


        return None


    # =====================================================
    # 初始化 AI 列
    # =====================================================

    @classmethod
    def ensure_ai_columns(
        cls,
        dataframe: pd.DataFrame,
    ) -> pd.DataFrame:

        df = dataframe.copy()


        for column in cls.AI_COLUMNS:

            if column not in df.columns:

                df[column] = ""

            else:

                # 当前导出应重新根据本次 profiles 写入。
                # 防止上一次测试结果残留。
                df[column] = ""


        return df


    # =====================================================
    # source_row_index 转换为 pandas 行位置
    # =====================================================

    @staticmethod
    def resolve_dataframe_position(
        source_row_index,
        row_count: int,
    ):

        if source_row_index is None:
            return None


        # =================================================
        # 当前系统的 source_row_index
        #
        # 从现有 Product Profile 可以看到：
        # 第一条 Excel 数据行为 source_row_index = 2。
        #
        # 这是 Excel 实际行号：
        #
        # Excel Row 1 = 表头
        # Excel Row 2 = 第一条数据
        #
        # pandas iloc:
        #
        # iloc[0] = 第一条数据
        #
        # 所以：
        #
        # pandas_position = source_row_index - 2
        # =================================================

        position = (
            source_row_index
            -
            2
        )


        if (
            position >= 0
            and
            position < row_count
        ):

            return position


        return None


    # =====================================================
    # SKU 辅助定位
    # =====================================================

    @staticmethod
    def locate_by_sku(
        dataframe: pd.DataFrame,
        sku_column,
        sku: str,
    ):

        if not sku_column:
            return None


        if not sku:
            return None


        matches = []


        for position in range(
            len(
                dataframe
            )
        ):

            value = dataframe.iloc[
                position
            ][
                sku_column
            ]


            if value is None:
                continue


            if str(
                value
            ).strip() == sku:

                matches.append(
                    position
                )


        # SKU 只有唯一匹配时才允许使用。
        #
        # 如果 SKU 重复，
        # 不能随便猜是哪一行。

        if len(
            matches
        ) == 1:

            return matches[0]


        return None


    # =====================================================
    # 定位 Profile 对应的 Excel 行
    # =====================================================

    @classmethod
    def locate_profile_row(
        cls,
        dataframe: pd.DataFrame,
        profile: dict,
        sku_column,
    ):

        source_row_index = (
            cls.get_source_row_index(
                profile
            )
        )


        # =================================================
        # 第一优先级：
        # source_row_index
        # =================================================

        position = (
            cls.resolve_dataframe_position(
                source_row_index,
                len(
                    dataframe
                ),
            )
        )


        if position is not None:

            return position


        # =================================================
        # 第二优先级：
        # 唯一 SKU
        #
        # 只做兼容旧 Profile 的保险。
        # =================================================

        sku = cls.find_sku(
            profile
        )


        return cls.locate_by_sku(
            dataframe,
            sku_column,
            sku,
        )


    # =====================================================
    # 判断 Profile 是否真的生成了内容
    # =====================================================

    @classmethod
    def has_generated_content(
        cls,
        generated: dict,
    ) -> bool:

        for column in cls.AI_COLUMNS:

            value = generated.get(
                column,
                "",
            )

            if (
                value is not None
                and
                str(
                    value
                ).strip()
            ):

                return True


        return False


    # =====================================================
    # 主导出
    # =====================================================

    @classmethod
    def export(
        cls,
        dataframe,
        profiles,
    ):

        if dataframe is None:

            raise ValueError(
                "原始 Excel DataFrame 不存在"
            )


        if not isinstance(
            dataframe,
            pd.DataFrame,
        ):

            raise TypeError(
                "dataframe 必须是 pandas DataFrame"
            )


        if profiles is None:

            profiles = []


        if not isinstance(
            profiles,
            list,
        ):

            raise TypeError(
                "profiles 必须是 list"
            )


        # =================================================
        # 1. 完整复制原 Excel 数据
        # =================================================

        result = (
            cls.ensure_ai_columns(
                dataframe
            )
        )


        sku_column = (
            cls.find_dataframe_sku_column(
                result
            )
        )


        # =================================================
        # 2. 建立写入保护
        #
        # 一个 Excel 行只能被一个 Profile 写入。
        # 防止重复 Profile 覆盖前面的正确结果。
        # =================================================

        written_positions = set()

        successful_profiles = 0

        skipped_profiles = 0

        duplicate_profiles = 0

        unmatched_profiles = []


        # =================================================
        # 3. 遍历 profiles
        #
        # 注意：
        #
        # 这里只遍历 AI 结果。
        #
        # 不创建新 DataFrame。
        # 不 append 原始行。
        # 不 concat。
        # =================================================

        for profile_index, profile in enumerate(
            profiles
        ):

            if not isinstance(
                profile,
                dict,
            ):

                skipped_profiles += 1

                continue


            generated = cls.get_generated(
                profile
            )


            # 没有任何生成内容，
            # 不写入 Excel。

            if not cls.has_generated_content(
                generated
            ):

                skipped_profiles += 1

                continue


            position = cls.locate_profile_row(
                result,
                profile,
                sku_column,
            )


            # 找不到对应原行，
            # 绝不猜测。

            if position is None:

                unmatched_profiles.append(
                    {
                        "profile_index":
                            profile_index,

                        "source_row_index":
                            cls.get_source_row_index(
                                profile
                            ),

                        "sku":
                            cls.find_sku(
                                profile
                            ),
                    }
                )

                continue


            # 同一个 Excel 行如果已经写过，
            # 后面的 Profile 不允许再次覆盖。

            if position in written_positions:

                duplicate_profiles += 1

                continue


            # =================================================
            # 4. 只写 AI 字段
            #
            # 原始 SKU、标题、描述、价格、图片……
            # 全部保持原样。
            # =================================================

            for column in cls.AI_COLUMNS:

                result.at[
                    result.index[
                        position
                    ],
                    column,
                ] = generated.get(
                    column,
                    "",
                )


            written_positions.add(
                position
            )

            successful_profiles += 1


        # =================================================
        # 5. 没有任何结果时直接报错
        # =================================================

        if successful_profiles == 0:

            raise ValueError(
                "没有找到可正确匹配并导出的 AI 优化结果。"
                "请检查 source_identity.source_row_index。"
            )


        # =================================================
        # 6. 最终完整性检查
        # =================================================

        if len(
            result
        ) != len(
            dataframe
        ):

            raise RuntimeError(
                "导出过程中原始 Excel 行数发生变化，"
                "已终止导出。"
            )


        # =================================================
        # 7. 输出 Excel
        # =================================================

        output = BytesIO()


        with pd.ExcelWriter(
            output,
            engine="openpyxl",
        ) as writer:

            result.to_excel(
                writer,
                index=False,
                sheet_name="AI Optimized",
            )


        output.seek(
            0
        )


        return output
