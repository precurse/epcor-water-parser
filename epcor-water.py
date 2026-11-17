#!/usr/bin/env python3
"""Calculate water stats from EPCOR (Edmonton) water reports.

Designed to be debuggable from a CI log:
  * Every HTTP request is logged (URL, status, redirects, timing, size).
  * A full DEBUG log plus the raw HTML / PDF / extracted text are written to
    --debug-dir so they can be uploaded with actions/upload-artifact.
  * Failures raise typed errors with distinct exit codes and emit GitHub
    ``::error::`` annotations and a Markdown job summary.
  * DEBUG output is turned on automatically when the run is re-run with
    "Enable debug logging" (RUNNER_DEBUG=1) or with --debug.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import logging
import os
import platform
import re
import sys
import time
import traceback
from dataclasses import dataclass, fields
from decimal import Decimal, ROUND_UP
from importlib import metadata
from io import BytesIO
from pathlib import Path
from typing import Optional

import pdfplumber
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("water")

DAILY_URL = "https://apps.epcor.ca/DailyWaterQuality/Default.aspx?zone={zone}"
PDF_URL = (
    "https://www.epcor.com/content/dam/epcor/documents/water-quality-reports/"
    "{year_mon}-edmonton-water-quality-monthly-summary.pdf"
)
PDF_SECTION_MARKER = "2.1.1 SUMMARY OF PARAMETERS"
REQUEST_TIMEOUT = (5, 30)  # (connect, read) seconds
USER_AGENT = "epcor-water-report/2.0 (+https://github.com/)"

# PDF row label -> WaterReport attribute
PDF_ROW_LABELS = {
    "total hardness": "total_hardness",
    "calcium hardness": "calcium_hardness",
    "sulphate dissolved": "sulphate",
    "chloride dissolved": "chloride",
    "sodium": "sodium",
}
# Which numeric column on a PDF row holds the monthly average (0-based).
PDF_VALUE_COLUMN = 1
NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")

IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


# --------------------------------------------------------------------------- #
# Errors (each has its own exit code so the failing stage is obvious in CI)
# --------------------------------------------------------------------------- #
class WaterReportError(Exception):
    exit_code = 1
    title = "Water report failed"


class DailyDataError(WaterReportError):
    exit_code = 2
    title = "Could not get EPCOR daily data"


class PdfDownloadError(WaterReportError):
    exit_code = 3
    title = "Could not download EPCOR monthly PDF"


class PdfParseError(WaterReportError):
    exit_code = 4
    title = "Could not parse EPCOR monthly PDF"


class ValidationError(WaterReportError):
    exit_code = 5
    title = "Water report values failed validation"


# --------------------------------------------------------------------------- #
# GitHub Actions helpers
# --------------------------------------------------------------------------- #
def _gha_escape(value: str, prop: bool = False) -> str:
    value = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if prop:
        value = value.replace(":", "%3A").replace(",", "%2C")
    return value


def gha_error(message: str, title: Optional[str] = None) -> None:
    if IN_ACTIONS:
        t = f",title={_gha_escape(title, prop=True)}" if title else ""
        print(f"::error{t}::{_gha_escape(message)}", flush=True)


def gha_warning(message: str) -> None:
    if IN_ACTIONS:
        print(f"::warning::{_gha_escape(message)}", flush=True)


@contextlib.contextmanager
def gha_group(name: str):
    if IN_ACTIONS:
        print(f"::group::{name}", flush=True)
    try:
        yield
    finally:
        if IN_ACTIONS:
            print("::endgroup::", flush=True)


def write_step_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")
    except OSError as exc:  # never let summary writing mask the real error
        log.warning("Could not write step summary: %s", exc)


# --------------------------------------------------------------------------- #
# Logging + debug artifacts
# --------------------------------------------------------------------------- #
class DebugDir:
    """Collects raw responses and logs for upload as a CI artifact."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)

    def save(self, name: str, data) -> None:
        target = self.path / name
        try:
            if isinstance(data, str):
                target.write_text(data, encoding="utf-8")
            else:
                target.write_bytes(data)
            log.debug("Saved debug file %s (%d bytes)", target, target.stat().st_size)
        except OSError as exc:
            log.warning("Could not save debug file %s: %s", target, exc)

    def files(self):
        return sorted(p.name for p in self.path.iterdir() if p.is_file())


