"""Cohort data exports: CSV, XLSX, PDF report and ZIP archive."""
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cohort import Cohort
from app.models.engagement import (
    Activity,
    AdditionalWork,
    Announcement,
    Event,
    Issue,
    PeriodicReview,
)
from app.models.intern import Intern
from app.models.internship import Submission, Task, TaskAssignment
from app.models.user import Role, User
from app.utils import export_utils


def _fmt(dt: datetime | None) -> str:
    return dt.isoformat() if dt else ""


async def _cohort_rows(session: AsyncSession, cohort_id: str) -> dict:
    interns = list(
        (await session.execute(select(Intern).where(Intern.cohort_id == cohort_id)))
        .scalars()
        .all()
    )
    tasks = list(
        (await session.execute(select(Task).where(Task.cohort_id == cohort_id)))
        .scalars()
        .all()
    )
    assignments = list(
        (
            await session.execute(
                select(TaskAssignment)
                .join(Task, TaskAssignment.task_id == Task.id)
                .where(Task.cohort_id == cohort_id)
            )
        )
        .scalars()
        .all()
    )
    submissions = list(
        (
            await session.execute(
                select(Submission)
                .join(Task, Submission.task_id == Task.id)
                .where(Task.cohort_id == cohort_id)
            )
        )
        .scalars()
        .all()
    )
    issues = list(
        (await session.execute(select(Issue).where(Issue.cohort_id == cohort_id)))
        .scalars()
        .all()
    )
    activities = list(
        (await session.execute(select(Activity).where(Activity.cohort_id == cohort_id)))
        .scalars()
        .all()
    )
    additional = list(
        (
            await session.execute(
                select(AdditionalWork).where(AdditionalWork.cohort_id == cohort_id)
            )
        )
        .scalars()
        .all()
    )
    announcements = list(
        (
            await session.execute(
                select(Announcement).where(Announcement.cohort_id == cohort_id)
            )
        )
        .scalars()
        .all()
    )
    events = list(
        (await session.execute(select(Event).where(Event.cohort_id == cohort_id)))
        .scalars()
        .all()
    )
    reviews = list(
        (
            await session.execute(
                select(PeriodicReview).where(PeriodicReview.cohort_id == cohort_id)
            )
        )
        .scalars()
        .all()
    )
    return {
        "interns": interns,
        "tasks": tasks,
        "assignments": assignments,
        "submissions": submissions,
        "issues": issues,
        "activities": activities,
        "additional": additional,
        "announcements": announcements,
        "events": events,
        "reviews": reviews,
    }


def _interns_sheet(rows) -> tuple[list[str], list[list]]:
    return (
        ["intern_id", "cohort_id", "user_id", "full_name", "email", "team", "status", "joining_date"],
        [
            [r.id, r.cohort_id, r.user_id, r.full_name, r.email, r.team, r.status.value, str(r.joining_date or "")]
            for r in rows
        ],
    )


def _tasks_sheet(rows) -> tuple[list[str], list[list]]:
    return (
        ["task_id", "cohort_id", "title", "priority", "status", "deadline", "expected_hours"],
        [
            [r.id, r.cohort_id, r.title, r.priority.value, r.status.value, str(r.deadline or ""), r.expected_hours]
            for r in rows
        ],
    )


def _assignments_sheet(rows) -> tuple[list[str], list[list]]:
    return (
        ["assignment_id", "task_id", "intern_id", "status"],
        [[r.id, r.task_id, r.intern_id, r.status.value] for r in rows],
    )


def _submissions_sheet(rows) -> tuple[list[str], list[list]]:
    return (
        ["submission_id", "task_id", "intern_id", "status", "time_spent_minutes", "submitted_at", "revision_of"],
        [
            [r.id, r.task_id, r.intern_id, r.status.value, r.time_spent_minutes, _fmt(r.submitted_at), r.revision_of or ""]
            for r in rows
        ],
    )


def _issues_sheet(rows) -> tuple[list[str], list[list]]:
    return (
        ["issue_id", "cohort_id", "intern_id", "category", "severity", "status", "created_at"],
        [[r.id, r.cohort_id, r.intern_id, r.category.value, r.severity.value, r.status.value, _fmt(r.created_at)] for r in rows],
    )


def _activities_sheet(rows) -> tuple[list[str], list[list]]:
    return (
        ["activity_id", "cohort_id", "intern_id", "date", "time_spent_minutes", "description"],
        [[r.id, r.cohort_id, r.intern_id, str(r.activity_date), r.time_spent_minutes, r.description[:200]] for r in rows],
    )


def _additional_sheet(rows) -> tuple[list[str], list[list]]:
    return (
        ["work_id", "cohort_id", "intern_id", "title", "date", "hours", "impact"],
        [[r.id, r.cohort_id, r.intern_id, r.title, str(r.work_date), r.hours, r.impact[:200]] for r in rows],
    )


def _announcements_sheet(rows) -> tuple[list[str], list[list]]:
    return (
        ["announcement_id", "cohort_id", "title", "audience", "published_at"],
        [[r.id, r.cohort_id, r.title, r.audience, _fmt(r.published_at)] for r in rows],
    )


def _events_sheet(rows) -> tuple[list[str], list[list]]:
    return (
        ["event_id", "cohort_id", "title", "start_time", "location", "recurrence"],
        [[r.id, r.cohort_id, r.title, _fmt(r.start_time), r.location, r.recurrence_rule or ""] for r in rows],
    )


