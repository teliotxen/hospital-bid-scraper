from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query

from app.loader import load_hospitals
from app.models import HospitalBids, HospitalInfo
from app.scraper import fetch_hospital_bids

# ---------------------------------------------------------------------------
# App lifespan – load hospital list once at startup
# ---------------------------------------------------------------------------

_hospitals: List[HospitalInfo] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _hospitals
    _hospitals = load_hospitals()
    yield


app = FastAPI(
    title="병원 입찰 정보 스크래퍼",
    description="병원 입찰 게시판에서 입찰 정보(입찰명, 날짜, URL)를 수집합니다.",
    version="1.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# HTTP client factory
# ---------------------------------------------------------------------------

def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(30.0))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/hospitals", summary="등록된 병원 목록 조회", response_model=List[HospitalInfo])
async def list_hospitals(
    region: Optional[str] = Query(default=None, description="지역 필터 (예: 서울특별시)"),
) -> List[HospitalInfo]:
    """스크래핑 없이 CSV에 등록된 병원 목록만 반환합니다."""
    hospitals = _hospitals
    if region:
        hospitals = [h for h in hospitals if region in h.region]
    return hospitals


@app.get("/bids", summary="모든 병원 입찰 정보 수집", response_model=List[HospitalBids])
async def get_all_bids(
    region: Optional[str] = Query(default=None, description="지역 필터 (예: 서울특별시)"),
) -> List[HospitalBids]:
    """
    CSV에 등록된 모든 병원의 입찰 정보를 병렬로 수집합니다.
    각 병원별로 입찰 목록(입찰명, 날짜, URL)을 반환하며,
    데이터가 없거나 오류가 있으면 `bids`는 빈 배열, `error`에 사유를 표시합니다.
    """
    hospitals = _hospitals
    if region:
        hospitals = [h for h in hospitals if region in h.region]

    async with _make_client() as client:
        tasks = [fetch_hospital_bids(client, h) for h in hospitals]
        results: List[HospitalBids] = list(await asyncio.gather(*tasks))

    return results


@app.get("/bids/{hospital_name}", summary="특정 병원 입찰 정보 수집", response_model=HospitalBids)
async def get_hospital_bids(hospital_name: str) -> HospitalBids:
    """
    병원명으로 단일 병원의 입찰 정보를 수집합니다.
    병원명은 CSV의 `병원명` 컬럼과 정확히 일치해야 합니다.
    """
    hospital = next(
        (h for h in _hospitals if h.hospital == hospital_name),
        None,
    )
    if not hospital:
        raise HTTPException(status_code=404, detail=f"병원을 찾을 수 없습니다: {hospital_name}")

    async with _make_client() as client:
        return await fetch_hospital_bids(client, hospital)
