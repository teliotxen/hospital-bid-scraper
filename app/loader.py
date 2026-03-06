from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Optional

from app.models import HospitalInfo

DATA_PATH = Path(__file__).parent.parent / "data" / "hospitals.csv"

# Clean parenthetical notes from tag values like "itemes(ul tag)"
_PAREN_RE = re.compile(r"\s*\(.*?\)\s*")


def _clean_tag(value: str) -> Optional[str]:
    value = _PAREN_RE.sub("", value).strip()
    return value if value else None


def load_hospitals(path: Path = DATA_PATH) -> list[HospitalInfo]:
    hospitals: list[HospitalInfo] = []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("병원명", "").strip()
            url = row.get("URL", "").strip()
            if not name or not url:
                continue
            hospitals.append(
                HospitalInfo(
                    hospital=name,
                    url=url,
                    region=row.get("지역", "").strip(),
                    table_tag=_clean_tag(row.get("table_tag", "")),
                    div_tag=_clean_tag(row.get("div_tag", "")),
                    type_desc=row.get("타입 설명", "").strip() or None,
                )
            )
    return hospitals
