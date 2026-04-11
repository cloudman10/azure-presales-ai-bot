import re

_STANDARD_VM_SIZES = {1, 2, 4, 8, 16, 32, 48, 64, 80, 96, 128, 192, 208, 416}
_CONSTRAINED_SIZES = sorted({2, 4, 8, 16, 32, 48}, reverse=True)


def _preprocess(raw: str) -> str:
    """
    Normalize raw user input before SKU pattern matching.

    Applied in order (all before the prefix is stripped):
      1. Prefix typos   : Standerd_ / Standar_ / standard_ → Standard_
      2. Version spacing: " v5" / " V3" → "_v5" / "_v3"
      3. Suffix typos   : adv\d → ads_v\d  |  [letter]sv\d → [letter]s_v\d
      4. Constrained gap: digit space digit → digit-digit ("E4 2ads" → "E4-2ads")
      5. Strip spaces   : remove any remaining whitespace
    """
    sku = raw.strip()
    # 1. Prefix typos — Stan[anything]_ → Standard_  (covers Standerd_, Standar_, STANDARD_)
    sku = re.sub(r'^Stan[a-z]*_', 'Standard_', sku, flags=re.IGNORECASE)
    # 2. Space before version number: "D4S v5" → "D4S_v5"
    sku = re.sub(r'\s+(v\d+)', r'_\1', sku, flags=re.IGNORECASE)
    # 3a. "adv" suffix typo: "2adv5" → "2ads_v5"  (adv = misspelling of "ads" + version v)
    sku = re.sub(r'adv(\d)', r'ads_v\1', sku, flags=re.IGNORECASE)
    # 3b. "[letter]sv" suffix typo: "4asv5" → "4as_v5"; lookbehind guards against bare "sv"
    #     after a digit (e.g. "d4sv5") which the downstream _ insertion already handles.
    sku = re.sub(r'(?<=[A-Za-z])sv(\d)', r's_v\1', sku, flags=re.IGNORECASE)
    # 4. Constrained vCPU spacing: "E4 2ads_v5" → "E4-2ads_v5"
    sku = re.sub(r'(\d)\s+(\d)', r'\1-\2', sku)
    # 5. Strip any remaining whitespace
    sku = re.sub(r'\s+', '', sku)
    return sku


def normalize_sku_name(raw: str) -> str | None:
    if not raw:
        return None
    sku = _preprocess(raw)
    if re.match(r'^Standard_', sku, re.IGNORECASE):
        sku = re.sub(r'^Standard_', '', sku, flags=re.IGNORECASE)

    # Handle constrained vCPU format: E8-4ads_v5 or E8-4adsv5
    constrained_match = re.match(r'^([A-Za-z])(\d+-\d+[A-Za-z]*)(_v\d+|v\d+)?$', sku, re.IGNORECASE)
    if constrained_match:
        series = constrained_match.group(1).upper()
        size = constrained_match.group(2).lower()
        version_raw = constrained_match.group(3)
        version = ('_v' + re.sub(r'[^0-9]', '', version_raw)) if version_raw else ''
        return 'Standard_' + series + size + version

    # Constrained vCPU without hyphen: e42adsv5 → Standard_E4-2ads_v5
    # Guard: only split if the first part is a known VM size, avoiding e32adsv5 → E3-2ads_v5.
    no_hyphen = re.match(r'^([A-Za-z])(\d+)([a-z]+)(_?v\d+)?$', sku, re.IGNORECASE)
    if no_hyphen:
        series = no_hyphen.group(1).upper()
        digits = no_hyphen.group(2)
        suffix = no_hyphen.group(3).lower()
        version_raw = no_hyphen.group(4)
        version = ('_v' + re.sub(r'[^0-9]', '', version_raw)) if version_raw else ''
        for cs in _CONSTRAINED_SIZES:
            cs_str = str(cs)
            if digits.endswith(cs_str):
                first = digits[:-len(cs_str)]
                if first and int(first) in _STANDARD_VM_SIZES and cs < int(first):
                    return f'Standard_{series}{first}-{cs_str}{suffix}{version}'

    # Standard format: insert _ before version if missing
    sku = re.sub(r'^([A-Za-z]\d+[A-Za-z]*?)(v\d+)$', r'\1_\2', sku, flags=re.IGNORECASE)
    match = re.match(r'^([A-Za-z])(\d+)([A-Za-z]*)(_v\d+)?$', sku, re.IGNORECASE)
    if not match:
        return None
    version = ('_v' + re.sub(r'[^0-9]', '', match.group(4))) if match.group(4) else ''
    return 'Standard_' + match.group(1).upper() + match.group(2) + match.group(3).lower() + version


def extract_sku(msg: str) -> str | None:
    patterns = [
        re.compile(r'Standard_[A-Za-z]\d+[A-Za-z]*_v\d+', re.IGNORECASE),
        re.compile(r'Standard_[A-Za-z]\d+[A-Za-z]*', re.IGNORECASE),
        re.compile(r'[A-Za-z]\d+[a-z]*_v\d+'),
        re.compile(r'[A-Za-z]\d+[a-z]*v\d+'),
        re.compile(r'[A-Za-z]\d+[a-z]*\s+v\d+'),
    ]
    for p in patterns:
        m = p.search(msg)
        if m and not re.match(r'^\d', m.group(0)):
            return m.group(0)
    return None
