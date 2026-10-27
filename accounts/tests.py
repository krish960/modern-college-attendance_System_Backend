import os
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError

from core.testing import PASSWORD, BaseTestCase, make_user

from core.models import AuditLog

from .models import User

LOGIN = "/api/auth/login/"


class LoginTests(BaseTestCase):
    def login(self, username, password=PASSWORD):
        return self.client.post(LOGIN, {"username": username, "password": password}, format="json")

    def test_login_returns_tokens_and_role_for_each_role(self):
        for username, role in (("admin", "ADMIN"), ("hod_cs", "HOD"), ("prof_a", "STAFF")):
            response = self.login(username)
            self.assertEqual(response.status_code, 200, response.data)
            self.assertIn("access", response.data)
            self.assertIn("refresh", response.data)
            self.assertEqual(response.data["user"]["role"], role)
            self.assertNotIn("password", response.data["user"])

    def test_wrong_password_is_rejected(self):
        self.assertEqual(self.login("prof_a", "wrong-password").status_code, 401)

    def test_inactive_user_cannot_log_in(self):
        self.t1.is_active = False
        self.t1.save()
        self.assertEqual(self.login("prof_a").status_code, 401)

    def test_deactivated_department_blocks_login_and_existing_tokens(self):
        token = self.login("prof_a").data["access"]
        self.cs.is_active = False
        self.cs.save()
        self.assertEqual(self.login("prof_a").status_code, 401)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 401)

    def test_logout_blacklists_refresh_token(self):
        data = self.login("prof_a").data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {data['access']}")
        self.assertEqual(self.client.post("/api/auth/logout/", {"refresh": data["refresh"]}, format="json").status_code, 205)
        self.client.credentials()
        refreshed = self.client.post("/api/auth/refresh/", {"refresh": data["refresh"]}, format="json")
        self.assertEqual(refreshed.status_code, 401)

    def test_refresh_token_works_before_logout(self):
        data = self.login("prof_a").data
        refreshed = self.client.post("/api/auth/refresh/", {"refresh": data["refresh"]}, format="json")
        self.assertEqual(refreshed.status_code, 200)
        self.assertIn("access", refreshed.data)

    def test_cannot_logout_with_someone_elses_token(self):
        other = self.login("prof_b").data["refresh"]
        mine = self.login("prof_a").data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {mine}")
        self.assertEqual(self.client.post("/api/auth/logout/", {"refresh": other}, format="json").status_code, 403)

    def test_login_is_rate_limited(self):
        for _ in range(10):
            self.assertEqual(self.login("prof_a", "bad").status_code, 401)
        self.assertEqual(self.login("prof_a", "bad").status_code, 429)

    def test_protected_endpoints_require_authentication(self):
        for url in ("/api/departments/", "/api/students/", "/api/lectures/", "/api/dashboard/", "/api/reports/daily/"):
            self.assertEqual(self.client.get(url).status_code, 401, url)

    def test_passwords_are_stored_hashed(self):
        self.assertNotEqual(self.t1.password, PASSWORD)
        self.assertNotIn(PASSWORD, self.t1.password)
        self.assertIn("$", self.t1.password)
        self.assertTrue(self.t1.check_password(PASSWORD))


