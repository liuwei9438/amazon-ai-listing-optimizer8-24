from __future__ import annotations

import json
from pathlib import Path

from core.content_ranking_engine import ContentRankingEngine


def main():

    # =====================================================
    # 测试文件路径
    #
    # 默认读取项目根目录下：
    # product_profiles_v2.4.3 (6).json
    #
    # 如果你的 JSON 不在根目录，
    # 修改这里即可。
    # =====================================================

    project_root = Path(__file__).resolve().parents[1]

    json_path = (
        project_root
        / "product_profiles_v2.4.3 (6).json"
    )


    if not json_path.exists():

        print("=" * 70)
        print("ERROR")
        print("=" * 70)

        print(
            f"找不到测试文件：\n{json_path}"
        )

        print()
        print(
            "请把 product_profiles_v2.4.3 (6).json "
            "放到项目根目录。"
        )

        return


    # =====================================================
    # 读取 Profile JSON
    # =====================================================

    with json_path.open(
        "r",
        encoding="utf-8",
    ) as f:

        profiles = json.load(f)


    if not isinstance(
        profiles,
        list,
    ):

        print(
            "ERROR: JSON 顶层必须是 list"
        )

        return


    print()
    print("=" * 80)
    print("CONTENT RANKING ENGINE TEST")
    print("=" * 80)

    print(
        f"Profiles: {len(profiles)}"
    )


    # =====================================================
    # 逐个测试
    # =====================================================

    for index, profile in enumerate(
        profiles,
        start=1,
    ):

        print()
        print()
        print("=" * 80)
        print(
            f"PRODUCT {index}"
        )
        print("=" * 80)


        source_identity = (
            profile.get(
                "source_identity",
                {}
            )
        )


        sku = source_identity.get(
            "sku",
            ""
        )


        product_identity = (
            profile.get(
                "product_identity",
                {}
            )
        )


        product_name = (
            product_identity.get(
                "title_product_identity",
                ""
            )
            or
            product_identity.get(
                "name",
                ""
            )
        )


        print(
            f"SKU: {sku}"
        )

        print(
            f"Product: {product_name}"
        )


        # =================================================
        # 检查 Strategy
        # =================================================

        title_strategy = (
            profile.get(
                "title_strategy",
                {}
            )
        )


        if not title_strategy:

            print()
            print(
                "SKIP: profile 没有 title_strategy"
            )

            continue


        print()
        print("-" * 80)
        print("TITLE STRATEGY")
        print("-" * 80)


        print(
            json.dumps(
                title_strategy,
                ensure_ascii=False,
                indent=2,
            )
        )


        # =================================================
        # Build Ranking
        # =================================================

        try:

            ranking_result = (
                ContentRankingEngine.build(
                    profile
                )
            )


        except Exception as exc:

            print()
            print(
                "CONTENT RANKING FAILED:"
            )

            print(
                str(exc)
            )

            continue


        # =================================================
        # 输出 Ranking
        # =================================================

        print()
        print("-" * 80)
        print("CONTENT RANKING")
        print("-" * 80)


        print(
            json.dumps(
                ranking_result,
                ensure_ascii=False,
                indent=2,
            )
        )


        # =================================================
        # 简化输出
        # =================================================

        print()
        print("-" * 80)
        print("RANKING SUMMARY")
        print("-" * 80)


        ranking = (
            ranking_result.get(
                "ranking",
                []
            )
        )


        for item in ranking:

            rank = item.get(
                "rank",
                ""
            )

            tier = item.get(
                "tier",
                ""
            )

            score = item.get(
                "score",
                ""
            )

            cost = item.get(
                "cost",
                ""
            )

            item_type = item.get(
                "type",
                ""
            )

            text = item.get(
                "text",
                ""
            )


            print(
                f"{rank:>2}. "
                f"[{tier}] "
                f"score={score:<3} "
                f"cost={cost:<2} "
                f"type={item_type:<15} "
                f"{text}"
            )


    print()
    print()
    print("=" * 80)
    print("TEST FINISHED")
    print("=" * 80)


if __name__ == "__main__":

    main()
