from enum import Enum
from typing import List

from pydantic import BaseModel

from rubric import Level


class SubmissionFile(BaseModel):
    name: str
    content: str
    path: str


class ScoreCriteriaResponse(BaseModel):
    selected_level: Level
    feedback: str | None = None


class BatchedScoreResponse(BaseModel):
    criteria_scores: List[ScoreCriteriaResponse]
    overall_feedback: str


class CriteriaScore(BaseModel):
    criteria_name: str
    level_definition: str
    score: float
    feedback: str | None = None


class SubmissionScore(BaseModel):
    total_score: float
    criteria_scores: List[CriteriaScore]
    overall_feedback: str | None = None


class Status(str, Enum):
    PENDING = "pending"
    SCORING = "scoring"
    SCORED = "scored"
    FAILED = "failed"


class Submission(BaseModel):
    name: str
    folder_path: str
    files: List[SubmissionFile]
    status: Status = Status.PENDING
    score: SubmissionScore | None = None
