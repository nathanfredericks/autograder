from pathlib import Path
from typing import List

import typer
import yaml
from pydantic import BaseModel, ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from utils import format_decimal, print_error


class Level(BaseModel):
    definition: str
    score: float


class Criteria(BaseModel):
    name: str
    levels: List[Level]


class Rubric(BaseModel):
    name: str
    criteria: List[Criteria]


def load_rubric(rubric_path: Path) -> Rubric:
    try:
        with open(rubric_path, "r") as f:
            rubric_data = yaml.safe_load(f)

        rubric = Rubric(**rubric_data)
        return rubric
    except ValidationError as e:
        print_error(f"Invalid rubric format: {e}")
        raise typer.Exit(1)
    except yaml.YAMLError as e:
        print_error(f"Invalid YAML in rubric file: {e}")
        raise typer.Exit(1)
    except Exception as e:
        print_error(f"Could not load rubric file: {e}")
        raise typer.Exit(1)


def print_rubric(rubric: Rubric) -> None:
    console = Console()

    max_levels = max(len(criteria.levels) for criteria in rubric.criteria)

    table = Table(show_lines=True, show_header=False)
    table.add_column(style="bold", overflow="fold")

    for _ in range(max_levels):
        table.add_column(overflow="fold")

    for criteria in rubric.criteria:
        sorted_levels = sorted(criteria.levels, key=lambda x: x.score)

        row_data = [criteria.name]
        for level in sorted_levels:
            level_text = f"{level.definition}\n[bold italic dark_green]{format_decimal(level.score)} points[/bold italic dark_green]"
            row_data.append(level_text)

        while len(row_data) <= max_levels:
            row_data.append("")

        table.add_row(*row_data)

    console.print(Panel(table, title="Rubric", title_align="left", border_style="blue"))
    console.print()