class AccountPermissionTests(BaseTestCase):
    def staff_payload(self, **extra):
        return {"username": "new_teacher", "password": "Anoth3r!Strong#pw", "first_name": "New",
                "last_name": "Teacher", "email": "new@example.com", **extra}

    def test_hod_registers_staff_in_own_department_and_role_cannot_be_forced(self):
        self.as_user(self.hod_cs)
        response = self.client.post(
            "/api/staff/", self.staff_payload(role="ADMIN", department=self.math.id, is_superuser=True), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        user = User.objects.get(username="new_teacher")
        self.assertEqual(user.role, User.Role.STAFF)
        self.assertEqual(user.department, self.cs)
        self.assertFalse(user.is_superuser)
        self.assertNotEqual(user.password, "Anoth3r!Strong#pw")

    def test_weak_password_is_rejected(self):
        self.as_user(self.hod_cs)
        response = self.client.post("/api/staff/", self.staff_payload(password="12345678"), format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.data)

    def test_password_is_required_on_create(self):
        self.as_user(self.hod_cs)
        payload = self.staff_payload()
        del payload["password"]
        self.assertEqual(self.client.post("/api/staff/", payload, format="json").status_code, 400)

    def test_admin_edits_staff_of_any_department_but_does_not_register_them(self):
        self.as_user(self.admin)
        self.assertEqual(self.client.post("/api/staff/", self.staff_payload(), format="json").status_code, 403)
        t = self.t1
        r = self.client.patch(f"/api/staff/{t.id}/", {"first_name": "Fixed", "designation": "Professor", "employee_id": "EMP-099"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        t.refresh_from_db()
        self.assertEqual((t.first_name, t.department_id), ("Fixed", self.cs.id))
        self.assertEqual(self.client.post(f"/api/staff/{t.id}/deactivate/").status_code, 200)
        self.assertEqual(self.client.post(f"/api/staff/{t.id}/activate/").status_code, 200)
        r = self.client.post(f"/api/staff/{t.id}/reset-password/", {"password": "Another#Pass2026"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def test_hod_chooses_permanent_or_provisional(self):
        self.as_user(self.hod_cs)
        r = self.client.post("/api/staff/", self.staff_payload(username="prov1", email="prov1@example.com", employment="PROVISIONAL"), format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["employment"], "PROVISIONAL")
        r2 = self.client.post("/api/staff/", self.staff_payload(username="perm1", email="perm1@example.com"), format="json")
        self.assertEqual(r2.data["employment"], "PERMANENT")  # permanent unless the HOD says otherwise
        r3 = self.client.patch(f"/api/staff/{r.data['id']}/", {"employment": "PERMANENT"}, format="json")
        self.assertEqual(r3.data["employment"], "PERMANENT")
        self.assertEqual(self.client.post("/api/staff/", self.staff_payload(username="bad1", employment="X"), format="json").status_code, 400)

    def test_staff_cannot_register_staff_or_hods(self):
        self.as_user(self.t1)
        self.assertEqual(self.client.post("/api/staff/", self.staff_payload(), format="json").status_code, 403)
        self.assertEqual(self.client.post("/api/hods/", self.staff_payload(department=self.cs.id), format="json").status_code, 403)

    def test_hod_cannot_register_hods(self):
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.post("/api/hods/", self.staff_payload(department=self.cs.id), format="json").status_code, 403)

    def test_admin_registers_hod_and_second_active_hod_is_refused(self):
        dept = self.cs.__class__.objects.create(name="Physics", code="PHY")
        self.as_user(self.admin)
        payload = self.staff_payload(username="hod_phy", department=dept.id)
        response = self.client.post("/api/hods/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(User.objects.get(username="hod_phy").role, User.Role.HOD)
        again = self.client.post("/api/hods/", self.staff_payload(username="hod_phy2", department=dept.id,
                                                                  email="x@example.com"), format="json")
        self.assertEqual(again.status_code, 400)
        self.assertIn("department", again.data)

    def test_admin_cannot_register_hod_for_inactive_department(self):
        self.math.is_active = False
        self.math.save()
        self.hod_math.is_active = False
        self.hod_math.save()
        self.as_user(self.admin)
        response = self.client.post("/api/hods/", self.staff_payload(department=self.math.id), format="json")
        self.assertEqual(response.status_code, 400)

    def test_admin_can_deactivate_and_reactivate_hod(self):
        self.as_user(self.admin)
        self.assertEqual(self.client.post(f"/api/hods/{self.hod_cs.id}/deactivate/").status_code, 200)
        self.hod_cs.refresh_from_db()
        self.assertFalse(self.hod_cs.is_active)
        self.assertEqual(self.client.post(LOGIN, {"username": "hod_cs", "password": PASSWORD}, format="json").status_code, 401)
        self.assertEqual(self.client.post(f"/api/hods/{self.hod_cs.id}/activate/").status_code, 200)

    def test_cannot_reactivate_hod_when_department_has_another_active_hod(self):
        self.as_user(self.admin)
        self.client.post(f"/api/hods/{self.hod_cs.id}/deactivate/")
        replacement = make_user("hod_cs_new", User.Role.HOD, self.cs)
        self.assertTrue(replacement.is_active)
        self.assertEqual(self.client.post(f"/api/hods/{self.hod_cs.id}/activate/").status_code, 400)

    def test_hod_can_edit_deactivate_and_reset_staff(self):
        self.as_user(self.hod_cs)
        response = self.client.patch(f"/api/staff/{self.t1.id}/", {"first_name": "Renamed", "role": "ADMIN",
                                                                    "employee_id": "E-1"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.t1.refresh_from_db()
        self.assertEqual(self.t1.first_name, "Renamed")
        self.assertEqual(self.t1.role, User.Role.STAFF)
        self.assertEqual(self.t1.profile.employee_id, "E-1")
        self.assertEqual(self.client.post(f"/api/staff/{self.t1.id}/deactivate/").status_code, 200)
        self.assertEqual(self.client.post(f"/api/staff/{self.t1.id}/activate/").status_code, 200)
        reset = self.client.post(f"/api/staff/{self.t1.id}/reset-password/", {"password": "Br4nd!New#Pass99"}, format="json")
        self.assertEqual(reset.status_code, 200)
        self.t1.refresh_from_db()
        self.assertTrue(self.t1.check_password("Br4nd!New#Pass99"))

    def test_employee_id_must_be_unique_within_department(self):
        self.as_user(self.hod_cs)
        self.client.patch(f"/api/staff/{self.t1.id}/", {"employee_id": "E-7"}, format="json")
        clash = self.client.patch(f"/api/staff/{self.t2.id}/", {"employee_id": "e-7"}, format="json")
        self.assertEqual(clash.status_code, 400)

    def test_teacher_cannot_change_own_role_or_department(self):
        self.as_user(self.t1)
        response = self.client.patch("/api/auth/me/", {"role": "ADMIN", "department": self.math.id,
                                                       "is_superuser": True, "first_name": "Me"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.t1.refresh_from_db()
        self.assertEqual(self.t1.role, User.Role.STAFF)
        self.assertEqual(self.t1.department, self.cs)
        self.assertFalse(self.t1.is_superuser)
        self.assertEqual(self.t1.first_name, "Me")
        self.assertEqual(self.client.patch(f"/api/staff/{self.t1.id}/", {"role": "ADMIN"}, format="json").status_code, 403)

    def test_change_password_requires_current_password(self):
        self.as_user(self.t1)
        bad = self.client.post("/api/auth/change-password/", {"old_password": "nope", "new_password": "Br4nd!New#Pass99"}, format="json")
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post("/api/auth/change-password/", {"old_password": PASSWORD, "new_password": "Br4nd!New#Pass99"}, format="json")
        self.assertEqual(ok.status_code, 200)
        self.t1.refresh_from_db()
        self.assertTrue(self.t1.check_password("Br4nd!New#Pass99"))

    def test_new_password_must_be_strong_different_and_confirmed(self):
        from django.core.cache import cache
        cache.clear()
        self.as_user(self.t1)
        url = "/api/auth/change-password/"
        for weak in ("short1!A", "alllowercase1!", "ALLUPPERCASE1!", "NoDigitsHere!!", "NoSymbol12345"):
            r = self.client.post(url, {"old_password": PASSWORD, "new_password": weak}, format="json")
            self.assertEqual(r.status_code, 400, weak)
            self.assertIn("new_password", r.data)
        same = self.client.post(url, {"old_password": PASSWORD, "new_password": PASSWORD}, format="json")
        self.assertEqual(same.status_code, 400)
        mismatch = self.client.post(url, {"old_password": PASSWORD, "new_password": "Br4nd!New#Pass99", "confirm_password": "other"}, format="json")
        self.assertIn("confirm_password", mismatch.data)
        ok = self.client.post(url, {"old_password": PASSWORD, "new_password": "Br4nd!New#Pass99", "confirm_password": "Br4nd!New#Pass99"}, format="json")
        self.assertEqual(ok.status_code, 200)

    def test_wrong_current_password_is_limited(self):
        from django.core.cache import cache
        cache.clear()
        self.as_user(self.t1)
        url = "/api/auth/change-password/"
        for _ in range(5):
            self.assertEqual(self.client.post(url, {"old_password": "nope", "new_password": "Br4nd!New#Pass99"}, format="json").status_code, 400)
        # now even the right password is refused until the wait is over
        blocked = self.client.post(url, {"old_password": PASSWORD, "new_password": "Br4nd!New#Pass99"}, format="json")
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("Too many", str(blocked.data))
        cache.clear()

    def test_staff_cannot_list_hods_or_staff_endpoints_write(self):
        self.as_user(self.t1)
        self.assertEqual(self.client.get("/api/hods/").status_code, 403)
        self.assertEqual(self.client.get("/api/staff/").status_code, 403)


class CreateAdminCommandTests(BaseTestCase):
    def test_creates_admin_with_hashed_password_from_environment(self):
        with mock.patch.dict(os.environ, {"ADMIN_PASSWORD": "Adm1n!Secure#Pass"}):
            call_command("create_college_admin", username="principal", email="p@example.com")
        user = User.objects.get(username="principal")
        self.assertEqual(user.role, User.Role.ADMIN)
        self.assertTrue(user.is_superuser)
        self.assertIsNone(user.department)
        self.assertNotEqual(user.password, "Adm1n!Secure#Pass")
        self.assertTrue(user.check_password("Adm1n!Secure#Pass"))

    def test_rejects_weak_password_and_duplicate_username(self):
        with mock.patch.dict(os.environ, {"ADMIN_PASSWORD": "123"}):
            with self.assertRaises(CommandError):
                call_command("create_college_admin", username="principal2")
        with mock.patch.dict(os.environ, {"ADMIN_PASSWORD": "Adm1n!Secure#Pass"}):
            with self.assertRaises(CommandError):
                call_command("create_college_admin", username="admin")

    def test_createsuperuser_manager_assigns_admin_role(self):
        user = User.objects.create_superuser("root", "r@example.com", "Adm1n!Secure#Pass")
        self.assertEqual(user.role, User.Role.ADMIN)


class AutoEmployeeIdTests(BaseTestCase):
    def new_teacher(self, username, **extra):
        self.as_user(self.hod_cs)
        return self.client.post("/api/staff/", {"username": username, "first_name": "A", "last_name": "B", "password": "Str0ng#Pass2026", **extra}, format="json")

    def test_employee_id_is_assigned_in_sequence_when_left_empty(self):
        ids = [self.new_teacher(f"auto{i}").data["employee_id"] for i in range(3)]
        self.assertEqual(ids, ["EMP-001", "EMP-002", "EMP-003"])

    def test_sequence_continues_after_the_highest_and_skips_hand_typed_styles(self):
        self.new_teacher("typed", employee_id="E-77")
        self.new_teacher("big", employee_id="EMP-010")
        self.assertEqual(self.new_teacher("next").data["employee_id"], "EMP-011")

    def test_numbers_are_per_department(self):
        self.new_teacher("cs1")
        self.as_user(self.hod_math)
        r = self.client.post("/api/staff/", {"username": "m1", "first_name": "A", "last_name": "B", "password": "Str0ng#Pass2026"}, format="json")
        self.assertEqual(r.data["employee_id"], "EMP-001")

    def test_hand_typed_id_is_still_respected(self):
        self.assertEqual(self.new_teacher("own", employee_id="STAFF-9").data["employee_id"], "STAFF-9")


class RegistrationChecksTests(BaseTestCase):
    def test_phone_numbers_must_look_like_phone_numbers(self):
        self.as_user(self.hod_cs)
        base = {"username": "t_ph", "password": "Pass#Word2026x", "first_name": "A", "last_name": "B", "email": "ph@q.com"}
        for bad in ("abc", "call me", "12", "1" * 20, "98765+4321"):
            r = self.client.post("/api/staff/", {**base, "phone": bad}, format="json")
            self.assertEqual(r.status_code, 400, bad)
            self.assertIn("phone", r.data)
        for good in ("", "98765 43210", "+91 98765-43210", "(020) 2565 1234"):
            n = abs(hash(good)) % 10000
            r = self.client.post("/api/staff/", {**base, "username": f"p{n}", "email": f"p{n}@q.com", "phone": good}, format="json")
            self.assertEqual(r.status_code, 201, (good, r.data))

    def test_a_username_cannot_be_changed(self):
        self.as_user(self.hod_cs)
        r = self.client.patch(f"/api/staff/{self.t1.id}/", {"username": "new_name"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.patch(f"/api/staff/{self.t1.id}/", {"username": self.t1.username, "first_name": "Z"}, format="json").status_code, 200)

    def test_roll_numbers_one_zero_one_and_001_are_the_same_student(self):
        self.as_user(self.t1)
        first = self.client.post("/api/students/", {"classroom": self.class_cs.id, "roll_number": "0901", "name": "Zero Nine"}, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(first.data["roll_number"], "901")
        again = self.client.post("/api/students/", {"classroom": self.class_cs.id, "roll_number": " 901 ", "name": "Other"}, format="json")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.client.post("/api/students/", {"classroom": self.class_cs.id, "roll_number": "00901", "name": "Other"}, format="json").status_code, 409)
        self.assertEqual(self.client.post("/api/students/", {"classroom": self.class_cs.id, "roll_number": "A07", "name": "Letters"}, format="json").data["roll_number"], "A07")


class LoginLockoutTests(BaseTestCase):
    """Five wrong passwords in a row lock the username for a while, even for the right password."""

    def attempt(self, password):
        return self.client.post("/api/auth/login/", {"username": "prof_a", "password": password}, format="json")

    def test_locked_after_five_wrong_passwords(self):
        from django.core.cache import cache
        cache.clear()
        for _ in range(5):
            self.assertEqual(self.attempt("wrong-password").status_code, 401)
        self.assertEqual(self.attempt(PASSWORD).status_code, 401)  # right password, still refused
        self.assertTrue(AuditLog.objects.filter(action="LOGIN_LOCKED").exists())
        cache.clear()

    def test_good_login_forgets_earlier_mistakes(self):
        from django.core.cache import cache
        cache.clear()
        for _ in range(4):
            self.attempt("wrong-password")
        self.assertEqual(self.attempt(PASSWORD).status_code, 200)
        for _ in range(4):
            self.attempt("wrong-password")
        self.assertEqual(self.attempt(PASSWORD).status_code, 200)  # counter had been reset
        cache.clear()
