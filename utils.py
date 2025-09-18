import logging
import re
from typing import Optional

from rich.console import Console
from rich.panel import Panel


def extract_name(folder_name: str) -> str:
    name_part = re.sub(r"_\d+_assignsubmission_file.*$", "", folder_name)
    return name_part.replace("_", " ")


def format_decimal(value: float) -> str:
    rounded = round(value, 2)
    formatted = f"{rounded:.2f}".rstrip("0").rstrip(".")

    return formatted


def print_panel(message: str, title: str = "Info", style: str = "blue") -> None:
    console = Console()
    console.print(
        Panel(
            message,
            title=f"[{style}]{title}[/{style}]",
            title_align="left",
            border_style=style,
        )
    )


def print_error(message: str) -> None:
    print_panel(message, "Error", "red")


class RichConsoleHandler(logging.Handler):
    def __init__(self, console: Optional[Console] = None):
        super().__init__()
        self.console = console or Console()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            self.console.print(message)
        except Exception:
            self.handleError(record)


def configure_rich_progress_logging(console: Console) -> None:
    console_handler = RichConsoleHandler(console)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)
    root_logger.setLevel(logging.INFO)