def setup_logging(debug_dir: DebugDir, verbose: bool) -> Path:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    # stdout (not stderr) so ordering with ::group:: lines stays correct.
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    root.addHandler(console)

    # The file always gets full DEBUG detail, even on a normal run.
    log_path = debug_dir.path / "run.log"
    fh = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(funcName)s:%(lineno)d  %(message)s")
    )
    root.addHandler(fh)

    # Keep third-party chatter out of the console unless verbose.
    for noisy in ("urllib3", "pdfminer", "pdfplumber", "PIL", "chardet", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if verbose else logging.WARNING)
    return log_path


def log_environment(args: argparse.Namespace) -> None:
    def ver(pkg: str) -> str:
        try:
            return metadata.version(pkg)
        except metadata.PackageNotFoundError:
            return "not installed"

    log.info("python=%s platform=%s", sys.version.split()[0], platform.platform())
    log.info(
        "requests=%s beautifulsoup4=%s pdfplumber=%s lxml=%s",
        ver("requests"), ver("beautifulsoup4"), ver("pdfplumber"), ver("lxml"),
    )
    log.info("args=%s", vars(args))
    log.info("today=%s (UTC now %s)", datetime.date.today(), datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
@dataclass
class WaterReport:
    ph: Optional[Decimal] = None
    alkalinity: Optional[Decimal] = None
    calcium_hardness: Optional[Decimal] = None
    total_hardness: Optional[Decimal] = None
    chloride: Optional[Decimal] = None
    sodium: Optional[Decimal] = None
    sulphate: Optional[Decimal] = None
    # provenance, handy when debugging stale data
    daily_date: Optional[str] = None
    alkalinity_source: Optional[str] = None
    pdf_month: Optional[str] = None

    def missing(self) -> list[str]:
        numeric = [f.name for f in fields(self) if f.name not in ("daily_date", "alkalinity_source", "pdf_month")]
        return [name for name in numeric if getattr(self, name) is None]

    @property
    def calcium(self) -> Decimal:
        return (self.calcium_hardness * Decimal("0.4")).quantize(Decimal("0.1"), rounding=ROUND_UP)

    @property
    def magnesium(self) -> Decimal:
        return (self.total_hardness - self.calcium_hardness) / Decimal("4")

    @property
    def bicarbonate(self) -> Decimal:
        return (self.alkalinity / Decimal("50")) * Decimal("61")

    def get_influxdb(self):
        # line protocol: measurement,tag=foo value=bar ts
        raise NotImplementedError


def validate(report: WaterReport) -> None:
    """Fail loudly instead of printing None / crashing on arithmetic later."""
    missing = report.missing()
    if missing:
        raise ValidationError(
            f"Missing values after scraping: {', '.join(missing)}. "
            "See pdf_section_*.txt / daily.html in the debug artifact to see what was parsed."
        )

    problems = []
    if not Decimal("5") <= report.ph <= Decimal("10"):
        problems.append(f"pH {report.ph} outside plausible range 5-10")
    if report.calcium_hardness > report.total_hardness:
        problems.append(
            f"calcium hardness ({report.calcium_hardness}) > total hardness ({report.total_hardness}); "
            "columns were probably misread"
        )
    for name in ("alkalinity", "calcium_hardness", "total_hardness", "chloride", "sodium", "sulphate"):
        if getattr(report, name) < 0:
            problems.append(f"{name} is negative ({getattr(report, name)})")
    if problems:
        raise ValidationError("Implausible values: " + "; ".join(problems))


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = USER_AGENT
    return session


def fetch(session: requests.Session, url: str, what: str, error_cls: type) -> requests.Response:
    """GET with full logging. Returns the response for any HTTP status;
    raises error_cls only for network-level failures."""
    log.info("GET %s  [%s]", url, what)
    start = time.monotonic()
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        elapsed = time.monotonic() - start
        raise error_cls(
            f"{what}: request to {url} failed after {elapsed:.1f}s: {type(exc).__name__}: {exc}"
        ) from exc

    elapsed = time.monotonic() - start
    log.info(
        "  -> HTTP %s in %.2fs, %d bytes, content-type=%s",
        resp.status_code, elapsed, len(resp.content), resp.headers.get("Content-Type"),
    )
    if resp.history:
        log.info("  redirects: %s -> %s", [r.status_code for r in resp.history], resp.url)
    log.debug("  response headers: %s", dict(resp.headers))
    return resp


# --------------------------------------------------------------------------- #
# Daily data (HTML)
# --------------------------------------------------------------------------- #
def _read_decimal(soup: BeautifulSoup, element_id: str) -> Decimal:
    el = soup.find(id=element_id)
    if el is None:
        raise LookupError(f"element id={element_id!r} not found")
    raw = el.get_text(strip=True)
    try:
        return Decimal(raw)
    except Exception:
        raise ValueError(f"element id={element_id!r} has non-numeric text {raw!r}") from None


def download_daily_data(session, report: WaterReport, zone: str, debug: DebugDir) -> None:
    url = DAILY_URL.format(zone=zone)
    resp = fetch(session, url, f"daily data zone={zone}", DailyDataError)
    debug.save("daily.html", resp.text)  # always keep it; it is small
    if resp.status_code != 200:
        raise DailyDataError(
            f"HTTP {resp.status_code} from {url}. Body starts with: {resp.text[:300]!r}"
        )

    soup = BeautifulSoup(resp.text, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else None

    def raw(element_id: str) -> str:
        el = soup.find(id=element_id)
        return el.get_text(strip=True) if el else "<missing>"

    # Discover day columns dynamically (…Label1 … …Label7), newest = highest number.
    ids = [el["id"] for el in soup.find_all(id=re.compile(r"^(ph|Alkalinity|Date)Label\d+$"))]
    suffixes = sorted({int(re.search(r"\d+$", i).group()) for i in ids}, reverse=True)
    if not suffixes:
        raise DailyDataError(
            f"No phLabelN/AlkalinityLabelN/DateLabelN elements on page (title={title!r}). "
            "The layout probably changed or an error/bot-block page was served; see daily.html."
        )

    # Show the whole table in the log so a failure is self-explanatory.
    for n in suffixes:
        log.info("  daily column %d: date=%s pH=%s alkalinity=%s",
                 n, raw(f"DateLabel{n}"), raw(f"phLabel{n}"), raw(f"AlkalinityLabel{n}"))

    def latest(prefix: str):
        """Most recent column whose value is numeric -> (value, date, suffix)."""
        for n in suffixes:
            try:
                return _read_decimal(soup, f"{prefix}{n}"), raw(f"DateLabel{n}"), n
            except (LookupError, ValueError):
                continue
        return None

    ph = latest("phLabel")
    if ph is None:
        raise DailyDataError(
            f"No numeric pH in any of {len(suffixes)} daily columns (title={title!r}); see daily.html."
        )
    report.ph, report.daily_date = ph[0], ph[1]
    log.info("pH=%s from %s (column %d)", ph[0], ph[1], ph[2])

    alk = latest("AlkalinityLabel")
    if alk is None:
        msg = (f"No numeric alkalinity in any of {len(suffixes)} daily columns; "
               "will fall back to the monthly PDF average")
        log.warning(msg)
        gha_warning(msg)
    else:
        report.alkalinity = alk[0]
        report.alkalinity_source = f"daily data {alk[1]}"
        log.info("alkalinity=%s from %s (column %d)", alk[0], alk[1], alk[2])


# --------------------------------------------------------------------------- #
# Monthly PDF
# --------------------------------------------------------------------------- #
def previous_month(n: int, today: Optional[datetime.date] = None) -> str:
    """Return 'YYYY-MM' for n calendar months before today."""
    today = today or datetime.date.today()
    year, month = today.year, today.month
    for _ in range(n):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return f"{year}-{month:02d}"


def parse_lines_from_pdf(pdf_file: BytesIO, debug: DebugDir, label: str) -> list[str]:
    page_texts: list[tuple[int, str]] = []
    with pdfplumber.open(pdf_file) as pdf:
        log.info("PDF has %d pages", len(pdf.pages))
        for page_no, page in enumerate(pdf.pages, 1):
            page_texts.append((page_no, page.extract_text() or ""))

    matches = [(n, t) for n, t in page_texts if PDF_SECTION_MARKER.lower() in t.lower()]
    log.info("Pages containing %r: %s", PDF_SECTION_MARKER, [n for n, _ in matches])

    if not matches:
        debug.save(
            f"pdf_all_text_{label}.txt",
            "\n\n".join(f"===== page {n} =====\n{t}" for n, t in page_texts),
        )
        raise PdfParseError(
            f"Section marker {PDF_SECTION_MARKER!r} not found on any of {len(page_texts)} pages. "
            f"Chars extracted per page: {[len(t) for _, t in page_texts]} "
            "(all zeros means a scanned/image PDF; otherwise the report format changed)."
        )

    # The first hit is normally the table of contents; the second is the real table.
    if len(matches) > 1:
        page_no, text = matches[1]
    else:
        page_no, text = matches[0]
        log.warning("Marker found on only one page (%d); it may be the table of contents.", page_no)

    debug.save(f"pdf_section_{label}.txt", text)
    log.info("Using page %d (%d lines)", page_no, text.count("\n") + 1)
    return text.split("\n")


def parse_values_from_pdf_lines(pdf_lines: list[str], report: WaterReport) -> None:
    found: dict[str, str] = {}
    labels = dict(PDF_ROW_LABELS)
    if report.alkalinity is None:  # daily page had no usable alkalinity
        labels["alkalinity"] = "alkalinity"
    for line in pdf_lines:
        lowered = line.strip().lower()
        for label, attr in labels.items():
            if not lowered.startswith(label):
                continue
            numbers = [tok for tok in lowered.split() if NUMBER_RE.match(tok)]
            log.debug("Row %r matched %r, numeric tokens=%s", line, label, numbers)
            if len(numbers) <= PDF_VALUE_COLUMN:
                log.warning(
                    "Row for %r has %d numeric columns, need at least %d: %r",
                    label, len(numbers), PDF_VALUE_COLUMN + 1, line,
                )
                continue
            if attr in found:
                log.warning("Duplicate row for %r (previous line %r); using the later one", label, found[attr])
            setattr(report, attr, Decimal(numbers[PDF_VALUE_COLUMN]))
            found[attr] = line
            log.info("PDF %s = %s", attr, numbers[PDF_VALUE_COLUMN])
            break

    not_found = [a for a in labels.values() if a not in found]
    if not_found:
        log.warning("Rows not found in PDF: %s", not_found)


def get_pdf_data(session, report: WaterReport, months_back: int, debug: DebugDir) -> None:
    attempts = []
    for i in range(1, months_back + 1):
        year_mon = previous_month(i)
        url = PDF_URL.format(year_mon=year_mon)
        resp = fetch(session, url, f"monthly PDF {year_mon}", PdfDownloadError)

        if resp.status_code != 200:
            attempts.append(f"{year_mon}: HTTP {resp.status_code} ({url})")
            continue
        if not resp.content.startswith(b"%PDF"):
            debug.save(f"monthly_{year_mon}_not_a_pdf.bin", resp.content)
            attempts.append(
                f"{year_mon}: HTTP 200 but body is not a PDF "
                f"(content-type={resp.headers.get('Content-Type')}, starts {resp.content[:40]!r})"
            )
            continue

        debug.save(f"monthly_{year_mon}.pdf", resp.content)
        log.info("Monthly data from %s", year_mon)
        try:
            lines = parse_lines_from_pdf(BytesIO(resp.content), debug, year_mon)
            parse_values_from_pdf_lines(lines, report)
        except WaterReportError:
            raise
        except Exception as exc:
            raise PdfParseError(f"{year_mon}: {type(exc).__name__}: {exc} (pdf saved to debug dir)") from exc
        report.pdf_month = year_mon
        if report.alkalinity is not None and report.alkalinity_source is None:
            report.alkalinity_source = f"monthly PDF {year_mon} (average, not daily)"
            log.warning("Alkalinity taken from %s", report.alkalinity_source)
        return

    raise PdfDownloadError(
        f"No monthly PDF found in the last {months_back} months:\n  " + "\n  ".join(attempts)
    )


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def report_lines(report: WaterReport) -> list[tuple[str, Decimal]]:
    return [
        ("pH", report.ph),
        ("Calcium (Ca)", report.calcium),
        ("Magnesium (Mg)", report.magnesium),
        ("Sodium (Na)", report.sodium),
        ("Bicarbonate (HCO3)", report.bicarbonate),
        ("Sulphate (SO4)", report.sulphate),
        ("Chloride (Cl)", report.chloride),
        ("Alkalinity (CaCO3)", report.alkalinity),
    ]


def tail(path: Path, n: int = 40) -> str:
    try:
        for h in logging.getLogger().handlers:
            h.flush()
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except OSError:
        return "(log unavailable)"


def failure_summary(title: str, message: str, tb: str, log_path: Path, debug: DebugDir) -> str:
    return (
        f"## :x: {title}\n\n"
        f"```\n{message}\n```\n\n"
        f"**Debug files** (upload `{debug.path}` as an artifact): {', '.join(debug.files()) or 'none'}\n\n"
        f"<details><summary>Traceback</summary>\n\n```\n{tb}\n```\n</details>\n\n"
        f"<details><summary>Last 40 log lines</summary>\n\n```\n{tail(log_path)}\n```\n</details>\n"
    )


def success_summary(report: WaterReport) -> str:
    rows = "\n".join(f"| {name} | {value} |" for name, value in report_lines(report))
    return (
        "## :droplet: EPCOR water report\n\n"
        f"pH from **{report.daily_date}**, alkalinity from **{report.alkalinity_source}**, "
        f"monthly data from **{report.pdf_month}**\n\n"
        f"| Parameter | Value |\n|---|---|\n{rows}\n"
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calculate water stats from EPCOR water reports")
    parser.add_argument("--zone", "-z", choices=["ELS", "Rossdale"], default="ELS")
    parser.add_argument("--months-back", type=int, default=4, help="how many previous months of PDFs to try")
    parser.add_argument("--debug", action="store_true", help="verbose console logging")
    parser.add_argument("--debug-dir", default="debug_artifacts", help="where logs/raw responses are written")
    parser.add_argument("--full", action="store_true", help="(deprecated, ignored)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    debug = DebugDir(args.debug_dir)
    verbose = args.debug or os.environ.get("RUNNER_DEBUG") == "1"
    log_path = setup_logging(debug, verbose)
    log_environment(args)

    report = WaterReport()
    try:
        session = make_session()
        with gha_group("Daily data"):
            download_daily_data(session, report, args.zone, debug)
        with gha_group("Monthly PDF"):
            get_pdf_data(session, report, args.months_back, debug)
        validate(report)
    except WaterReportError as exc:
        log.error("%s: %s", exc.title, exc, exc_info=True)
        gha_error(str(exc), title=exc.title)
        write_step_summary(failure_summary(exc.title, str(exc), traceback.format_exc(), log_path, debug))
        return exc.exit_code
    except Exception as exc:  # genuine bug: keep every detail we can
        log.exception("Unexpected error")
        gha_error(f"{type(exc).__name__}: {exc}", title="Unexpected error")
        log.error("Report state at failure: %s", report)
        write_step_summary(
            failure_summary("Unexpected error", f"{type(exc).__name__}: {exc}", traceback.format_exc(), log_path, debug)
        )
        return 1

    for name, value in report_lines(report):
        print(f"{name}: {value}")
    write_step_summary(success_summary(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
