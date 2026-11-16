"""CSV bulk import for interns and tasks: template, preview (row-level report), commit.

Preview parses and validates every row without writing anything, and stores the file so
commit re-reads exactly what was previewed. Commit re-validates (the database may have
changed since the preview) and creates all valid rows in one transaction.

Imported values are stored as typed; spreadsheet formula injection is neutralised on
the way *out* by ``export_utils.neutralize``, so a hostile ``=HYPERLINK(...)`` name is
harmless when the cohort is later exported.
"""
import csv
import io
import json
import uuid
from typing import Any

from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import field_error
from app.models.cohort import Cohort
from app.models.intern import Intern, InternStatus
from app.models.job import Job
from app.models.user import User
from app.schemas.workflow import InternCreate, TaskCreate
from app.utils.storage import get_storage

KINDS = ("interns", "tasks")
MAX_REPORTED_ERRORS = 200
PREVIEW_ROWS = 20
PLACEHOLDER_PASSWORD = "imported-placeholder"  # satisfies schema; real hash comes from params

REQUIRED_COLUMNS = {"interns": ["full_name", "email"], "tasks": ["title"]}

TEMPLATES: dict[str, tuple[list[str], list[str]]] = {
    "interns": (
        ["full_name", "email", "team", "institution", "course", "phone", "joining_date", "end_date"],
        ["Ada Lovelace", "ada@example.com", "Platform", "Example University", "Computer Science",
         "+44 20 7946 0000", "2026-07-01", "2026-12-31"],
    ),
    "tasks": (
        ["title", "description", "priority", "start_date", "deadline", "expected_hours", "assignee_emails"],
        ["Write onboarding notes", "Summarise week one", "MEDIUM", "2026-07-01", "2026-07-08", "4",
         "ada@example.com;grace@example.com"],
    ),
}


def template_csv(kind: str) -> bytes:
    headers, example = TEMPLATES[kind]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    writer.writerow(example)
    return buffer.getvalue().encode("utf-8-sig")


def parse_csv(content: bytes, kind: str) -> list[dict[str, str]]:
    settings = get_settings()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise field_error("file", "File must be UTF-8 encoded CSV") from exc
    reader = csv.DictReader(io.StringIO(text))
    expected = TEMPLATES[kind][0]
    headers = [h.strip() for h in (reader.fieldnames or [])]
    missing = [h for h in REQUIRED_COLUMNS[kind] if h not in headers]
    if missing:
        raise field_error("file", f"Missing required column(s): {', '.join(missing)}. Download the template.")
    unknown = [h for h in headers if h not in expected]
    if unknown:
        raise field_error("file", f"Unknown column(s): {', '.join(unknown)}")
    rows: list[dict[str, str]] = []
    for raw in reader:
        if len(rows) >= settings.import_max_rows:
            raise field_error("file", f"Too many rows: the limit is {settings.import_max_rows} per file")
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if k}
        if any(row.values()):
            rows.append(row)
    if not rows:
        raise field_error("file", "The file has no data rows")
    return rows


def _pydantic_errors(row_no: int, exc: ValidationError) -> list[dict[str, Any]]:
    out = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "row"
        msg = err.get("msg", "Invalid value").removeprefix("Value error, ")
        if ": " in msg and loc == "row":
            loc, msg = msg.split(": ", 1)
        out.append({"row": row_no, "field": loc, "message": msg})
    return out


