"""Student Management: one page of students, with search, without changing the full list."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.models.user_model import CurrentUser
from app.services.user_service import UserService


def _user(
    user_id: str,
    *,
    name: str,
    email: str,
    role: str = "student",
    created_at: str,
    niat_id: str | None = None,
    mobile_no: str | None = None,
    university_id: str | None = None,
) -> dict:
    return {
        "id": user_id,
        "name": name,
        "email": email,
        "role": role,
        "created_at": created_at,
        "niat_id": niat_id,
        "mobile_no": mobile_no,
        "university_id": university_id,
        "approval_status": "approved",
    }


def _service(students: list[dict], everyone: list[dict] | None = None) -> UserService:
    firebase = MagicMock()
    firebase.query_collection.return_value = students
    firebase.get_collection.return_value = everyone if everyone is not None else students
    return UserService(firebase=firebase)


def test_overview_counts_students_and_the_newest_pending_evaluators():
    service = _service(
        [],
        everyone=[
            _user(
                "admin",
                name="Ada Admin",
                email="ada@example.com",
                role="admin",
                created_at="2026-01-01",
            ),
            _user("s1", name="Student", email="s@example.com", created_at="2026-08-01"),
            _user(
                "old",
                name="Old Pending",
                email="old@example.com",
                role="evaluator",
                created_at="2026-07-01",
            ),
            _user(
                "new",
                name="New Pending",
                email="new@example.com",
                role="evaluator",
                created_at="2026-09-01",
            ),
            _user(
                "ok",
                name="Approved",
                email="ok@example.com",
                role="evaluator",
                created_at="2026-08-01",
            ),
        ],
    )
    service.firebase.get_collection.return_value[-2]["approval_status"] = "pending"
    service.firebase.get_collection.return_value[-3]["approval_status"] = "pending"

    overview = service.overview(pending_limit=1)

    assert overview["student_count"] == 1
    assert overview["evaluator_count"] == 3
    assert overview["total_users"] == 4
    assert overview["pending_count"] == 2
    assert overview["pending_evaluators"] == [
        {"id": "new", "name": "New Pending", "email": "new@example.com"}
    ]


def test_student_page_is_fifteen_newest_first_and_skips_other_roles():
    students = [
        _user(
            f"s{i}",
            name=f"Student {i}",
            email=f"s{i}@example.com",
            created_at=f"2026-08-{i + 1:02d}T10:00:00",
        )
        for i in range(16)
    ]
    service = _service(students)

    page = service.paginate_users(page=1, page_size=15, role="student")

    assert page["total"] == 16
    assert page["page_size"] == 15
    assert len(page["items"]) == 15
    assert page["items"][0]["id"] == "s15"
    assert page["items"][-1]["id"] == "s1"
    service.firebase.query_collection.assert_called_once_with("users", "role", "==", "student")


def test_search_matches_name_email_and_niat_id():
    students = [
        _user(
            "a",
            name="Sanjay Chepuri",
            email="chepuri@example.com",
            created_at="2026-09-23",
            niat_id="N25KHUIHKUH",
        ),
        _user(
            "b",
            name="Vikas Akula",
            email="vikas@example.com",
            created_at="2026-08-28",
            niat_id="NW007364",
        ),
    ]
    service = _service(students)

    by_name = service.paginate_users(page=1, page_size=15, role="student", q="sanjay")
    by_id = service.paginate_users(page=1, page_size=15, role="student", q="nw007364")

    assert [item["id"] for item in by_name["items"]] == ["a"]
    assert [item["id"] for item in by_id["items"]] == ["b"]
    assert by_id["total"] == 1


def test_search_matches_mobile_with_or_without_country_code():
    students = [
        _user(
            "a",
            name="Ada",
            email="ada@example.com",
            created_at="2026-09-01",
            mobile_no="+919640198259",
        ),
        _user(
            "b",
            name="Grace",
            email="grace@example.com",
            created_at="2026-08-01",
            mobile_no="+918888877777",
        ),
    ]
    service = _service(students)

    for query in ("919640198259", "9640198259", "96401-98259", "98259"):
        page = service.paginate_users(page=1, page_size=15, role="student", q=query)
        assert [item["id"] for item in page["items"]] == ["a"]

    other = service.paginate_users(page=1, page_size=15, role="student", q="88888")
    assert [item["id"] for item in other["items"]] == ["b"]


def test_university_filter_keeps_one_college_and_counts_matches():
    students = [
        _user(
            "a",
            name="Ada",
            email="ada@example.com",
            created_at="2026-08-01",
            university_id="uni-a",
        ),
        _user(
            "b",
            name="Bea",
            email="bea@example.com",
            created_at="2026-08-02",
            university_id="uni-b",
        ),
        _user(
            "c",
            name="Cy",
            email="cy@example.com",
            created_at="2026-08-03",
            university_id="uni-a",
        ),
    ]
    service = _service(students)
    service.firebase.get_document.return_value = {"name": "College A", "location": "Hyd"}

    page = service.paginate_users(
        page=1,
        page_size=15,
        role="student",
        university_id="  uni-a  ",
    )

    assert page["total"] == 2
    assert [item["id"] for item in page["items"]] == ["c", "a"]
    service.firebase.get_document.assert_called_once_with("universities", "uni-a")


def test_university_filter_composes_with_search_and_pagination():
    students = [
        _user(
            "a",
            name="Ada",
            email="ada@example.com",
            created_at="2026-08-01",
            university_id="uni-a",
        ),
        _user(
            "b",
            name="Ada Two",
            email="ada2@example.com",
            created_at="2026-08-02",
            university_id="uni-a",
        ),
        _user(
            "c",
            name="Bea",
            email="bea@example.com",
            created_at="2026-08-03",
            university_id="uni-a",
        ),
        _user(
            "d",
            name="Ada Other",
            email="other@example.com",
            created_at="2026-08-04",
            university_id="uni-b",
        ),
    ]
    service = _service(students)
    service.firebase.get_document.return_value = {"name": "College A", "location": "Hyd"}

    searched = service.paginate_users(
        page=1,
        page_size=15,
        role="student",
        q="ada",
        university_id="uni-a",
    )
    second = service.paginate_users(
        page=2,
        page_size=1,
        role="student",
        university_id="uni-a",
    )

    assert searched["total"] == 2
    assert [item["id"] for item in searched["items"]] == ["b", "a"]
    assert second["total"] == 3
    assert [item["id"] for item in second["items"]] == ["b"]


def test_omitting_university_id_does_not_filter_or_look_up_a_college():
    students = [
        _user(
            "a",
            name="Ada",
            email="ada@example.com",
            created_at="2026-08-01",
            university_id="uni-a",
        ),
        _user(
            "b",
            name="Bea",
            email="bea@example.com",
            created_at="2026-08-02",
            university_id="uni-b",
        ),
    ]
    service = _service(students)

    page = service.paginate_users(page=1, page_size=15, role="student")

    assert page["total"] == 2
    assert [item["id"] for item in page["items"]] == ["b", "a"]
    service.firebase.get_document.assert_not_called()


@pytest.mark.parametrize("bad_id", ["", "   ", "uni/a", ".", ".."])
def test_malformed_university_id_is_rejected(bad_id: str):
    service = _service([])

    with pytest.raises(ValueError, match="Invalid university id"):
        service.paginate_users(page=1, page_size=15, role="student", university_id=bad_id)

    service.firebase.get_document.assert_not_called()
    service.firebase.query_collection.assert_not_called()


def test_unknown_university_id_is_rejected():
    service = _service([])
    service.firebase.get_document.return_value = None

    with pytest.raises(ValueError, match="Unknown university"):
        service.paginate_users(page=1, page_size=15, role="student", university_id="missing")

    service.firebase.get_document.assert_called_once_with("universities", "missing")
    service.firebase.query_collection.assert_not_called()


def test_page_past_the_end_is_empty():
    service = _service([_user("a", name="Ada", email="a@example.com", created_at="2026-08-01")])

    page = service.paginate_users(page=2, page_size=15, role="student")

    assert page["items"] == []
    assert page["total"] == 1


def test_page_query_returns_an_object_and_a_bare_get_stays_a_list():
    admin = CurrentUser(user_id="admin-1", email="admin@example.com", role="admin", name="Admin")
    student = _user(
        "s1",
        name="Sanjay",
        email="s@example.com",
        created_at="2026-09-23T10:00:00+05:30",
        niat_id="N1",
    )
    service = MagicMock()
    service.get_non_admin_users.return_value = [student]

    def _page(**kwargs: object) -> dict:
        if kwargs.get("university_id") == "missing":
            raise ValueError("Unknown university")
        return {
            "items": [student],
            "total": 20,
            "page": 1,
            "page_size": 15,
        }

    service.paginate_users.side_effect = _page
    service.to_user_response.side_effect = UserService.to_user_response

    from app.dependencies import get_user_service
    from app.main import app
    from app.middleware.auth_middleware import get_admin_user

    app.dependency_overrides[get_admin_user] = lambda: admin
    app.dependency_overrides[get_user_service] = lambda: service
    try:
        with (
            patch("app.main.DatabaseSeeder") as seeder_cls,
            patch("app.dependencies.init_app_container", return_value=MagicMock()),
        ):
            seeder_cls.return_value.seed_all.return_value = True
            client = TestClient(app)
            paged = client.get(
                "/admin/users",
                params={
                    "page": 1,
                    "page_size": 15,
                    "role": "student",
                    "q": "sanjay",
                    "university_id": "uni-niat",
                },
            )
            bare = client.get("/admin/users")
            unknown = client.get(
                "/admin/users",
                params={"page": 1, "university_id": "missing"},
            )
            too_big = client.get("/admin/users", params={"page": 1, "page_size": 101})
    finally:
        app.dependency_overrides.clear()

    assert paged.status_code == 200
    body = paged.json()
    assert body["total"] == 20
    assert body["page_size"] == 15
    assert body["items"][0]["id"] == "s1"
    assert body["items"][0]["role"] == "student"
    service.paginate_users.assert_any_call(
        page=1,
        page_size=15,
        role="student",
        q="sanjay",
        university_id="uni-niat",
    )
    assert isinstance(bare.json(), list)
    assert bare.json()[0]["id"] == "s1"
    assert unknown.status_code == 400
    assert unknown.json()["detail"] == "Unknown university"
    assert too_big.status_code == 422
