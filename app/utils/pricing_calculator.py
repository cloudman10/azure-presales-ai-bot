HOURS_PER_MONTH = 730


def _get_item_price_type(item: dict) -> str:
    return item.get('priceType') or item.get('type') or ''


def ri_monthly(item: dict) -> float:
    price = item['retailPrice']
    uom = (item.get('unitOfMeasure') or '').lower()
    term = item.get('reservationTerm') or '1 Year'

    # Annual/multi-year lump sum stored with non-hour unit
    if 'year' in uom:
        if term == '3 Years':
            return price / 36
        return price / 12

    # Stored as hourly rate — check if true hourly or lump sum mislabelled as hourly
    # Heuristic: real VM hourly RI rates are always under $50/hr
    if price > 50:
        if term == '3 Years':
            return (price / 26280) * HOURS_PER_MONTH
        return (price / 8760) * HOURS_PER_MONTH

    # True hourly rate
    return price * HOURS_PER_MONTH


def detect_item_os(item: dict) -> str:
    product = (item.get('productName') or '').lower()
    sku = (item.get('skuName') or '').lower()
    if 'windows' in product or 'windows' in sku:
        return 'Windows'
    return 'Linux'


def find_price(items: list, os: str, price_type: str, term: str = None) -> dict | None:
    # Filter out spot and low priority items
    clean = [
        item for item in items
        if 'spot' not in (item.get('skuName') or '').lower()
        and 'low priority' not in (item.get('skuName') or '').lower()
    ]

    def is_hourly(item: dict) -> bool:
        return 'Hour' in (item.get('unitOfMeasure') or '')

    def matches_price_type(item: dict) -> bool:
        pt = _get_item_price_type(item)
        if price_type == 'Consumption':
            return pt == 'Consumption' and is_hourly(item)
        if price_type == 'Reservation':
            return pt == 'Reservation' and item.get('reservationTerm') == term and is_hourly(item)
        return False

    if price_type == 'Consumption':
        by_os = [item for item in clean if detect_item_os(item) == os and matches_price_type(item)]
        if by_os:
            return by_os[0]
        # Fallback: Windows = highest priced, Linux = lowest priced
        all_consumption = [item for item in clean if matches_price_type(item)]
        if not all_consumption:
            return None
        if os == 'Windows':
            return sorted(all_consumption, key=lambda x: x['retailPrice'], reverse=True)[0]
        return sorted(all_consumption, key=lambda x: x['retailPrice'])[0]

    if price_type == 'Reservation':
        by_os = [item for item in clean if detect_item_os(item) == os and matches_price_type(item)]
        if by_os:
            return by_os[0]
        # Fallback: Windows = highest priced, Linux = lowest priced
        all_ri = [item for item in clean if matches_price_type(item)]
        if not all_ri:
            return None
        if os == 'Windows':
            return sorted(all_ri, key=lambda x: x['retailPrice'], reverse=True)[0]
        return sorted(all_ri, key=lambda x: x['retailPrice'])[0]

    return None
