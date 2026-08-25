from __future__ import annotations

import json
import os

from analyzer.title_strategy_generator import (
    TitleStrategyGenerator,
)


PROFILE_FILE = "product_profiles_v2.4.3.json"


def load_profiles():

    with open(
        PROFILE_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        return json.load(f)



def main():

    api_key = os.getenv(
        "OPENAI_API_KEY"
    )

    if not api_key:

        raise RuntimeError(
            "Missing OPENAI_API_KEY"
        )


    profiles = load_profiles()


    generator = TitleStrategyGenerator()


    # 测试第三个产品：9D剃须刀

    profile = profiles[2]


    result = generator.generate(
        profile,
        api_key,
    )


    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        )
    )



if __name__ == "__main__":

    main()