def _reviews_sheet(rows) -> tuple[list[str], list[list]]:
    return (
        ["review_id", "cohort_id", "intern_id", "type", "reviewer_id", "created_at"],
        [[r.id, r.cohort_id, r.intern_id, r.review_type, r.reviewer_id or "", _fmt(r.created_at)] for r in rows],
    )


SHEET_BUILDERS = {
    "Interns": (_interns_sheet, "interns"),
    "Tasks": (_tasks_sheet, "tasks"),
    "Assignments": (_assignments_sheet, "assignments"),
    "Submissions": (_submissions_sheet, "submissions"),
    "Issues": (_issues_sheet, "issues"),
    "Activities": (_activities_sheet, "activities"),
    "AdditionalWork": (_additional_sheet, "additional"),
    "Announcements": (_announcements_sheet, "announcements"),
    "Events": (_events_sheet, "events"),
    "Reviews": (_reviews_sheet, "reviews"),
}


def build_xlsx_export(data: dict) -> bytes:
    sheets = {}
    for name, (builder, key) in SHEET_BUILDERS.items():
        sheets[name] = builder(data[key])
    return export_utils.to_xlsx(sheets)


def csv_export(data: dict, dataset: str) -> bytes:
    builder, key = SHEET_BUILDERS[dataset]
    headers, rows = builder(data[key])
    return export_utils.to_csv(headers, rows)


def build_pdf_report(data: dict, cohort: Cohort) -> bytes:
    interns = data["interns"]
    tasks = data["tasks"]
    submissions = data["submissions"]
    issues = data["issues"]
    activities = data["activities"]
    completed = sum(1 for t in tasks if t.status.value == "COMPLETED")
    total_minutes = sum(a.time_spent_minutes for a in activities)
    open_issues = sum(1 for i in issues if i.status.value in ("OPEN", "IN_PROGRESS"))
    hours, minutes = divmod(total_minutes, 60)
    sections = [
        (
            "1. Programme Overview",
            [
                ("Cohort", cohort.name),
                ("Year", str(cohort.year)),
                ("Status", cohort.status.value),
                ("Period", f"{cohort.start_date or 'n/a'} to {cohort.end_date or 'n/a'}"),
            ],
        ),
        (
            "2. Intern Cohort",
            [("Total interns", str(len(interns)))]
            + [("Intern", i.full_name) for i in interns[:20]],
        ),
        (
            "3. Projects / Teams",
            [("Teams", ", ".join(sorted({i.team for i in interns if i.team})) or "None recorded")],
        ),
        (
            "4. Tasks and Deliverables",
            [("Total tasks", str(len(tasks))), ("Completed", str(completed))],
        ),
        (
            "5. Completion Statistics",
            [
                ("Completion rate", f"{(completed / len(tasks) * 100):.0f}%" if tasks else "n/a"),
                ("Submissions", str(len(submissions))),
            ],
        ),
        (
            "6. Activity / Time Summary",
            [("Total logged time", f"{hours}h {minutes}m"), ("Activity entries", str(len(activities)))],
        ),
        (
            "7. Issues and Challenges",
            [("Total issues", str(len(issues))), ("Open", str(open_issues))],
        ),
        (
            "8. Performance Review Summary",
            [("Periodic reviews", str(len(data["reviews"])))],
        ),
        (
            "9. Additional Contributions",
            [("Additional work records", str(len(data["additional"])))],
        ),
        (
            "10. Major Activities / Events",
            [("Events held", str(len(data["events"])))],
        ),
        (
            "11. Overall Observations",
            [("Note", "See periodic reviews and submission feedback for qualitative observations.")],
        ),
        (
            "12. Conclusion",
            [("Note", f"Report generated {datetime.now(UTC).isoformat()}")],
        ),
    ]
    return export_utils.build_pdf_report(cohort.name, cohort.year, sections)


def build_zip_archive(data: dict, cohort: Cohort) -> bytes:
    year = cohort.year
    entries = {
        f"{year}_internship_interns.xlsx": build_xlsx_export(data),
        f"{year}_internship_report.pdf": build_pdf_report(data, cohort),
        f"{year}_README.txt": (
            f"{cohort.name} ({year}) internship archive\n"
            "Generated from the Internship Management System.\n"
            "The database remains the primary source of truth.\n"
        ).encode(),
    }
    return export_utils.build_zip(entries)


async def get_cohort_export_data(session: AsyncSession, cohort_id: str) -> tuple:
    cohort = await session.get(Cohort, cohort_id)
    if cohort is None:
        raise HTTPException(status_code=404, detail="Cohort not found")
    data = await _cohort_rows(session, cohort_id)
    return cohort, data


def can_export(user: User) -> bool:
    return user.role in (Role.ADMIN, Role.SUPER_ADMIN)


EXPORT_FORMATS = ("xlsx", "csv", "pdf", "zip")
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def render(data: dict, cohort: Cohort, fmt: str, dataset: str | None = None) -> tuple[bytes, str, str]:
    """Build an export file. Returns ``(content, filename, content_type)``."""
    year = cohort.year
    if fmt == "xlsx":
        return build_xlsx_export(data), f"{year}_internship.xlsx", XLSX_TYPE
    if fmt == "csv":
        if dataset not in SHEET_BUILDERS:
            raise ValueError(f"Unknown dataset '{dataset}'")
        return csv_export(data, dataset), f"{year}_{dataset}.csv", "text/csv"
    if fmt == "pdf":
        return build_pdf_report(data, cohort), f"{year}_internship_report.pdf", "application/pdf"
    if fmt == "zip":
        return build_zip_archive(data, cohort), f"{year}_internship_archive.zip", "application/zip"
    raise ValueError(f"Unknown export format '{fmt}'")

