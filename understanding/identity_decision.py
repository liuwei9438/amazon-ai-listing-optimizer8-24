from __future__ import annotations

import json

from openai import OpenAI

from services.ai_runtime import DEFAULT_TIMEOUT_SECONDS, execute_with_retry


class IdentityDecisionError(Exception):
    pass


class IdentityDecisionEngine:
    """
    Identity Decision Engine V2.0

    职责：
    - 收集现有身份候选
    - 让 AI 做统一身份决策
    - 输出唯一 canonical identity
    - 强制区分产品身份与数量、兼容品牌、型号、零件号、规格等独立信息
    - 输出可解释的 Identity Decision 评分

    不负责：
    - 生成标题
    - 评分 title candidates
    - 兼容关系排序
    - 字符预算
    """

    SYSTEM_PROMPT = """
You are an Amazon product identity decision engine.

Your task is NOT to write a product title.

Your task is to determine the single canonical product identity
that downstream listing systems should consistently use.

The canonical identity must answer only:

"What exactly is the physical product being sold?"

The canonical identity is the single authoritative product identity.
It is NOT the full Amazon title.

=================================================
CORE PRINCIPLE
=================================================

Select the ONE identity that best balances:

1. identity accuracy
2. identity completeness
3. semantic boundary correctness
4. necessary generic context
5. customer search intent
6. product recognition
7. category naming convention
8. character efficiency

Character efficiency is the LAST decision factor.

A shorter identity is NOT better if shortening makes the product
less accurate, less complete, or materially ambiguous.

=================================================
1. IDENTITY ACCURACY
=================================================

The canonical identity must correctly identify
the actual physical product being sold.

Reject or penalize identities that:

- describe only a broad category
- describe the wrong physical object
- describe only part of the sold product
- confuse the product with the machine/device it fits
- use seller-created wording instead of the real product identity

Examples of overly broad concepts include:

- Parts
- Accessories
- Components
- Replacement Parts

when a more precise verified product identity exists.

=================================================
2. IDENTITY COMPLETENESS
=================================================

The canonical identity must contain enough information
for a customer to understand what the product actually is.

Prefer a complete product identity over a shorter but vague identity.

However:

Completeness means complete PRODUCT IDENTITY,
not complete PRODUCT INFORMATION.

Do NOT make an identity "more complete" by absorbing:

- quantity
- compatible brand
- compatible model
- part number
- series number
- dimensions
- material
- color
- voltage
- power
- secondary specifications
- functional features
- marketing wording

Those belong to independent downstream fields.

=================================================
3. IDENTITY BOUNDARY CORRECTNESS
=================================================

The canonical identity should contain only product-identity meaning.

If information can be represented independently later
without changing what the physical product fundamentally is,
that information normally must NOT remain inside canonical_identity.

Independent downstream information includes:

- package quantity
- pack count
- compatibility brand
- compatibility wording
- compatible model
- part number
- series number
- dimensions
- material
- color
- voltage
- power
- technical specifications
- secondary features

Do not consume title identity space with information
that downstream systems already manage separately.

=================================================
4. GENERIC CONTEXT VS COMPATIBILITY
=================================================

Generic device/application context may remain in canonical_identity
ONLY when removing it would make the product materially ambiguous
or incomplete.

Examples of generic identity context:

- Robot Vacuum Cleaner
- Washing Machine
- Refrigerator
- Electric Scooter
- CNC Router
- Sewing Machine

Generic context explains WHAT KIND OF PRODUCT this is.

Specific third-party brands and compatible model numbers
describe WHAT THE PRODUCT FITS.

Those are compatibility information, not product identity.

Therefore:

- "Robot Vacuum Cleaner Wheel Motor" may be a valid identity
  when "Wheel Motor" alone is too broad.

- A brand-specific phrase such as
  "Wheel Motor for Tefal" should normally NOT be canonical identity.

Instead downstream systems should keep:

IDENTITY:
Robot Vacuum Cleaner Wheel Motor

COMPATIBILITY:
Compatible with Tefal

The same separation principle applies to models and part numbers.

=================================================
5. CONTEXT NECESSITY TEST
=================================================

Before keeping generic context inside canonical_identity, ask:

"If this context is removed,
could the remaining product name reasonably refer
to several materially different product types?"

If YES:
keep the minimum verified generic context needed for clarity.

If NO:
remove the context from canonical_identity.

Do not keep context merely because it appears in SOURCE.

Do not use compatibility brand or model
as a substitute for generic product context.

=================================================
6. SEARCH INTENT FIT
=================================================

Among identities that are accurate, complete,
and boundary-clean, prefer wording that naturally matches
how customers search for the physical product.

Search value must never override:

- factual accuracy
- product identity completeness
- semantic boundary correctness

=================================================
7. PRODUCT RECOGNITION
=================================================

Prefer wording that allows a customer
to immediately understand what physical item is being sold.

Avoid unnecessarily technical, seller-specific,
or ambiguous wording when a clearer verified identity exists.

=================================================
8. CATEGORY CONVENTION
=================================================

Prefer wording that follows normal marketplace naming conventions
for the relevant product type.

Do not invent a category identity when the source does not support it.

=================================================
9. CHARACTER EFFICIENCY
=================================================

Character efficiency is a tie-breaker only.

Use it only after candidate identities are already:

- accurate
- sufficiently complete
- boundary-clean
- appropriately contextualized

Never choose a shorter identity
if it loses necessary product meaning.

=================================================
HARD BOUNDARY RULE
=================================================

Canonical identity must normally exclude:

- package quantity
- compatible brand names
- compatibility wording
- compatible model numbers
- part numbers
- series numbers
- dimensions
- material
- color
- voltage
- power
- independent specifications
- secondary functional features
- promotional wording

If a candidate contains one of these items
and a clean verified identity alternative exists,
the clean alternative should win.

=================================================
FINAL SELECTION LOGIC
=================================================

Do NOT choose canonical_identity by simple average score alone.

Use this decision order:

STAGE 1 — Accuracy Gate

Eliminate candidates that do not accurately identify
the physical product being sold.

STAGE 2 — Boundary Gate

Prefer candidates with clean identity boundaries.

A candidate containing quantity, compatible brand,
compatible model, part number, series number,
or independent specification should normally not win
when a clean verified alternative exists.

STAGE 3 — Completeness Gate

If one candidate is materially too broad or incomplete
and another verified candidate provides a complete identity,
reject the incomplete candidate.

STAGE 4 — Context Necessity

Prefer the candidate containing the minimum generic
device/application context required for clear product identification.

Do not treat compatibility brand or model
as necessary identity context.

STAGE 5 — Marketplace Quality

Compare:

- search_intent_fit
- product_recognition
- category_convention

STAGE 6 — Character Efficiency

Use character efficiency only as the final tie-breaker
between otherwise strong identities.

=================================================
SCORING RULE
=================================================

Score the FINAL selected canonical identity
from 0 to 100 for each factor:

- identity_accuracy
- identity_completeness
- boundary_correctness
- context_necessity
- search_intent_fit
- product_recognition
- category_convention
- character_efficiency

Score meaning:

90-100:
Excellent

75-89:
Strong

60-74:
Acceptable but imperfect

40-59:
Weak

0-39:
Poor

Important:

A high character_efficiency score must never compensate
for poor identity_accuracy, identity_completeness,
or boundary_correctness.

=================================================
IMPORTANT RULES
=================================================

- Do not invent unsupported product facts.
- Do not add unsupported brands, models, specifications,
  quantities, materials, or features.
- Do not choose seller-created marketing names merely because they are unique.
- Do not rewrite a clear verified identity unless necessary.
- Do not use product-specific hardcoded assumptions.
- Do not optimize the full Amazon title.
- Decide only the canonical product identity.
- Return exactly one canonical_identity.
- Keep rejected_identities concise and explain the real reason for rejection.

Return valid JSON only.
""".strip()

    @staticmethod
    def build_input(
        profile: dict,
    ) -> dict:
        """
        从现有 profile 收集身份来源。
        """

        if not isinstance(profile, dict):
            raise IdentityDecisionError(
                "Identity Decision profile must be a dictionary"
            )

        product_identity = profile.get(
            "product_identity",
            {},
        )

        if not isinstance(
            product_identity,
            dict,
        ):
            product_identity = {}

        product_knowledge = profile.get(
            "product_knowledge",
            {},
        )

        if not isinstance(
            product_knowledge,
            dict,
        ):
            product_knowledge = {}

        knowledge_identity = product_knowledge.get(
            "identity",
            {},
        )

        if not isinstance(
            knowledge_identity,
            dict,
        ):
            knowledge_identity = {}

        basic_info = profile.get(
            "basic_info",
            {},
        )

        if not isinstance(
            basic_info,
            dict,
        ):
            basic_info = {}

        source_identities = {
            "raw_product_name": {
                "text": str(
                    product_identity.get(
                        "name",
                        "",
                    )
                    or
                    basic_info.get(
                        "product_name",
                        "",
                    )
                    or
                    ""
                ).strip(),
                "source_path": "product_identity.name",
            },

            "buyer_search_identity": {
                "text": str(
                    product_identity.get(
                        "buyer_search_identity",
                        "",
                    )
                    or
                    ""
                ).strip(),
                "source_path": "product_identity.buyer_search_identity",
            },

            "title_product_identity": {
                "text": str(
                    product_identity.get(
                        "title_product_identity",
                        "",
                    )
                    or
                    ""
                ).strip(),
                "source_path": "product_identity.title_product_identity",
            },

            "knowledge_object_name": {
                "text": str(
                    knowledge_identity.get(
                        "object_name",
                        "",
                    )
                    or
                    ""
                ).strip(),
                "source_path": "product_knowledge.identity.object_name",
            },
        }

        return {
            "source_identities": source_identities,

            "category": str(
                product_identity.get(
                    "category",
                    "",
                )
                or
                knowledge_identity.get(
                    "category",
                    "",
                )
                or
                ""
            ).strip(),

            "parent_product": str(
                product_identity.get(
                    "parent_product",
                    "",
                )
                or
                knowledge_identity.get(
                    "parent_product",
                    "",
                )
                or
                ""
            ).strip(),

            "context": (
                knowledge_identity.get(
                    "context",
                    [],
                )
                if isinstance(
                    knowledge_identity.get(
                        "context",
                        [],
                    ),
                    list,
                )
                else []
            ),

            "buyer_search_intent": str(
                product_knowledge.get(
                    "seo",
                    {},
                ).get(
                    "search_intent",
                    "",
                )
                if isinstance(
                    product_knowledge.get(
                        "seo",
                        {},
                    ),
                    dict,
                )
                else ""
            ).strip(),
        }

    @staticmethod
    def generate(
        profile: dict,
        api_key: str,
        model: str = "gpt-4.1-mini",
    ) -> dict:
        """
        生成 Identity Decision。
        """

        if not api_key:
            raise IdentityDecisionError(
                "OpenAI API key is required"
            )

        decision_input = (
            IdentityDecisionEngine
            .build_input(
                profile
            )
        )

        client = OpenAI(
            api_key=api_key,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            max_retries=0,
        )

        def _request_once():
            return client.chat.completions.create(
                model=model,

            messages=[
                {
                    "role": "system",
                    "content":
                        IdentityDecisionEngine.SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task":
                                "Determine the single canonical product identity.",

                            "input":
                                decision_input,

                            "required_output":
                            {
                                "schema_version":
                                    "2.0",

                                "source_identities":
                                    decision_input.get(
                                        "source_identities",
                                        {},
                                    ),

                                "category_identity":
                                {
                                    "text":
                                        "",

                                    "reason":
                                        "",
                                },

                                "canonical_identity":
                                {
                                    "text":
                                        "",

                                    "decision_source":
                                        "",

                                    "confidence":
                                        0,

                                    "reason":
                                        "",
                                },

                                "decision_factors":
                                {
                                    "identity_accuracy":
                                        0,

                                    "identity_completeness":
                                        0,

                                    "boundary_correctness":
                                        0,

                                    "context_necessity":
                                        0,

                                    "search_intent_fit":
                                        0,

                                    "product_recognition":
                                        0,

                                    "category_convention":
                                        0,

                                    "character_efficiency":
                                        0,
                                },

                                "rejected_identities":
                                    [],
                            },
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                },
            ],

            response_format={
                "type": "json_object"
            },
            )

        response = execute_with_retry(
            _request_once,
            stage="identity_decision",
        )

        try:
            result = json.loads(
                response.choices[0]
                .message
                .content
            )

        except Exception as exc:
            raise IdentityDecisionError(
                f"Identity Decision parse failed: {exc}"
            )

        return (
            IdentityDecisionEngine
            .normalize_result(
                result,
                decision_input,
            )
        )

    @staticmethod
    def normalize_result(
        result: dict,
        decision_input: dict,
    ) -> dict:
        """
        只做 Schema 保护，不重新决定身份。
        """

        if not isinstance(
            result,
            dict,
        ):
            raise IdentityDecisionError(
                "Identity Decision result must be a dictionary"
            )

        canonical = result.get(
            "canonical_identity",
            {},
        )

        if not isinstance(
            canonical,
            dict,
        ):
            canonical = {}

        text = str(
            canonical.get(
                "text",
                "",
            )
            or
            ""
        ).strip()

        if not text:
            raise IdentityDecisionError(
                "canonical_identity.text is required"
            )

        decision_source = str(
            canonical.get(
                "decision_source",
                "",
            )
            or
            ""
        ).strip()

        allowed_sources = {
            "raw_product_name",
            "buyer_search_identity",
            "title_product_identity",
            "knowledge_object_name",
            "category_identity",
            "synthesized",
        }

        if (
            decision_source
            not in allowed_sources
        ):
            decision_source = "synthesized"

        try:
            confidence = int(
                round(
                    float(
                        canonical.get(
                            "confidence",
                            0,
                        )
                    )
                )
            )

        except (
            TypeError,
            ValueError,
        ):
            confidence = 0

        confidence = max(
            0,
            min(
                100,
                confidence,
            ),
        )

        reason = str(
            canonical.get(
                "reason",
                "",
            )
            or
            ""
        ).strip()

        category_identity = result.get(
            "category_identity",
            {},
        )

        if not isinstance(
            category_identity,
            dict,
        ):
            category_identity = {}

        factors = result.get(
            "decision_factors",
            {},
        )

        if not isinstance(
            factors,
            dict,
        ):
            factors = {}

        def normalize_score(
            value,
        ) -> int:

            try:
                score = int(
                    round(
                        float(
                            value
                        )
                    )
                )

            except (
                TypeError,
                ValueError,
            ):
                score = 0

            return max(
                0,
                min(
                    100,
                    score,
                ),
            )

        normalized_factors = {
            "identity_accuracy":
                normalize_score(
                    factors.get(
                        "identity_accuracy",
                        0,
                    )
                ),

            "identity_completeness":
                normalize_score(
                    factors.get(
                        "identity_completeness",
                        0,
                    )
                ),

            "boundary_correctness":
                normalize_score(
                    factors.get(
                        "boundary_correctness",
                        0,
                    )
                ),

            "context_necessity":
                normalize_score(
                    factors.get(
                        "context_necessity",
                        0,
                    )
                ),

            "search_intent_fit":
                normalize_score(
                    factors.get(
                        "search_intent_fit",
                        0,
                    )
                ),

            "product_recognition":
                normalize_score(
                    factors.get(
                        "product_recognition",
                        0,
                    )
                ),

            "category_convention":
                normalize_score(
                    factors.get(
                        "category_convention",
                        0,
                    )
                ),

            "character_efficiency":
                normalize_score(
                    factors.get(
                        "character_efficiency",
                        0,
                    )
                ),
        }

        rejected = result.get(
            "rejected_identities",
            [],
        )

        if not isinstance(
            rejected,
            list,
        ):
            rejected = []

        normalized_rejected = []

        for item in rejected:

            if not isinstance(
                item,
                dict,
            ):
                continue

            rejected_text = str(
                item.get(
                    "text",
                    "",
                )
                or
                ""
            ).strip()

            if not rejected_text:
                continue

            normalized_rejected.append(
                {
                    "text":
                        rejected_text,

                    "source":
                        str(
                            item.get(
                                "source",
                                "",
                            )
                            or
                            ""
                        ).strip(),

                    "reason":
                        str(
                            item.get(
                                "reason",
                                "",
                            )
                            or
                            ""
                        ).strip(),
                }
            )

        return {
            "schema_version":
                "2.0",

            "source_identities":
                decision_input.get(
                    "source_identities",
                    {},
                ),

            "category_identity":
            {
                "text":
                    str(
                        category_identity.get(
                            "text",
                            "",
                        )
                        or
                        ""
                    ).strip(),

                "reason":
                    str(
                        category_identity.get(
                            "reason",
                            "",
                        )
                        or
                        ""
                    ).strip(),
            },

            "canonical_identity":
            {
                "text":
                    text,

                "decision_source":
                    decision_source,

                "confidence":
                    confidence,

                "reason":
                    reason,
            },

            "decision_factors":
                normalized_factors,

            "rejected_identities":
                normalized_rejected,
        }
