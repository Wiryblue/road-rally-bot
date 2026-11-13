import asyncio

from config import get_gclient
from database import cursor, db


def _fetch_rows(sheet_name: str):
    # Blocking network call; always run it off the event loop.
    return get_gclient().open(sheet_name).sheet1.get_all_records()


async def load_tasks_from_sheet(sheet_name: str) -> tuple[int, int, str | None]:
    """Import tasks from a Google Sheet. Returns (added, updated, error_message).

    Tasks are matched on (Location, Description), so re-loading a sheet updates points/judge
    for existing tasks instead of duplicating them, and task IDs stay stable.
    """
    try:
        rows = await asyncio.to_thread(_fetch_rows, sheet_name)
    except Exception as e:
        print(f"Error loading tasks: {e}")
        return 0, 0, f"Couldn't open the sheet ({type(e).__name__}). Check the name and that it's shared with the service account."

    # Validate everything first so a bad row doesn't leave a half-imported sheet.
    tasks = []
    for row_number, task in enumerate(rows, start=2):
        if not str(task.get("Description", "")).strip():
            continue  # skip blank rows
        try:
            tasks.append((int(task["Location"]), str(task["Description"]).strip(), int(task["Points"]), int(task.get("Judge") or 0)))
        except (KeyError, ValueError) as e:
            return 0, 0, f"Row {row_number} is invalid ({e}). Columns needed: Location, Description, Points, Judge."

    added = updated = 0
    for location, description, points, judge in tasks:
        cursor.execute("SELECT id FROM tasks WHERE location = ? AND description = ?", (location, description))
        existing = cursor.fetchone()
        if existing:
            cursor.execute("UPDATE tasks SET points = ?, judge = ? WHERE id = ?", (points, judge, existing[0]))
            updated += 1
        else:
            cursor.execute(
                "INSERT INTO tasks (location, description, points, judge) VALUES (?, ?, ?, ?)",
                (location, description, points, judge),
            )
            added += 1
    db.commit()
    return added, updated, None
