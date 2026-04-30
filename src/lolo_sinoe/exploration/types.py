"""Data types for the exploration phase."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class LinkInfo:
    href: str
    text: str
    is_internal: bool


@dataclass
class FormInfo:
    action: str
    method: str
    field_names: list[str]


@dataclass
class DownloadInfo:
    url: str
    text: str
    inferred_kind: str  # pdf | doc | unknown


@dataclass
class TableInfo:
    headers: list[str]
    row_count: int
    sample_first_row: list[str] = field(default_factory=list)


@dataclass
class VisitedPage:
    url: str
    title: str
    reached_from: str | None
    depth: int
    visited_at: datetime
    html_path: Path
    screenshot_path: Path
    network_log_path: Path
    forms_found: list[FormInfo] = field(default_factory=list)
    links_found: list[LinkInfo] = field(default_factory=list)
    downloadables_found: list[DownloadInfo] = field(default_factory=list)
    tables_found: list[TableInfo] = field(default_factory=list)
    jsf_components: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
