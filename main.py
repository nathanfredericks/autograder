import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import List

import typer
from celery import group
from rich.progress import (BarColumn, MofNCompleteColumn, Progress,
                           TaskProgressColumn, TextColumn, TimeElapsedColumn,
                           TimeRemainingColumn)

from models import CriteriaScore, Status, Submission, SubmissionScore
from rubric import load_rubric, print_rubric
from submission import get_submissions
from tasks import generate_overall_feedback, prepare_submission, score_criteria
from utils import (configure_rich_progress_logging, extract_name,
                   format_decimal, print_error)


def initialize_csv(output_path: Path = Path("scores.csv")) -> None:
    fieldnames = ["Name", "Criteria", "Level", "Score", "Feedback"]

    with open(output_path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()


def append_submission_to_csv(
    submission: Submission, output_path: Path = Path("scores.csv")
) -> None:
    fieldnames = ["Name", "Criteria", "Level", "Score", "Feedback"]

    with open(output_path, "a", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

        if submission.score is None:
            status_info = "Failed" if submission.status == Status.FAILED else "No Score"
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

    with Progress(*progress_columns, transient=False) as progress:
        configure_rich_progress_logging(progress.console)
        logger = logging.getLogger(__name__)

        task = progress.add_task(
            "Scoring Submissions",
            total=len(submission_folders),
        )

        for submission_folder in submission_folders:
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
                    scoring_jobs = group(
                        score_criteria.s(
                            criteria.model_dump(),
                            assignment_description_content,
                            submission_dict,
                        )
                        for criteria in rubric.criteria
                    )

                    result_group = scoring_jobs.apply_async()

                    try:
                        criteria_scores_dicts = result_group.get()
                        criteria_scores = [
                            CriteriaScore.model_validate(score_dict)
                            for score_dict in criteria_scores_dicts
                        ]
                    except Exception as e:
                        logger.error(e)
                        submission.status = Status.FAILED
                        all_submissions.append(submission)
                        append_submission_to_csv(submission, csv_output_path)
                        progress.update(task, advance=1)
                        continue

                    if criteria_scores:
                        total_score = sum(score.score for score in criteria_scores)

                        try:
                            overall_feedback_task = generate_overall_feedback.delay(
                                assignment_description_content,
                                submission_dict,
                                criteria_scores_dicts,
                            )
                            overall_feedback = overall_feedback_task.get()
                        except Exception as e:
                            logger.error(e)
                            overall_feedback = None

                        submission.score = SubmissionScore(
                            total_score=total_score,
                            criteria_scores=criteria_scores,
                            overall_feedback=overall_feedback,
                        )
                        submission.status = Status.SCORED

                        all_submissions.append(submission)
                        append_submission_to_csv(submission, csv_output_path)

                        logger.info(
                            f"{submission.name}: {format_decimal(total_score)} points"
                        )

                        for score in criteria_scores:
                            logger.info(
                                f"* {score.criteria_name}: {format_decimal(score.score)} points"
                            )
                            logger.info(f"  * {score.level_definition}")
                            if score.feedback:
                                logger.info(f"  * Feedback: {score.feedback}")

                        if overall_feedback:
                            logger.info(f"* Overall Feedback: {overall_feedback}")

                progress.update(task, advance=1)

            except Exception as e:
                name = extract_name(submission_name)
                failed_submission = Submission(
                    name=name,
                    folder_path=submission_folder,
                    files=[],
                    status=Status.FAILED,
                    score=None,
                )
                all_submissions.append(failed_submission)

                append_submission_to_csv(failed_submission, csv_output_path)

                logger.error(e)
                progress.update(task, advance=1)


typer.run(main)
