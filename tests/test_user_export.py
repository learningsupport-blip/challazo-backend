"""Student Management CSV: the same filters as the user list, every matching row."""

import csv
import io
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.models.user_model import CurrentUser
from app.services.user_service import USER_EXPORT_HEADERS, UserService
from app.utils.time import IST


def _user(
    user_id: str,
    *,
    name: str,
    email: str,
    role: str = "student",
    created_at: str,
    **extra: object,
) -> dict:
    return {
        "id": user_id,
        "name": name,
        "email": email,
        "role": role,
        "created_at": created_at,
        "approval_status": "approved",
        **extra,
    }


def _service(students: list[dict]) -> UserService:
    firebase = MagicMock()
    firebase.query_collection.return_value = students
    return UserService(firebase=firebase)


def _rows(content: bytes) -> list[list[str]]:
    assert content.startswith(b"\xef\xbb\xbf")
    return list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))


def _college_document() -> dict:
    return {"name": "College A", "location": "Hyd"}


@contextmanager
def _client(
    service: UserService | None = None,
    *,
    as_user: CurrentUser | None = None,
    anonymous: bool = False,
):
    from app.dependencies import get_firebase, get_user_service
    from app.main import app
    from app.middleware.auth_middleware import get_admin_user, get_current_user

    admin = CurrentUser(user_id="admin-1", email="admin@example.com", role="admin", name="Admin")
    if anonymous:
        app.dependency_overrides[get_firebase] = lambda: MagicMock()
        app.dependency_overrides[get_user_service] = lambda: MagicMock()
    elif as_user is None:
        app.dependency_overrides[get_admin_user] = lambda: admin
    else:
        app.dependency_overrides[get_current_user] = lambda: as_user
    if service is not None and not anonymous:
        app.dependency_overrides[get_user_service] = lambda: service
    try:
        with (
            patch("app.main.DatabaseSeeder") as seeder_cls,
            patch("app.dependencies.init_app_container", return_value=MagicMock()),
        ):
            seeder_cls.return_value.seed_all.return_value = True
            yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_export_matches_every_page_for_one_college():
    students = [
        _user(
            f"s{index}",
            name=f"Student {index}",
            email=f"s{index}@example.com",
            created_at=f"2026-08-{index + 1:02d}T10:00:00+05:30",
            university_id="uni-a" if index < 3 else "uni-b",
        )
        for index in range(4)
    ]
    service = _service(students)
    service.firebase.get_document.return_value = _college_document()

    collected: list[dict] = []
    for page_number in (1, 2):
        page = service.paginate_users(
            page=page_number,
            page_size=2,
            role="student",
            university_id="uni-a",
        )
        collected.extend(page["items"])

    content, _filename = service.export_users_csv(role="student", university_id="uni-a")
    rows = _rows(content)

    assert page["total"] == 3
    assert rows[0] == list(USER_EXPORT_HEADERS)
    assert [row[0] for row in rows[1:]] == [item["id"] for item in collected]
    assert len(rows) == 4


def test_search_and_college_filter_combine_and_no_filter_exports_every_student():
    students = [
        _user(
            "ada-a",
            name="Ada",
            email="ada@example.com",
            created_at="2026-08-02T10:00:00+05:30",
            university_id="uni-a",
        ),
        _user(
            "bea-a",
            name="Bea",
            email="bea@example.com",
            created_at="2026-08-03T10:00:00+05:30",
            university_id="uni-a",
        ),
        _user(
            "ada-b",
            name="Ada Other",
            email="other@example.com",
            created_at="2026-08-04T10:00:00+05:30",
            university_id="uni-b",
        ),
    ]
    service = _service(students)
    service.firebase.get_document.return_value = _college_document()

    filtered, _filename = service.export_users_csv(role="student", q="ada", university_id="uni-a")
    everyone, filename = service.export_users_csv()

    assert [row[0] for row in _rows(filtered)[1:]] == ["ada-a"]
    assert [row[0] for row in _rows(everyone)[1:]] == ["ada-b", "bea-a", "ada-a"]
    assert "-all-" in filename
    service.firebase.query_collection.assert_called_with("users", "role", "==", "student")


