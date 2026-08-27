from __future__ import annotations

from typing import Any

from services import (
    OpenAIResponsesClient,
    AIClientError,
)

from .fact_lock import (
    build_fact_lock,
    validate_fact_lock,
)

from .product_profile_schema import (
    empty_profile,
    json_schema,
)

from .profile_validator import (
    normalize_profile,
    validate_profile,
)

from .understanding_prompt import (
    SYSTEM_PROMPT,
    build_user_prompt,
)

from .attribute_engine import (
    extract_basic_attributes,
)

from .attribute_validator import (
    validate_attributes,
    validate_compatibility,
)

from .identifier_classifier import (
    IdentifierClassifier,
    IdentifierClassificationError,
)

from core.source_fact_extractor import (
    SourceFactExtractor,
)

from core.source_fact_reconciler import (
    SourceFactReconciler,
)



class UnderstandingError(
    RuntimeError
):
    pass





def _clean_brand_text(value: Any) -> str:
    if value is None:
        return ""
    return __import__("re").sub(
        r"\s+",
        " ",
        str(value),
    ).strip()


def _strip_exact_brand_phrase(
    text: Any,
    brand: str,
) -> str:
    """
    Remove a known seller/store brand as a standalone phrase.

    The phrase is removed case-insensitively and separators are cleaned.
    We do not perform fuzzy removal because that could damage legitimate
    model numbers or compatibility brands.
    """

    source = _clean_brand_text(text)
    brand = _clean_brand_text(brand)

    if not source or not brand:
        return source

    pattern = (
        r"(?<![A-Za-z0-9])"
        +
        __import__("re").escape(brand)
        +
        r"(?![A-Za-z0-9])"
    )

    cleaned = __import__("re").sub(
        pattern,
        " ",
        source,
        flags=__import__("re").IGNORECASE,
    )

    cleaned = __import__("re").sub(
        r"\s+",
        " ",
        cleaned,
    ).strip(" -–—|,;/:")

    return cleaned


def _remove_brand_from_string_list(
    values: Any,
    brand: str,
) -> list[str]:

    if isinstance(values, list):
        source_values = values
    elif isinstance(values, (tuple, set)):
        source_values = list(values)
    elif values:
        source_values = [values]
    else:
        source_values = []

    result: list[str] = []
    seen: set[str] = set()

    for value in source_values:
        cleaned = _strip_exact_brand_phrase(
            value,
            brand,
        )

        key = cleaned.casefold()

        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)

    return result


