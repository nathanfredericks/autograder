import os
from pathlib import Path
from typing import Any, Dict, List

import openai
from celery import Celery
from dotenv import load_dotenv
from openai.types.responses import ResponseInputParam

load_dotenv()

from models import (
    BatchedScoreResponse,
    CriteriaScore,
    Status,
    Submission,
    SubmissionFile,
)
from rubric import Criteria
from utils import extract_name

app = Celery(
    "tasks", broker="redis://localhost:6379/0", backend="redis://localhost:6379/0"
)


client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


@app.task
def prepare_submission(
    folder_path: str,
) -> Dict[str, Any]:
    folder_path_obj = Path(folder_path)
    name = extract_name(folder_path_obj.name)

    try:
        python_files = list(folder_path_obj.rglob("*.py"))

        submission_files: List[SubmissionFile] = []
        for file_path in python_files:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            submission_files.append(
                SubmissionFile(
                    name=file_path.name, content=content, path=str(file_path)
                )
            )

        submission = Submission(
            name=name,
            folder_path=str(folder_path),
            files=submission_files,
            status=Status.SCORING,
        )

        return submission.model_dump()

    except Exception:
        raise


@app.task
def score_submission_batched(
    criteria_dicts: List[Dict[str, Any]],
    assignment_description: str,
    submission_dict: Dict[str, Any],
) -> Dict[str, Any]:
    submission = Submission.model_validate(submission_dict)
    all_criteria = [
        Criteria.model_validate(criteria_dict) for criteria_dict in criteria_dicts
    ]

    input_messages: ResponseInputParam = [
        {
            "role": "developer",
            "content": """You are a Computer Science professor scoring a student's assignment for Computer Programming 1 in Python. 
You will be provided with the assignment description, the student's submission, and multiple scoring criteria.
For each criteria, select the most appropriate level and provide feedback if the student did not meet the criteria.
Also provide overall feedback on the submission.
Be fair but thorough in your assessment. Consider code quality, correctness, and adherence to requirements.

IMPORTANT - Use simple language in all feedback:
- Use clear, direct language and avoid complex terminology
- Aim for a Flesch reading score of 80 or higher (8th grade reading level)
- Use the active voice
- Avoid adverbs
- Avoid buzzwords and instead use plain English
- Use jargon where relevant
- Avoid being salesy or overly enthusiastic and instead express calm confidence
- Keep sentences short and clear
- Avoid using em dashes

WHEN YOU ARE PROVIDING OVERALL FEEDBACK, DO NOT MENTION THE FILENAME OR HEADER.
""",
        },
        {
            "role": "user",
            "content": f"""Assignment description:
{assignment_description}

Student: {submission.name}
Submitted files: {len(submission.files)}
""",
        },
    ]

    for file in submission.files:
        input_messages.append(
            {
                "role": "user",
                "content": f"""File name: {file.name}
```python
{file.content}
```""",
            }
        )

    criteria_info: List[str] = []
    for i, criteria in enumerate(all_criteria):
        levels_text = "\n".join(
            [
                f"  * Level {j + 1}: {level.definition} ({level.score} points)"
                for j, level in enumerate(
                    sorted(criteria.levels, key=lambda x: x.score)
                )
            ]
        )
        criteria_info.append(
            f"""Criteria {i + 1}: {criteria.name}
Available levels:
{levels_text}"""
        )

    input_messages.append(
        {
            "role": "user",
            "content": f"""Score ALL of the following criteria based on the student's submission:

{chr(10).join(criteria_info)}

For each criteria, please provide:
1. selected_level: Choose the exact definition and score from the levels above.
2. feedback: Brief one-sentence explanation using simple language if the student did not meet the criteria. Use active voice and keep it clear and direct.

Additionally, provide overall feedback on the submission in 1-2 encouraging sentences using simple language. Focus on code quality, comments, and readability. Keep sentences short and clear.
""",
        }
    )

    try:
        response = client.responses.parse(
            model="gpt-5-mini",
            input=input_messages,
            text={"verbosity": "low"},
            text_format=BatchedScoreResponse,
            reasoning={"effort": "high"},
            store=False,
        )

        if not response or not response.output_parsed:
            raise ValueError("No response from OpenAI")

    except Exception:
        raise

    output_parsed = response.output_parsed

    criteria_scores: List[CriteriaScore] = []
    for i, (criteria, score_response) in enumerate(
        zip(all_criteria, output_parsed.criteria_scores)
    ):
        criteria_score = CriteriaScore(
            criteria_name=criteria.name,
            level_definition=score_response.selected_level.definition,
            score=score_response.selected_level.score,
            feedback=score_response.feedback,
        )
        criteria_scores.append(criteria_score)

    return {
        "criteria_scores": [score.model_dump() for score in criteria_scores],
        "overall_feedback": output_parsed.overall_feedback,
    }
