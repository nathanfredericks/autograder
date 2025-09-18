from pathlib import Path
from typing import List

from utils import print_error


def get_submissions(submissions_folder: str) -> List[str]:
    submissions_path = Path(submissions_folder)
    submission_folders: List[str] = []

    if not submissions_path.exists():
        print_error(f"Submissions folder '{submissions_folder}' does not exist.")
        return submission_folders

    for folder in submissions_path.iterdir():
        if folder.is_dir() and "_assignsubmission_file" in folder.name:
            submission_folders.append(str(folder))

    submission_folders.sort(key=lambda x: Path(x).name.lower())

    return submission_folders
