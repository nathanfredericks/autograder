import os
from pathlib import Path
from typing import Any, Dict, List

import openai
from celery import Celery
from dotenv import load_dotenv
from openai.types.responses import ResponseInputParam

load_dotenv()

from models import (CriteriaScore, OverallFeedbackResponse,
                    ScoreCriteriaResponse, Status, Submission, SubmissionFile)
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
def score_criteria(
    criteria_dict: Dict[str, Any],
    assignment_description: str,
    submission_dict: Dict[str, Any],
) -> dict[str, Any]:
    criteria = Criteria.model_validate(criteria_dict)
    submission = Submission.model_validate(submission_dict)

    input_messages: ResponseInputParam = [
        {
            "role": "developer",
            "content": """You are a Computer Science professor scoring a student's assignment for Computer Programming 1 in Python. 
You will be provided with the assignment description, the student's submission, and the scoring criteria.
Select the most appropriate level for the criteria and provide feedback if the student did not meet the criteria.
Be fair but thorough in your assessment. Consider code quality, correctness, and adherence to requirements.
""",
        },
        {
            "role": "user",
            "content": f"""Assignment description:
{assignment_description}
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

    levels_text = "\n".join(
        [
            f"* Level {i + 1}: {level.definition} ({level.score} points)"
            for i, level in enumerate(sorted(criteria.levels, key=lambda x: x.score))
        ]
    )

    input_messages.append(
        {
            "role": "user",
            "content": f"""Score the following criteria based on the student's submission:
Criteria:
{criteria.name}

Available levels:
{levels_text}

Please provide:
1. selected_level: Choose the exact definition and score from the levels above.
2. feedback: Brief one-sentence explanation of the score if the student did not meet the criteria.
""",
        }
    )

    try:
        response = client.responses.parse(
            model="gpt-5-mini",
            input=input_messages,
            text={"verbosity": "low"},
            text_format=ScoreCriteriaResponse,
            reasoning={"effort": "low"},
            store=False,
        )

        if not response or not response.output_parsed:
            raise ValueError("No response from OpenAI")

    except Exception:
        raise

    output_parsed = response.output_parsed

    criteria_score = CriteriaScore(
        criteria_name=criteria.name,
        level_definition=output_parsed.selected_level.definition,
        score=output_parsed.selected_level.score,
        feedback=output_parsed.feedback,
    )

    return criteria_score.model_dump()


@app.task
def generate_overall_feedback(
    assignment_description: str,
    submission_dict: Dict[str, Any],
    criteria_scores_dicts: List[Dict[str, Any]],
) -> str:
    submission = Submission.model_validate(submission_dict)
    criteria_scores = [
        CriteriaScore.model_validate(score_dict) for score_dict in criteria_scores_dicts
    ]

    total_score = sum(score.score for score in criteria_scores)

    scores_summary = "\n".join(
        [
            f"- {score.criteria_name}: {score.score} points ({score.level_definition})"
            for score in criteria_scores
        ]
    )

    input_messages: ResponseInputParam = [
        {
            "role": "developer",
            "content": """You are a friendly Computer Science professor providing overall feedback on a student's Python assignment.
Based on the assignment description, the student's code, and their scores, provide concise, encouraging feedback in 1-2 sentences.

Guidelines:
- If they did well with no major issues, give positive encouragement like "Keep up the great work!"
- If there are issues, be constructive but friendly
- Focus on code quality, comments, readability, and overall approach
- Keep it brief and encouraging
- Be specific when possible (mention what they did well or what to improve)
- Only provide feedback on the submission, not on the criteria.

Writing Guidelines:
- Use clear, direct language and avoid complex terminology.
- Aim for a Flesch reading score of 80 or higher.
- Use the active voice.
- Avoid adverbs.
- Avoid buzzwords and instead use plain English.
- Use jargon where relevant.
- Avoid being salesy or overly enthusiastic and instead express calm confidence.
""",
        },
        {
            "role": "user",
            "content": f"""Assignment Description:
{assignment_description}

Student: {submission.name}
Total Score: {total_score} points

Scoring Results:
{scores_summary}

Student's Code:""",
        },
    ]

    for file in submission.files:
        input_messages.append(
            {
                "role": "user",
                "content": f"""File: {file.name}
```python
{file.content}
```""",
            }
        )

    input_messages.append(
        {
            "role": "user",
            "content": "Please provide concise, friendly overall feedback (1 line) on this submission, focusing on code quality, comments, and readability.",
        }
    )

    try:
        response = client.responses.parse(
            model="gpt-5-mini",
            input=input_messages,
            text={"verbosity": "low"},
            text_format=OverallFeedbackResponse,
            reasoning={"effort": "low"},
            store=False,
        )

        if not response or not response.output_parsed:
            raise ValueError("No response from OpenAI")

        feedback = response.output_parsed.feedback

        return feedback

    except Exception:
        raise
