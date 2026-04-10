"""
SKU Normalizer Agent
Validates and normalizes Azure VM SKU names from user input.
Uses a rule-based approach — no LLM needed for this task.
"""

import re
from app.utils.sku_normalizer import normalize_sku_name

# Known constrained vCPU patterns: size-vcpu
# e.g. E4-2, E8-4, E16-4, E16-8, E32-8, E32-16, E64-32
CONSTRAINED_VCPU_PATTERN = re.compile(
    r'^([A-Za-z]+)(\d+)[-_]?(\d+)([a-z]*)[-_]?(v\d+)?$',
    re.IGNORECASE
)

# Common series letters
VALID_SERIES = {'A', 'B', 'D', 'E', 'F', 'G', 'H', 'L', 'M', 'N', 'S', 'T'}

def normalize_sku(raw: str) -> dict:
    """
    Normalize a raw SKU string from user input.

    Returns:
        {
            "normalized": "Standard_E4-2ads_v5",  # canonical form
            "valid": True,
            "original": "e42adsv5",
            "error": None
        }
    """
    if not raw:
        return {"normalized": None, "valid": False, "original": raw, "error": "Empty SKU"}

    # Strip whitespace and common prefixes
    cleaned = raw.strip()

    # Try normalize_sku_name first
    result = normalize_sku_name(cleaned)

    if result:
        return {
            "normalized": result,
            "valid": True,
            "original": raw,
            "error": None
        }

    return {
        "normalized": None,
        "valid": False,
        "original": raw,
        "error": f"Could not normalize SKU: {raw}. Please check the SKU name."
    }


def extract_and_normalize_sku(llm_output: str) -> str | None:
    """
    Extract SKU from LLM FETCH_PRICING output and normalize it.
    Used to override whatever the LLM produced.
    """
    result = normalize_sku(llm_output)
    return result["normalized"] if result["valid"] else llm_output
