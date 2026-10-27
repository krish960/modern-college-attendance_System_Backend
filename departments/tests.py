from core.models import AuditLog
from core.testing import BaseTestCase

from .models import Department


class DepartmentAdminTests(BaseTestCase):
    def test_admin_creates_department_and_it_is_audited(self):
        self.as_user(self.admin)
        response = self.client.post("/api/departments/", {"name": "Physics", "code": "phy"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["code"], "PHY")
        self.assertTrue(AuditLog.objects.filter(action="DEPARTMENT_CREATED").exists())

    def test_duplicate_department_names_and_codes_are_blocked_case_insensitively(self):
        self.as_user(self.admin)
        dup_name = self.client.post("/api/departments/", {"name": "  computer   science ", "code": "X1"}, format="json")
        self.assertEqual(dup_name.status_code, 400)
        self.assertIn("name", dup_name.data)
        dup_code = self.client.post("/api/departments/", {"name": "Another", "code": "cs"}, format="json")
        self.assertEqual(dup_code.status_code, 400)
        self.assertIn("code", dup_code.data)
        self.assertEqual(Department.objects.count(), 2)

    def test_rename_to_existing_name_is_blocked_but_own_name_is_fine(self):
        self.as_user(self.admin)
        self.assertEqual(self.client.patch(f"/api/departments/{self.cs.id}/", {"name": "Mathematics"}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(f"/api/departments/{self.cs.id}/", {"name": "Computer Science"}, format="json").status_code, 200)

    def test_list_shows_hod_and_live_counts(self):
        self.as_user(self.admin)
        response = self.client.get("/api/departments/")
        by_code = {d["code"]: d for d in response.data["results"]}
        self.assertEqual(by_code["CS"]["staff_count"], 2)
        self.assertEqual(by_code["CS"]["student_count"], 5)
        self.assertEqual(by_code["CS"]["hod"]["username"], "hod_cs")
        self.assertEqual(by_code["MATH"]["staff_count"], 1)
        self.assertEqual(by_code["MATH"]["student_count"], 3)

    def test_search_and_filter(self):
        self.as_user(self.admin)
        self.assertEqual(self.client.get("/api/departments/?search=math").data["count"], 1)
        self.client.post(f"/api/departments/{self.math.id}/deactivate/")
        self.assertEqual(self.client.get("/api/departments/?is_active=false").data["count"], 1)
        self.assertEqual(self.client.get("/api/departments/?is_active=true").data["count"], 1)
        self.assertTrue(AuditLog.objects.filter(action="DEPARTMENT_DEACTIVATED").exists())

    def test_departments_cannot_be_deleted(self):
        self.as_user(self.admin)
        self.assertEqual(self.client.delete(f"/api/departments/{self.cs.id}/").status_code, 405)


class DepartmentAccessTests(BaseTestCase):
    def test_hod_and_staff_only_see_their_own_department(self):
        for user in (self.hod_cs, self.t1):
            self.as_user(user)
            response = self.client.get("/api/departments/")
            self.assertEqual([d["id"] for d in response.data["results"]], [self.cs.id])
            self.assertEqual(self.client.get(f"/api/departments/{self.math.id}/").status_code, 404)

    def test_hod_and_staff_cannot_modify_departments(self):
        for user in (self.hod_cs, self.t1):
            self.as_user(user)
            self.assertEqual(self.client.post("/api/departments/", {"name": "Hack", "code": "H"}, format="json").status_code, 403)
            self.assertEqual(self.client.patch(f"/api/departments/{self.cs.id}/", {"name": "Hack"}, format="json").status_code, 403)
            self.assertEqual(self.client.post(f"/api/departments/{self.cs.id}/deactivate/").status_code, 403)