def sanitize_seller_brand_contamination(
    profile: dict[str, Any],
) -> dict[str, Any]:
    """
    Deterministic seller/store-brand firewall.

    Once AI has explicitly identified brand_info.seller_brand, that brand is
    metadata only. It must not leak into compatibility or generated content.

    This does NOT guess seller brands. It only acts on an explicitly detected
    seller_brand, keeping false-positive risk low.
    """

    if not isinstance(profile, dict):
        return profile

    brand_info = profile.get(
        "brand_info",
        {},
    )

    if not isinstance(brand_info, dict):
        return profile

    seller_brand = _clean_brand_text(
        brand_info.get(
            "seller_brand",
            "",
        )
    )

    if not seller_brand:
        return profile

    # -----------------------------------------------------
    # Brand arrays: seller brand cannot become compatibility
    # -----------------------------------------------------
    for key in (
        "third_party_brands",
        "detected_brands",
    ):
        brand_info[key] = [
            brand
            for brand in (
                brand_info.get(
                    key,
                    [],
                )
                if isinstance(
                    brand_info.get(
                        key,
                        [],
                    ),
                    list,
                )
                else []
            )
            if (
                _clean_brand_text(brand).casefold()
                !=
                seller_brand.casefold()
            )
        ]

    compatibility = profile.setdefault(
        "compatibility",
        {},
    )

    if isinstance(compatibility, dict):
        compatibility["brands"] = [
            brand
            for brand in (
                compatibility.get(
                    "brands",
                    [],
                )
                if isinstance(
                    compatibility.get(
                        "brands",
                        [],
                    ),
                    list,
                )
                else []
            )
            if (
                _clean_brand_text(brand).casefold()
                !=
                seller_brand.casefold()
            )
        ]

        compatibility[
            "compatibility_notes"
        ] = _remove_brand_from_string_list(
            compatibility.get(
                "compatibility_notes",
                [],
            ),
            seller_brand,
        )

    # -----------------------------------------------------
    # Identity / source-derived semantic fields
    # -----------------------------------------------------
    product_identity = profile.get(
        "product_identity",
        {},
    )

    if isinstance(product_identity, dict):
        for key in (
            "name",
            "buyer_search_identity",
            "title_product_identity",
            "category",
            "parent_product",
        ):
            if key in product_identity:
                product_identity[key] = (
                    _strip_exact_brand_phrase(
                        product_identity.get(
                            key,
                            "",
                        ),
                        seller_brand,
                    )
                )

        for key in (
            "context",
            "design_features",
            "functional_features",
            "usage_scenarios",
        ):
            product_identity[key] = (
                _remove_brand_from_string_list(
                    product_identity.get(
                        key,
                        [],
                    ),
                    seller_brand,
                )
            )

    basic_info = profile.get(
        "basic_info",
        {},
    )

    if isinstance(basic_info, dict):
        for key in (
            "product_name",
            "product_type",
            "category",
            "main_function",
        ):
            if key in basic_info:
                basic_info[key] = (
                    _strip_exact_brand_phrase(
                        basic_info.get(
                            key,
                            "",
                        ),
                        seller_brand,
                    )
                )

    title_information = profile.get(
        "title_information",
        {},
    )

    if isinstance(title_information, dict):
        for key in (
            "priority_attributes",
            "important_specifications",
            "important_context",
            "important_compatibility",
        ):
            title_information[key] = (
                _remove_brand_from_string_list(
                    title_information.get(
                        key,
                        [],
                    ),
                    seller_brand,
                )
            )

    attributes = profile.get(
        "attributes",
        {},
    )

    if isinstance(attributes, dict):
        for key in (
            "functions",
            "usage_scenarios",
            "factual_selling_points",
            "materials",
            "design_features",
            "functional_features",
            "specifications",
            "package_contents",
            "installation",
        ):
            if key in attributes:
                attributes[key] = (
                    _remove_brand_from_string_list(
                        attributes.get(
                            key,
                            [],
                        ),
                        seller_brand,
                    )
                )

    # Keep seller_brand itself for diagnostics/auditing.
    brand_info[
        "seller_brand_filtered"
    ] = True

    return profile



