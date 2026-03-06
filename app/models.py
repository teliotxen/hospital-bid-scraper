from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class BidItem(BaseModel):
    title: Optional[str] = None
    date: Optional[str] = None
    url: Optional[str] = None


class HospitalBids(BaseModel):
    hospital: str
    region: str
    source_url: str
    bids: list[BidItem]
    error: Optional[str] = None


class HospitalInfo(BaseModel):
    hospital: str
    url: str
    region: str
    table_tag: Optional[str] = None
    div_tag: Optional[str] = None
    type_desc: Optional[str] = None
