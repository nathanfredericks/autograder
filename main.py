import csv
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import List

import typer
from rich.progress import (BarColumn, MofNCompleteColumn, Progress,
                           TaskProgressColumn, TextColumn, TimeElapsedColumn,
                           TimeRemainingColumn)

from models import CriteriaScore, Status, Submission, SubmissionScore
from rubric import Rubric, load_rubric, print_rubric
from submission import get_submissions
from tasks import prepare_submission, score_submission_batched
from utils import (configure_rich_progress_logging, extract_name,
                   format_decimal, print_error)

CSV_FIELDNAMES = ["Name", "Criteria", "Level", "Score", "Feedback"]


def initialize_csv(output_path: Path = Path("scores.csv")) -> None:
    with open(output_path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()


def append_submission_to_csv(
    submission: Submission,
    output_path: Path = Path("scores.csv"),
    csv_lock: threading.Lock | None = None,
) -> None:
    if csv_lock:
        csv_lock.acquire()

    try:
        with open(output_path, "a", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=CSV_FIELDNAMES)

            if submission.score is None:
                status_info = (
                    "Failed" if submission.status == Status.FAILED else "No Score"
                )
                writer.writerow(
                    {
                        "Name": submission.name,
                        "Criteria": "",
                        "Level": "",
                        "Score": "",
                        "Feedback": status_info,
                    }
                )
                return

            for criteria_score in submission.score.criteria_scores:
                writer.writerow(
                    {
                        "Name": submission.name,
                        "Criteria": criteria_score.criteria_name,
                        "Level": criteria_score.level_definition,
                        "Score": format_decimal(criteria_score.score),
                        "Feedback": criteria_score.feedback,
                    }
                )

            if submission.score.overall_feedback:
                writer.writerow(
                    {
                        "Name": submission.name,
                        "Criteria": "",
                        "Level": "",
                        "Score": "",
                        "Feedback": submission.score.overall_feedback,
                    }
                )
    finally:
        if csv_lock:
            csv_lock.release()


def validate_paths(
    assignment_description: Path, rubric_path: Path, submissions_folder: Path
):
    if not rubric_path.exists():
        print_error(f"Rubric path '{rubric_path}' does not exist.")
        raise typer.Exit(1)

    if not rubric_path.is_file():
        print_error(f"Rubric path '{rubric_path}' is not a file.")
        raise typer.Exit(1)

    if not assignment_description.exists():
        print_error(
            f"Assignment description path '{assignment_description}' does not exist."
        )
        raise typer.Exit(1)

    if not assignment_description.is_file():
        print_error(
            f"Assignment description path '{assignment_description}' is not a file."
        )
        raise typer.Exit(1)

    if not submissions_folder.exists():
        print_error(f"Submissions folder path '{submissions_folder}' does not exist.")
        raise typer.Exit(1)

    if not submissions_folder.is_dir():
        print_error(
            f"Submissions folder path '{submissions_folder}' is not a directory."
        )
        raise typer.Exit(1)


def create_failed_submission(
    submission_folder: str, submission_name: str
) -> Submission:
    name = extract_name(submission_name)
    return Submission(
        name=name,
        folder_path=submission_folder,
        files=[],
        status=Status.FAILED,
        score=None,
    )


def process_single_submission(
    submission_folder: str,
    rubric: Rubric,
    assignment_description_content: str,
    csv_output_path: Path,
    logger: logging.Logger,
    csv_lock: threading.Lock,
) -> Submission:
    submission_name = Path(submission_folder).name
    submission = None
    submission_dict = None
    criteria_scores = None

    try:
        try:
            prepare_result = prepare_submission.delay(submission_folder)
            submission_dict = prepare_result.get()
            submission = Submission.model_validate(submission_dict)
        except Exception as e:
            logger.error(e)
            raise

        if submission:
            try:
                criteria_dicts = [criteria.model_dump() for criteria in rubric.criteria]
                batched_scoring_task = score_submission_batched.delay(
                    criteria_dicts,
                    assignment_description_content,
                    submission_dict,
                )
                batched_result = batched_scoring_task.get()

                criteria_scores = [
                    CriteriaScore.model_validate(score_dict)
                    for score_dict in batched_result["criteria_scores"]
                ]
                overall_feedback = batched_result["overall_feedback"]

            except Exception as e:
                logger.error(e)
                submission.status = Status.FAILED
                return submission

            if criteria_scores:
                total_score = sum(score.score for score in criteria_scores)

                submission.score = SubmissionScore(
                    total_score=total_score,
                    criteria_scores=criteria_scores,
                    overall_feedback=overall_feedback,
                )
                submission.status = Status.SCORED

                logger.info(f"{submission.name}: {format_decimal(total_score)} points")

                for score in criteria_scores:
                    logger.info(
                        f"* {score.criteria_name}: {format_decimal(score.score)} points"
                    )
                    logger.info(f"  * {score.level_definition}")
                    if score.feedback:
                        logger.info(f"  * Feedback: {score.feedback}")

                if overall_feedback:
                    logger.info(f"* Overall Feedback: {overall_feedback}")

        return submission

    except Exception as e:
        logger.error(e)
        return create_failed_submission(submission_folder, submission_name)


def main(
    assignment_description: Path = typer.Argument(
        ..., help="Path to the assignment description file"
    ),
    rubric_path: Path = typer.Argument(..., help="Path to the rubric file"),
    submissions_folder: Path = typer.Argument(
        ..., help="Path to the folder containing submissions"
    ),
):
    validate_paths(assignment_description, rubric_path, submissions_folder)

    rubric = load_rubric(rubric_path)
    print_rubric(rubric)

    try:
        with open(assignment_description, "r", encoding="utf-8") as f:
            assignment_description_content = f.read().strip()
    except Exception as e:
        print_error(f"Error reading assignment description: {e}")
        raise typer.Exit(1)

    if not assignment_description_content:
        print_error("Assignment description is empty.")
        raise typer.Exit(1)

    submission_folders = get_submissions(str(submissions_folder))

    if not submission_folders:
        print_error("No submissions found.")
        raise typer.Exit(1)

    progress_columns = [
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
    ]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_output_path = Path(f"scores_{timestamp}.csv")
    initialize_csv(csv_output_path)

    all_submissions: List[Submission] = []
    csv_lock = threading.Lock()

    with Progress(*progress_columns, transient=False) as progress:
        configure_rich_progress_logging(progress.console)
        logger = logging.getLogger(__name__)

        task = progress.add_task(
            "Scoring Submissions",
            total=len(submission_folders),
        )

        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_folder = {
                executor.submit(
                    process_single_submission,
                    submission_folder,
                    rubric,
                    assignment_description_content,
                    csv_output_path,
                    logger,
                    csv_lock,
                ): submission_folder
                for submission_folder in submission_folders
            }

            for future in as_completed(future_to_folder):
                try:
                    submission = future.result()
                    all_submissions.append(submission)
                    append_submission_to_csv(submission, csv_output_path, csv_lock)
                    progress.update(task, advance=1)

                except Exception as e:
                    submission_folder = future_to_folder[future]
                    submission_name = Path(submission_folder).name
                    failed_submission = create_failed_submission(
                        submission_folder, submission_name
                    )
                    all_submissions.append(failed_submission)
                    append_submission_to_csv(
                        failed_submission, csv_output_path, csv_lock
                    )
                    logger.error(f"Error processing {submission_name}: {e}")
                    progress.update(task, advance=1)


typer.run(main)