class ProductUnderstandingEngine:


    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4.1-mini"
    ):


        self.client = OpenAIResponsesClient(

            api_key=api_key,

            model=model,
            stage="product_understanding",

        )


        self.identifier_classifier = IdentifierClassifier(

            api_key=api_key,

            model=model,

        )



    def analyze(
        self,
        record: Any
    ) -> dict[str, Any]:


        # =================================================
        # Source Fact Preservation
        #
        # Build a deterministic source ledger BEFORE AI understanding.
        # AI may classify facts, but cannot make them disappear.
        # =================================================

        source_fact_ledger = (
            SourceFactExtractor.extract(
                record
            )
        )


        expected_lock = build_fact_lock(
            record
        )


        prompt = build_user_prompt(

            record,

            expected_lock,

            empty_profile(),

            source_fact_ledger=source_fact_ledger,

        )


        try:

            raw = self.client.create_json(

                SYSTEM_PROMPT,

                prompt,

                json_schema(),

            )


        except AIClientError as exc:

            raise UnderstandingError(

                str(exc)

            ) from exc



        profile = normalize_profile(
            raw
        )


        # =================================================
        # Seller / Store Brand Hygiene
        #
        # The source seller's own brand is metadata only.
        # Remove it before identifier, compatibility, knowledge,
        # title, bullet and description stages can consume it.
        # =================================================

        profile = sanitize_seller_brand_contamination(
            profile
        )


        # =================================================
        # Source Fact Ledger + Reconciliation
        # =================================================

        profile[
            "source_fact_ledger"
        ] = source_fact_ledger

        profile[
            "source_fact_audit"
        ] = SourceFactReconciler.reconcile(
            profile,
            source_fact_ledger,
        )



        # =================================================
        # Identifier Classification
        # =================================================

        try:

            profile = self.classify_identifiers(
                profile,
                record,
            )


        except IdentifierClassificationError as exc:

            raise UnderstandingError(
                str(exc)
            ) from exc



        # =================================================
        # Deterministic Attributes
        # =================================================

        extracted_attributes = (
            extract_basic_attributes(
                record
            )
        )


        profile.setdefault(

            "attributes",

            {}

        )


        profile["attributes"].update(

            extracted_attributes

        )


        profile["attributes"] = (

            validate_attributes(

                profile["attributes"]

            )

        )



        # =================================================
        # Compatibility Validation
        # =================================================

        profile.setdefault(

            "compatibility",

            {}

        )


        profile["compatibility"] = (

            validate_compatibility(

                profile["compatibility"]

            )

        )



        # =================================================
        # Final Source Fact Reconciliation
        # =================================================

        profile[
            "source_fact_audit"
        ] = SourceFactReconciler.reconcile(
            profile,
            source_fact_ledger,
        )


        # =================================================
        # Source Identity
        # =================================================

        profile["source_identity"] = {


            "sku":

                getattr(

                    record,

                    "sku",

                    ""

                ),


            "parent_sku":

                getattr(

                    record,

                    "parent_sku",

                    ""

                ),


            "source_row_index":

                getattr(

                    record,

                    "row_number",

                    None

                ),

        }



        # =================================================
        # Fact Lock
        # =================================================

        profile["fact_lock"] = expected_lock



        errors = (

            validate_profile(profile)

            +

            validate_fact_lock(

                profile,

                expected_lock,

            )

        )


        if errors:

            raise UnderstandingError(

                "；".join(errors)

            )



        return profile



    # =====================================================
    # Identifier Classification
    # =====================================================

    def classify_identifiers(
        self,
        profile: dict[str, Any],
        record: Any,
    ) -> dict[str, Any]:


        compatibility = profile.get(

            "compatibility",

            {}

        )


        if not isinstance(

            compatibility,

            dict

        ):

            compatibility = {}



        candidates = []


        candidates.extend(

            compatibility.get(

                "models",

                []

            )

        )


        candidates.extend(

            compatibility.get(

                "part_numbers",

                []

            )

        )



        if not candidates:

            return profile



        product_context = {


            "title":

                getattr(

                    record,

                    "title",

                    ""

                ),


            "product_type":

                profile.get(

                    "basic_info",

                    {}

                ).get(

                    "product_type",

                    ""

                ),


            "main_function":

                profile.get(

                    "basic_info",

                    {}

                ).get(

                    "main_function",

                    ""

                ),


            "compatibility":

                compatibility,

        }



        result = (

            self.identifier_classifier.classify(

                product_context,

                candidates,

            )

        )



        model_values = []

        part_values = []



        for item in result.get(

            "identifier_results",

            []

        ):


            if item.get(

                "type"

            ) == "model_number":


                model_values.append(

                    item.get(

                        "value"

                    )

                )



            elif item.get(

                "type"

            ) == "part_number":


                part_values.append(

                    item.get(

                        "value"

                    )

                )



        # 保留 AI 原始结果中未被否定的信息
        # 这里只更新确认项

        if model_values:

            compatibility["models"] = model_values


        if part_values:

            compatibility["part_numbers"] = part_values



        profile["compatibility"] = compatibility


        return profile
