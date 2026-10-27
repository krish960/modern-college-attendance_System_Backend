"""Shared fixtures for the test-suite (not imported by production code)."""
import datetime

from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APITestCase

from academics.models import ClassRoom, Subject, TeacherSubjectAssignment
from accounts.models import StaffProfile, User
from attendance.models import Lecture
from departments.models import Department
from students.models import Student

PASSWORD = "Str0ng!Pass#123"


def make_user(username, role, department=None, **extra):
    user = User(username=username, role=role, department=department,
                first_name=username.replace("_", " ").title(), last_name="Test", **extra)
    user.set_password(PASSWORD)
    user.save()
    StaffProfile.objects.create(user=user)
    return user


class BaseTestCase(APITestCase):
    """
    Two departments (Computer Science, Mathematics), an admin, one HOD each, three teachers,
    one class per department and five CS students.
    """

    def setUp(self):
        cache.clear()  # reset the login throttle between tests
        self.today = timezone.localdate()
        self.cs = Department.objects.create(name="Computer Science", code="CS")
        self.math = Department.objects.create(name="Mathematics", code="MATH")

        self.admin = make_user("admin", User.Role.ADMIN, is_staff=True, is_superuser=True)
        self.hod_cs = make_user("hod_cs", User.Role.HOD, self.cs)
        self.hod_math = make_user("hod_math", User.Role.HOD, self.math)
        self.t1 = make_user("prof_a", User.Role.STAFF, self.cs)
        self.t2 = make_user("prof_b", User.Role.STAFF, self.cs)
        self.t_math = make_user("prof_m", User.Role.STAFF, self.math)

        self.class_cs = ClassRoom.objects.create(
            department=self.cs, name="MCS", semester=1, division="A", academic_year="2026-27")
        self.class_math = ClassRoom.objects.create(
            department=self.math, name="MSc Maths", semester=1, division="A", academic_year="2026-27")

        self.python = Subject.objects.create(department=self.cs, name="Python", code="PY101")
        self.dbms = Subject.objects.create(department=self.cs, name="DBMS", code="DB101")
        self.algebra = Subject.objects.create(department=self.math, name="Algebra", code="AL101")

        self.assign_py = TeacherSubjectAssignment.objects.create(
            teacher=self.t1, subject=self.python, classroom=self.class_cs, academic_year="2026-27")
        self.assign_db = TeacherSubjectAssignment.objects.create(
            teacher=self.t2, subject=self.dbms, classroom=self.class_cs, academic_year="2026-27")
        self.assign_math = TeacherSubjectAssignment.objects.create(
            teacher=self.t_math, subject=self.algebra, classroom=self.class_math, academic_year="2026-27")

        self.students = [
            Student.objects.create(classroom=self.class_cs, roll_number=str(i), name=f"Student {i}", created_by=self.t1)
            for i in range(1, 6)
        ]
        self.math_students = [
            Student.objects.create(classroom=self.class_math, roll_number=str(i), name=f"Math Student {i}",
                                   created_by=self.t_math)
            for i in range(1, 4)
        ]

    # --- helpers -------------------------------------------------------------
    def as_user(self, user):
        self.client.force_authenticate(user)
        return user

    def make_lecture(self, assignment=None, date=None, start="09:00", end="10:00", status=Lecture.Status.DRAFT):
        return Lecture.objects.create(
            teacher_subject_assignment=assignment or self.assign_py,
            lecture_date=date or self.today,
            start_time=datetime.time.fromisoformat(start), end_time=datetime.time.fromisoformat(end),
            status=status,
        )

    def marks(self, students, present_ids=()):
        return {"records": [
            {"student": s.id, "status": "PRESENT" if s.id in present_ids else "ABSENT"} for s in students
        ]}

    def submit(self, lecture, teacher, present_count=3, students=None):
        """Submit attendance as `teacher`: first `present_count` students present, rest absent."""
        students = students or self.students
        present = {s.id for s in students[:present_count]}
        self.as_user(teacher)
        response = self.client.post(f"/api/lectures/{lecture.id}/submit/", self.marks(students, present), format="json")
        assert response.status_code == 200, response.data
        lecture.refresh_from_db()
        return lecture
