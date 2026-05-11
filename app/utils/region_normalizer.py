REGION_MAP: dict[str, str] = {
    'australia east': 'australiaeast', 'australia southeast': 'australiasoutheast',
    'australia central': 'australiacentral', 'east asia': 'eastasia',
    'southeast asia': 'southeastasia', 'central india': 'centralindia',
    'south india': 'southindia', 'west india': 'westindia',
    'japan east': 'japaneast', 'japan west': 'japanwest',
    'korea central': 'koreacentral', 'korea south': 'koreasouth',
    'new zealand north': 'newzealandnorth', 'malaysia west': 'malaysiawest',
    'malaysia central': 'malaysiacentral', 'indonesia central': 'indonesiacentral',
    'east us': 'eastus', 'east us 2': 'eastus2', 'west us': 'westus',
    'west us 2': 'westus2', 'west us 3': 'westus3', 'central us': 'centralus',
    'north central us': 'northcentralus', 'south central us': 'southcentralus',
    'canada central': 'canadacentral', 'canada east': 'canadaeast',
    'brazil south': 'brazilsouth', 'north europe': 'northeurope',
    'west europe': 'westeurope', 'uk south': 'uksouth', 'uk west': 'ukwest',
    'france central': 'francecentral', 'germany west central': 'germanywestcentral',
    'switzerland north': 'switzerlandnorth', 'norway east': 'norwayeast',
    'sweden central': 'swedencentral', 'uae north': 'uaenorth',
    'south africa north': 'southafricanorth', 'israel central': 'israelcentral',
    'qatar central': 'qatarcentral',
}

# City → arm region name mappings
CITY_MAP: dict[str, str] = {
    # Australia
    'sydney':    'australiaeast',
    'canberra':  'australiaeast',
    'brisbane':  'australiaeast',
    'perth':     'australiaeast',
    'adelaide':  'australiaeast',
    'melbourne': 'australiasoutheast',
    'melboure':  'australiasoutheast',   # common typo
    'melbourre': 'australiasoutheast',   # common typo
    'melborne':  'australiasoutheast',   # common typo

    # Asia Pacific
    'singapore':   'southeastasia',
    'tokyo':       'japaneast',
    'osaka':       'japanwest',
    'seoul':       'koreacentral',
    'busan':       'koreasouth',
    'mumbai':      'centralindia',
    'pune':        'centralindia',
    'hyderabad':   'centralindia',
    'chennai':     'southindia',
    'bangalore':   'southindia',
    'hong kong':   'eastasia',
    'hongkong':    'eastasia',
    'beijing':     'chinanorth',
    'shanghai':    'chinaeast',
    'taipei':      'eastasia',
    'kuala lumpur':'southeastasia',
    'jakarta':     'southeastasia',
    'bangkok':     'southeastasia',
    'manila':      'southeastasia',

    # Middle East
    'dubai':     'uaenorth',
    'abu dhabi': 'uaecentral',
    'riyadh':    'saudiarabiawest',
    'doha':      'qatarcentral',
    'tel aviv':  'israelcentral',

    # Europe
    'london':     'uksouth',
    'manchester': 'ukwest',
    'amsterdam':  'westeurope',
    'dublin':     'northeurope',
    'paris':      'francecentral',
    'frankfurt':  'germanywestcentral',
    'berlin':     'germanywestcentral',
    'zurich':     'switzerlandnorth',
    'geneva':     'switzerlandwest',
    'stockholm':  'swedencentral',
    'oslo':       'norwayeast',
    'helsinki':   'finlandcentral',
    'warsaw':     'polandcentral',
    'milan':      'italynorth',
    'madrid':     'spaincentral',

    # Americas
    'new york':      'eastus',
    'new york city': 'eastus',
    'nyc':           'eastus',
    'virginia':      'eastus',
    'washington':    'eastus2',
    'chicago':       'northcentralus',
    'dallas':        'southcentralus',
    'texas':         'southcentralus',
    'phoenix':       'westus3',
    'los angeles':   'westus',
    'san francisco': 'westus',
    'seattle':       'westus2',
    'toronto':       'canadacentral',
    'vancouver':     'canadacentral',
    'montreal':      'canadaeast',
    'sao paulo':     'brazilsouth',
    'mexico city':   'mexicocentral',

    # Africa
    'johannesburg': 'southafricanorth',
    'cape town':    'southafricawest',
}


def extract_region(msg: str) -> dict | None:
    lower = msg.lower()

    # Match by display name (longest first to avoid partial matches)
    for key in sorted(REGION_MAP.keys(), key=len, reverse=True):
        if key in lower:
            return {'display_name': key, 'arm_name': REGION_MAP[key]}

    # Match by arm name (longest first)
    for key, val in sorted(REGION_MAP.items(), key=lambda x: len(x[1]), reverse=True):
        if val in lower:
            return {'display_name': key, 'arm_name': val}

    # Match by city
    for city, arm in CITY_MAP.items():
        if city in lower:
            display = next((k for k, v in REGION_MAP.items() if v == arm), arm)
            return {'display_name': display, 'arm_name': arm}

    return None


def display_region(arm: str) -> str:
    for key, val in REGION_MAP.items():
        if val == arm:
            return ' '.join(word.capitalize() for word in key.split())
    return arm
