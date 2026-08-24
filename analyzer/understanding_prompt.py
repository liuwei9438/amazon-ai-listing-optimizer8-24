from __future__ import annotations

import json

from dataclasses import asdict, is_dataclass

from typing import Any



PROMPT_VERSION = "V6.1-semantic-identifier-gate"


SYSTEM_PROMPT = """
You are the product understanding layer of an Amazon listing system.

Your task is to analyze the product source information and return ONLY structured product data matching the provided JSON schema.

Rules:

- Do not write titles, bullets, descriptions, or marketing copy.
- Preserve source facts exactly.
- Never invent missing information.
- Use empty strings or empty arrays when information is unavailable.
- Separate product identity, features, usage scenarios, specifications, and identifiers.
- Treat third-party brands as compatibility references unless ownership is proven.
- Never accept claims such as original, genuine, official, OEM, authentic as verified facts.

Return only JSON matching the schema.
""".strip()



def _record_dict(
    record: Any,
) -> dict[str, Any]:


    if is_dataclass(record):

        return asdict(record)


    if isinstance(
        record,
        dict,
    ):

        return dict(record)


    if hasattr(
        record,
        "__dict__",
    ):

        return dict(
            vars(record)
        )


    return {
        "value":
            str(record)
    }




def build_user_prompt(
    record: Any,
    fact_lock: dict[str, Any],
    profile_template: dict[str, Any],
    source_fact_ledger: dict[str, Any] | None = None,
) -> str:


    source = _record_dict(
        record
    )

    if not isinstance(
        source_fact_ledger,
        dict,
    ):
        source_fact_ledger = {}

    return f"""
Analyze the SOURCE as one product and fill the complete Product Profile.


IMPORTANT RULES:


## 0. SOURCE FACT COVERAGE — NO SILENT DROP

A deterministic SOURCE FACT LEDGER is provided below.

This ledger exists because source listings often contain high-value facts
that can be accidentally lost during product understanding.

You MUST inspect the original SOURCE together with SOURCE_FACT_LEDGER.

Important distinction:

- You are allowed to decide that a source phrase is low value, marketing,
  seller/store branding, redundant, or unsuitable for the title.
- You are NOT allowed to silently forget a high-value source fact.

Pay special attention to:

- model numbers
- part numbers
- product codes
- series
- device / machine / equipment context
- compatibility clues
- dimensions and specifications
- quantity
- material
- product-defining secondary nouns

If an alphanumeric token appears in the source title, preserve the
underlying source fact, but DO NOT assume that the token is an identifier.

Before assigning any alphanumeric token to model_number, part_number,
series_number, or unknown_code, first determine whether it has a clear
non-identifier meaning.

The following must be classified by their actual meaning and must NOT
enter identifier fields merely because they contain letters and numbers:

- package quantity, such as 2pcs, 10PCS, 3 sets
- dimensions, such as 52mm, 79X72mm, 65x12x28mm
- voltage, such as 12V, 220V, 8.4V
- power, such as 6W, 240W
- resistance, such as 8K, 8-9k ohms
- weight or capacity
- speed, pressure, temperature, frequency, or other measurable values
- wire/conductor counts, such as 4-Wire, 4-Wires, 4-Conductor
- configuration counts or feature levels
- color or material descriptors
- package or set counts

Preserving a source fact does NOT mean preserving it as an identifier.

Use this semantic classification order before identifier assignment:

1. quantity
2. dimensions / measurable specification
3. voltage / power / resistance / electrical specification
4. material / color
5. feature / configuration
6. compatibility context
7. model_number / part_number / series_number
8. unknown_code only when no supported semantic classification is possible

Only assign a token to model_number, part_number, or series_number when
there is positive source evidence that it identifies a product, device,
machine, replacement part, or product family.

unknown_code is a last-resort classification.
It must not be used for a value that already has a clear specification,
quantity, feature, material, color, or compatibility meaning.

If a source title contains meaningful device/application context such as
"Woodworking Edgebanding Machine", "CNC Router", "Sewing Machine", etc.,
preserve that context when it helps identify or search for the product.

Marketing terms such as Wholesale, Original, Genuine, Official, OEM,
Premium, Best Seller, etc. must NOT be preserved as product facts.

Seller/store/private-label branding must follow the Seller Brand Hygiene
rules below and must not be converted into compatibility automatically.


## 0.5 NUMERIC COMPATIBILITY MODEL RECOGNITION

Pure numeric identifiers can be legitimate compatibility models.

Example source:
"Suitable For Chainsaw 340 345 346 350 351 353 357 359 362 365 372
 Equipment Ignition Coil"

When the source grammar clearly presents these numbers as compatible equipment
models, preserve them as compatibility.models / important_compatibility even
though they contain digits only.

Do NOT require a model to contain letters.

Do NOT convert a discrete model list into a numeric range.

Example:
340 345 346 350 351 353 357 359 362 365 372

must NOT become:
340-372

because that would imply unsupported intermediate models.

Distinguish numeric models from:
- dimensions
- voltage/power
- quantities
- measurement tolerance notes
- dates
- source instruction numbering

Use source context to classify them.


## 1. Fact Protection

Only use information directly supported by SOURCE.

Do not invent:

- quantity
- material
- color
- dimensions
- voltage
- power
- package contents
- compatible models
- part numbers
- product functions
- usage scenarios


If information is missing:

Use:

- empty string
- empty list



## 2. Product Identity Classification


Separate information into:


## 2. Product Identity Classification


Separate product identity information into different levels.


product_identity.name:

The core product name only.

This is the direct name of the product itself.

Do not include:

- usage scenarios
- target users
- materials
- marketing words
- compatibility claims
- application descriptions

title_product_identity:

Select exactly ONE authoritative product identity
for downstream Amazon title generation.

This field must contain the single best expression of:

"What exactly is the customer buying?"

Do not return alternative product identities.

Do not return a list.

Do not combine several synonymous product names.

The selected title_product_identity becomes the authoritative
product identity for all downstream title decisions.


IDENTITY SELECTION PRINCIPLE:

When multiple valid ways of describing the product exist,
evaluate them and select the ONE expression that provides
the strongest overall product identification.

Evaluate possible identity expressions in this order:

1. Product identification accuracy

Does the expression correctly identify
the actual physical product being sold?

An expression that identifies the wrong object,
a broader category, or only part of the sold product
must not be selected.


2. Product identity completeness

Does the expression communicate enough information
for a customer to understand what the product actually is?

Prefer an identity that fully describes the sold item
over one that communicates only a partial product concept.


3. Identity boundary correctness

Prefer an identity that keeps
independent product attributes separate.

A longer expression is NOT more complete
if its extra words actually belong to:

- quantity
- compatibility
- model
- part number
- specification
- material
- color
- feature

Completeness means complete product identity,
not complete product information.


4. Necessary device or application context

Determine whether the product name alone is sufficient.

If customers need device, machine, equipment,
or application context to understand what the product is,
include that context in title_product_identity.
CONTEXT NECESSITY TEST:

Generic device or application context may remain
inside title_product_identity only when removing it
would make the product identity materially ambiguous
or incomplete.

Use the minimum context necessary.

Do not include:

- a specific compatible brand
- a specific compatible model
- a compatibility phrase

merely to make the identity appear more complete.

Necessary generic context describes
what kind of product this is.

Compatibility describes
what specific brand, model, machine,
or platform the product fits.

Do not confuse these two roles.
Include only context that materially improves
product identification.

Do not include context merely because it appears in SOURCE.


5. Customer search relevance

Among identities that are equally accurate and complete,
prefer the expression that most naturally matches
how customers would identify or search for the product.


6. Character efficiency

Only after accuracy, completeness, necessary context,
and search relevance are satisfied,
prefer the more character-efficient expression.

Never choose a shorter identity if shortening it
reduces product identification accuracy or completeness.


OVER-BROAD IDENTITY RULE:

Do not select an expression that only describes
a broad category or generic product group.

Examples of overly broad identity concepts include
generic ideas such as:

- parts
- accessories
- components
- replacement parts

when the actual sold product can be identified more precisely.


OVER-NARROW IDENTITY RULE:

Do not select an identity that describes only
a partial product concept when necessary context is missing.

If the core product type could reasonably refer
to products used with many different devices or applications,
include enough verified device/application context
to make the sold product clear.


IDENTITY BOUNDARY:

title_product_identity may contain:

- the actual core product type
- necessary product-defining component type
- necessary device/application context
IDENTITY SEPARABILITY TEST:

Before finalizing title_product_identity,
check whether each piece of information belongs
to the physical product identity itself
or to an independent downstream attribute.

Ask:

"Can this information be represented independently
without changing what the physical product fundamentally is?"

If YES,
do not absorb it into title_product_identity.

Independent downstream information includes:

- quantity
- compatibility brand
- compatible model
- part number
- series number
- dimensions
- color
- material
- voltage
- power
- technical specifications
- functional features

These facts must remain separate
so downstream title strategy can evaluate them independently.
title_product_identity must not contain:

- package quantity
- compatible brand names
- compatibility wording
- compatible model numbers
- part numbers unless inseparable from product identity
- material unless inseparable from product identity
- color
- dimensions
- voltage
- power
- secondary features
- marketing wording
- seller-created categories
- promotional wording


SINGLE IDENTITY RULE:

There must be exactly ONE title_product_identity.

Other valid synonymous, broader, narrower,
or alternative search expressions must not be merged
into title_product_identity.

They may remain available elsewhere in the Product Profile
for:

- bullet points
- SEO keywords
- backend search terms
- supporting product information

but they must not become additional title identities.


FINAL CHECK:

Before returning title_product_identity, verify:

1. Does it accurately identify the sold product?
2. Is it sufficiently complete?
3. Does it contain necessary product context?
4. Is it neither too broad nor too narrow?
5. Is it natural for customer search?
6. Does it exclude package quantity?
7. Does it exclude compatibility brands and wording?
8. Does it exclude compatible models and part numbers?
9. Does it exclude independent specifications and features?
10. Does every remaining context word help identify
    what the physical product actually is?

If a shorter identity fails any of these checks,
use the more complete identity.



context:

Include:

- device context
- application object
- environment
- target user


design_features:

Only physical design characteristics.


functional_features:

Only confirmed product functions.


usage_scenarios:

Only supported usage situations.



## 3. Identifier Classification


Analyze numbers, codes, and alphanumeric values according to their actual product role.


Do not classify values only because they contain numbers, letters, or special characters.


Classify into:


model_number:

A value that identifies a specific product model, device model, or compatible machine model.

A model_number should help users distinguish one product identity from another.


part_number:

A manufacturer-defined or replacement part identifier.


series_number:

A product family or series identifier.


unknown_code:

A code whose meaning cannot be safely determined.

Important quantity classification rule:

Do not classify package quantity as identifiers.

Examples:

- 2PCS
- 4PCS
- 6PCS
- 12 pieces
- 3 sets

These values represent package quantity and must be classified as quantity information.

They should not enter:

- model_numbers
- part_numbers
- series_numbers
- unknown_codes

Only classify values as identifiers when they identify a specific product model, part number, or product code.

Decision process:


First determine whether the value identifies a specific product identity.


If the value only describes:

- product capability
- technical performance
- feature level
- configuration count
- protection level
- operating parameter
- measurable specification

do not classify it as model_number.


If a token has a recognizable factual meaning such as quantity,
specification, feature, material, color, compatibility, or context,
classify it in that semantic field and DO NOT place it in unknown_code.

unknown_code is allowed only when the source clearly contains a code-like
token and its semantic role genuinely cannot be determined after checking
all supported factual categories.


Do not put:

- voltage
- power
- dimensions
- weight
- technical specifications

into identifiers.


Only verified product identity information can enter:

- model_numbers
- part_numbers
- series_numbers



## 4. Specification Extraction


Extract measurable facts separately.


Include:


dimensions:

Size information.


weight:

Weight information.


voltage:

Voltage information.


power:

Power rating.


capacity:

Capacity information.



## 5. Brand Handling


SOURCE BRAND HYGIENE — IMPORTANT

Collected marketplace data may contain the source seller's / store's own
brand name inside the title, bullets, description, details, or metadata.

Treat these as two different concepts:

seller_brand:
- the merchant/store/private-label brand belonging to the listing seller
- source-shop branding or seller-created brand wording
- NOT a compatibility target
- NOT part of product identity
- NOT a search term to preserve in generated listing content

third_party_brands:
- external equipment/product brands that the item is genuinely compatible with
- may be used only with explicit compatibility wording

If a source seller/store brand is identifiable:
- put it only in brand_info.seller_brand
- DO NOT copy it into third_party_brands
- DO NOT copy it into detected_brands
- DO NOT copy it into compatibility.brands
- DO NOT include it in product_identity.name
- DO NOT include it in title_product_identity
- DO NOT preserve it in title, bullets, highlights, description, SEO keywords
  or backend keywords
- remove it mentally from the source before deciding the real product identity

Never infer that a seller/store brand is a compatibility brand merely because
it appears in the source title or description.

When brand ownership is uncertain:
- do not label an arbitrary product brand as seller_brand
- only classify as seller_brand when source context reasonably indicates it is
  the listing seller/store/private-label branding


For brand information:


If the product is compatible with a third-party brand:

relationship:

unbranded_compatible


Use:

Compatible with + brand


Do not treat:

- original
- genuine
- official
- OEM
- authentic

as verified facts.



## 6. Compatibility


Only keep compatibility information directly supported by SOURCE.


Do not invent:

- models
- brands
- replacements



## 7. Feature Classification


Classify features into:


materials:

Confirmed materials only.


design_features:

Physical structure only.


functional_features:

Confirmed functions only.


specifications:

Measured values or technical parameters.


usage_scenarios:

Supported usage only.



Do not convert assumptions into features.

## Title Information Selection


The Amazon title has limited space.

Select the most valuable information that should appear in the title.


priority_attributes:

Select important attributes that help customers distinguish or choose the product.

Prioritize information that is difficult to recover after removing it from the title.

Examples:

- package quantity
- special configuration
- important design characteristics
- version differences


important_specifications:

Select technical specifications that customers commonly search for.

Examples:

- power
- capacity
- size
- important model-related specifications


important_quantity:

Always keep package quantity when quantity is clearly provided in SOURCE.

Package quantity should be considered a high-priority title attribute when it affects customer purchase decisions.

Quantity is especially important for:

- replacement parts
- accessories
- multi-piece packages
- consumable products

Examples:

- 2PCS Filter
- 6PCS Tuning Pegs
- 4PCS Replacement Blades


important_context:

Keep important product context required for customer understanding.

Examples:

- device type
- application object
- special usage context


important_compatibility:

Keep important compatibility information supported by SOURCE.

Examples:

- compatible brands
- compatible device families
- important compatible models


Do not select:

- generic seller categories
- marketing words
- unsupported claims
- low-value information that does not help search relevance or purchase decision


The goal is:

Choose the highest-value facts for a limited Amazon title length.

## 8. Search Strategy

Generate search strategy only from verified information.

Primary search identity should be derived from buyer_search_identity when available.

title_identifiers:

Only high-value identifiers suitable for title.
Title identifiers must come only from verified model_number, part_number, or series_number.

Do not use:
- specifications
- features
- performance values
- marketing descriptions

bullet_identifiers:

Additional compatibility identifiers.


backend_identifiers:

Remaining verified identifiers.


Do not invent keywords.



## 9. Compliance


Identify risky claims:


- original
- genuine
- official
- OEM
- authentic
- authorized
- best seller
- #1
- premium quality


Return compliance information.



## 10. Output


Return every field required by PROFILE TEMPLATE.


The output must match the JSON schema exactly.



SOURCE:

{json.dumps(
    source,
    ensure_ascii=False,
    default=str
)}


VERIFIED SOURCE_FACT_LEDGER:

{json.dumps(source_fact_ledger, ensure_ascii=False, indent=2)}


FACT LOCK:

{json.dumps(
    fact_lock,
    ensure_ascii=False
)}


PROFILE TEMPLATE:

{json.dumps(
    profile_template,
    ensure_ascii=False
)}

""".strip()
