from config import gclient
from database import cursor, db

def load_tasks_from_sheet(sheet_name: str) -> bool:
    try:
        sheet = gclient.open(sheet_name).sheet1
        tasks = sheet.get_all_records()
        for task in tasks:
            location = task["Location"]
            description = task["Description"]
            points = task["Points"]
            judge = task["Judge"]
            cursor.execute(
                "INSERT INTO tasks (location, description, points, judge) VALUES (?, ?, ?, ?)",
                (location, description, points, judge),
            )
        db.commit()
        return True
    except Exception as e:
        print(f"Error loading tasks: {e}")
        return False
