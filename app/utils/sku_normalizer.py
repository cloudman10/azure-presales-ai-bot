import re


def normalize_sku_name(raw: str) -> str | None:
    if not raw:
        return None
    sku = raw.strip()
    if re.match(r'^Standard_', sku, re.IGNORECASE):
        sku = re.sub(r'^Standard_', '', sku, flags=re.IGNORECASE)
    sku = re.sub(r'\s+', '', sku)

    # Handle constrained vCPU format: E8-4ads_v5 or E8-4adsv5
    constrained_match = re.match(r'^([A-Za-z])(\d+-\d+[A-Za-z]*)(_v\d+|v\d+)?$', sku, re.IGNORECASE)
    if constrained_match:
        series = constrained_match.group(1).upper()
        size = constrained_match.group(2).lower()
        version_raw = constrained_match.group(3)
        version = ('_v' + re.sub(r'[^0-9]', '', version_raw)) if version_raw else ''
        return 'Standard_' + series + size + version

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
