from typing import Optional

from pydantic import BaseModel


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str
    type: str  # "conversation" or "pricing"
    session_id: str
    picks: Optional[dict] = None  # set when type=="advisor" so frontend can fetch full pricing


class WelcomeResponse(BaseModel):
    reply: str


class ReportRequest(BaseModel):
    session_id: str
    pricing_text: str


class BasketDisk(BaseModel):
    role: str       # "OS disk" | "Data disk"
    type: str       # "premium_ssd" | "standard_ssd" | "standard_hdd" | "premium_ssd_v2"
    tier: str       # "P10" | "E10" | etc. (or "v2" for v2 disks)
    size_gb: int
    cost: float     # monthly USD for this disk


class BasketAddRequest(BaseModel):
    session_id: str
    label: Optional[str] = None       # auto-derived from sku/os/region if omitted
    sku: str
    os: str
    region: str
    term: str                          # "PAYG" | "1yr RI" | "3yr RI" | "Savings Plan"
    count: int = 1
    vm_unit_cost: float                # monthly USD, one VM, no disks
    disks: list[BasketDisk] = []
    pricing_text: Optional[str] = None  # full formatted block, for display only


class BasketItem(BaseModel):
    id: str
    added_at: str           # ISO 8601 UTC
    label: Optional[str] = None
    sku: str
    os: str
    region: str
    term: str
    count: int
    vm_unit_cost: float
    disks: list[BasketDisk]
    line_total: float       # (vm_unit_cost + sum(disk.cost)) * count
    pricing_text: Optional[str] = None


class BasketTotalResponse(BaseModel):
    grand_total: float
    item_count: int


class BasketReportRequest(BaseModel):
    session_id: str
