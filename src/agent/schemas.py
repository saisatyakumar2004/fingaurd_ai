from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Status = Literal["verified", "flagged", "ambiguous", "insufficient_evidence"]

ISSUE_TYPES = [
    "scale_mismatch", "identifier_collision", "missing_input", "conflicting_sources",
    "unit_inconsistency", "period_mismatch", "value_mismatch",
]


class EvidenceItem(BaseModel):
    source: str                      # input_record | reference_store
    ref: str                         # record_id / observation_id / company_id / figi
    value: Optional[float] = None
    detail: str = ""


class AgentResponse(BaseModel):
    case_id: str
    status: Status
    answer: str
    normalized_value: Optional[float] = None
    unit: Optional[str] = None
    company_identifier: Optional[str] = None
    fiscal_period: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    explanation: str
    detected_issue_types: list[str] = Field(default_factory=list)
    recommended_action: str
