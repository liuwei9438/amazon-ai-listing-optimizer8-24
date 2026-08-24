
from analyzer.seo_intent_engine import generate_primary_search


def run_test(name, profile, expected):
    result = generate_primary_search(profile)
    actual = result.get("primary_search", [])

    print(name)
    print("Result:", actual)

    if actual != expected:
        raise AssertionError(
            f"{name} failed\nExpected: {expected}\nActual: {actual}"
        )

    print("PASS\n")


def main():
    run_test(
        "LG Washing Machine Button",
        {
            "product_type": "Washing Machine Part",
            "attributes": {
                "functions": [
                    "Start Button Power Drive Button"
                ]
            },
        },
        [
            "washing machine start button power drive button"
        ],
    )

    run_test(
        "Dyson Shaver Head",
        {
            "product_type": "Shaver Part",
            "attributes": {
                "functions": [
                    "Replacement Head"
                ]
            },
        },
        [
            "electric shaver replacement head"
        ],
    )

    run_test(
        "Epson Print Head",
        {
            "product_type": "Printer Part",
            "attributes": {
                "functions": [
                    "Print Head"
                ]
            },
        },
        [
            "printer print head"
        ],
    )


if __name__ == "__main__":
    main()