async def _validate(
    session: AsyncSession, kind: str, cohort: Cohort, rows: list[dict[str, str]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(valid_rows, errors)``; row numbers are 1-based data rows (header = row 1)."""
    valid: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if kind == "interns":
        emails = [r.get("email", "").lower() for r in rows if r.get("email")]
        existing: set[str] = set()
        for start in range(0, len(emails), 1000):
            chunk = emails[start : start + 1000]
            found = await session.execute(select(func.lower(User.email)).where(func.lower(User.email).in_(chunk)))
            existing.update(found.scalars().all())
        seen: set[str] = set()
        for index, row in enumerate(rows, start=2):
            data: dict[str, Any] = {
                k: (v or None) if k in ("joining_date", "end_date") else v for k, v in row.items()
            }
            try:
                parsed = InternCreate(cohort_id=cohort.id, password=PLACEHOLDER_PASSWORD, **data)
            except ValidationError as exc:
                errors.extend(_pydantic_errors(index, exc))
                continue
            email = parsed.email.lower()
            if email in seen:
                errors.append({"row": index, "field": "email", "message": "Duplicate email in this file"})
                continue
            if email in existing:
                errors.append({"row": index, "field": "email", "message": "A user with this email already exists"})
                continue
            seen.add(email)
            valid.append(parsed.model_dump(exclude={"password", "cohort_id"}))
    else:
        interns = await session.execute(
            select(Intern.id, func.lower(Intern.email)).where(
                Intern.cohort_id == cohort.id, Intern.status == InternStatus.ACTIVE
            )
        )
        by_email = {email: intern_id for intern_id, email in interns.all()}
        for index, row in enumerate(rows, start=2):
            assignees = [e.strip().lower() for e in row.pop("assignee_emails", "").replace(",", ";").split(";") if e.strip()]
            data = {
                k: (v or None) if k in ("start_date", "deadline") else v for k, v in row.items()
            }
            if not data.get("priority"):
                data.pop("priority", None)
            if not data.get("expected_hours"):
                data.pop("expected_hours", None)
            if data.get("priority"):
                data["priority"] = str(data["priority"]).upper()
            try:
                parsed_task = TaskCreate(cohort_id=cohort.id, **data)
            except ValidationError as exc:
                errors.extend(_pydantic_errors(index, exc))
                continue
            unknown = [e for e in assignees if e not in by_email]
            if unknown:
                errors.append({
                    "row": index,
                    "field": "assignee_emails",
                    "message": f"No active intern in this cohort with email: {', '.join(unknown[:5])}",
                })
                continue
            record = parsed_task.model_dump(exclude={"cohort_id", "intern_ids"})
            record["intern_ids"] = [by_email[e] for e in assignees]
            valid.append(record)
    return valid, errors


async def preview(
    session: AsyncSession,
    user: User,
    kind: str,
    cohort: Cohort,
    content: bytes,
    filename: str,
    password_hash: str | None,
) -> Job:
    rows = parse_csv(content, kind)
    valid, errors = await _validate(session, kind, cohort, rows)
    key = f"imports/{cohort.organization_id}/{cohort.id}/{uuid.uuid4().hex}.csv"
    await get_storage().put(key, content, "text/csv")
    params: dict[str, Any] = {"kind": kind, "cohort_id": cohort.id, "file_key": key, "filename": filename, "stage": "preview"}
    if password_hash:
        params["password_hash"] = password_hash
    job = Job(
        user_id=user.id,
        organization_id=cohort.organization_id,
        kind="import",
        params_json=json.dumps(params),
        result_json=json.dumps({
            "total_rows": len(rows),
            "valid_rows": len(valid),
            "error_count": len(errors),
            "errors": errors[:MAX_REPORTED_ERRORS],
            "preview": jsonable_encoder(valid[:PREVIEW_ROWS]),
        }),
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def commit_import(session: AsyncSession, job: Job) -> dict[str, Any]:
    from app.services import access, intern_service, task_service

    params = json.loads(job.params_json)
    kind = params["kind"]
    actor = await session.get(User, job.user_id)
    if actor is None or not actor.is_active:
        raise RuntimeError("The user who started this import no longer exists")
    cohort = await access.load_cohort(session, actor, params["cohort_id"], write=True)
    content = await get_storage().get(params["file_key"])
    rows = parse_csv(content, kind)
    valid, errors = await _validate(session, kind, cohort, rows)
    if errors and not params.get("skip_invalid"):
        raise RuntimeError(f"{len(errors)} row(s) became invalid since the preview; preview again")
    created = 0
    if kind == "interns":
        password_hash = params.get("password_hash")
        if not password_hash:
            raise RuntimeError("Missing initial password for imported interns")
        for record in valid:
            await intern_service.create_intern(
                session,
                actor,
                cohort_id=cohort.id,
                password=PLACEHOLDER_PASSWORD,
                password_hash=password_hash,
                commit=False,
                **record,
            )
            created += 1
    else:
        for record in valid:
            await task_service.create_task(session, actor, cohort_id=cohort.id, commit=False, **record)
            created += 1
    await session.commit()
    return {
        "created": created,
        "skipped": len(errors),
        "errors": errors[:MAX_REPORTED_ERRORS],
        "kind": kind,
    }


def stats_from(job: Job) -> dict[str, Any]:
    return json.loads(job.result_json or "{}")


def ensure_kind(kind: str) -> None:
    if kind not in KINDS:
        raise field_error("kind", f"Unknown import type '{kind}'", 404)