def test_filename_uses_the_college_slug_and_export_time():
    service = _service(
        [
            _user(
                "s1",
                name="Ada",
                email="ada@example.com",
                created_at="2026-08-01T10:00:00+05:30",
                university_id="uni-a",
            )
        ]
    )
    service.firebase.get_document.return_value = _college_document()
    frozen = datetime(2026, 11, 18, 14, 40, tzinfo=IST)

    with patch("app.services.user_service.now_ist", return_value=frozen):
        _content, filename = service.export_users_csv(role="student", university_id="uni-a")

    assert filename == "students-college-a-hyd-20261118-1440.csv"


def test_formula_cells_are_quoted_and_non_ascii_names_round_trip():
    service = _service(
        [
            _user(
                "s1",
                name="=Ada",
                first_name="José",
                email="ada@example.com",
                created_at="2026-08-01T10:00:00+05:30",
            )
        ]
    )

    content, _filename = service.export_users_csv()
    row = _rows(content)[1]

    assert content.startswith(b"\xef\xbb\xbf")
    assert row[1] == "José"
    assert row[3] == "'=Ada"


def test_optional_fields_dates_and_team_members_are_formatted():
    service = _service(
        [
            _user(
                "full",
                name="Ada Lovelace",
                first_name="Ada",
                last_name="Lovelace",
                email="ada@example.com",
                created_at="2026-09-23T15:45:00+05:30",
                updated_at="2026-09-24T08:05:00+05:30",
                mobile_no="+919876543210",
                college_type="NIAT",
                niat_id="N1",
                college_id="C1",
                university="College A",
                university_id="uni-a",
                university_location="Hyd",
                batch_name="2024-2028",
                batch_id="batch-1",
                team_name="Atlas",
                team_leader_name="Ada",
                team_members=[
                    {"name": "Bea", "email": "bea@example.com"},
                    {"name": "Cy", "email": "cy@example.com"},
                ],
                email_verified=True,
                phone_verified=False,
            ),
            _user(
                "sparse",
                name="Sparse",
                email="sparse@example.com",
                created_at="2026-08-01T10:00:00+05:30",
            ),
        ]
    )

    rows = _rows(service.export_users_csv()[0])
    full = rows[1]
    sparse = rows[2]

    assert full[5].startswith("'+")
    assert full[16] == "Bea <bea@example.com>; Cy <cy@example.com>"
    assert full[18] == "Yes"
    assert full[19] == "No"
    assert full[20] == "2026-09-23 15:45"
    assert full[21] == "2026-09-24 08:05"
    assert sparse[1] == ""
    assert sparse[5] == ""
    assert sparse[16] == ""
    assert sparse[17] == "approved"
    assert sparse[18] == ""
    assert sparse[19] == ""
    assert sparse[21] == ""


def test_unknown_university_is_400_and_non_admins_are_rejected():
    service = _service([])
    service.firebase.get_document.return_value = None
    student = CurrentUser(
        user_id="student-1",
        email="student@example.com",
        role="student",
        name="Student",
    )

    with _client(service) as client:
        unknown = client.get("/admin/users/export", params={"university_id": "missing"})
        downloaded = client.get("/admin/users/export")
    with _client(as_user=student) as client:
        forbidden = client.get("/admin/users/export")
    with _client(anonymous=True) as client:
        anonymous = client.get("/admin/users/export")

    assert unknown.status_code == 400
    assert unknown.json()["detail"] == "Unknown university"
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "text/csv; charset=utf-8"
    assert 'filename="students-all-' in downloaded.headers["content-disposition"]
    assert forbidden.status_code == 403
    assert anonymous.status_code == 401
