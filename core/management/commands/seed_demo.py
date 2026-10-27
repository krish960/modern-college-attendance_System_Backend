"""
Optional development data: two departments with an HOD, teachers, classes, students,
a weekly timetable and a few days of submitted attendance.

    python manage.py seed_demo            # asks for nothing; prints the demo logins
    python manage.py seed_demo --password "Another#Pass1"

Never run this against a real college database. It refuses to run when DEBUG is off.
"""
import datetime
import random

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from academics.models import ClassRoom, Subject, TeacherSubjectAssignment
from accounts.models import StaffProfile, User
from accounts.serializers import AccountWriteSerializer
from attendance import services
from attendance.models import Lecture, TimetableSlot
from departments.models import Department
from students.models import Student

# academic year given to every demo class and assignment
YEAR = "2026-27"
# name pieces; a demo student name is a random first name plus a random last name
FIRST = ["Aarav", "Vihaan", "Ananya", "Diya", "Rohan", "Sneha", "Kabir", "Isha", "Aditya", "Priya", "Rahul", "Meera",
         "Siddharth", "Neha", "Omkar", "Pooja", "Tejas", "Shruti", "Yash", "Kavya", "Atharv", "Riya", "Nikhil", "Tanvi"]
LAST = ["Patil", "Deshmukh", "Joshi", "Kulkarni", "Shinde", "More", "Pawar", "Jadhav", "Gaikwad", "Bhosale"]

# what to create: for each department its HOD, teachers, one class, subjects and how many students
DEPARTMENTS = [
    {
        "name": "Computer Science", "code": "CS", "hod": ("hod_cs", "Anil", "Kulkarni"),
        "class": ("MCS Part I", 1, "A"),
        "staff": [("prof_rane", "Sunita", "Rane"), ("prof_gokhale", "Mahesh", "Gokhale"), ("prof_iyer", "Lakshmi", "Iyer")],
        "subjects": [("Python Programming", "CS101"), ("Database Management Systems", "CS102"), ("Computer Networks", "CS103")],
        "students": 24,
    },
    {
        "name": "Mathematics", "code": "MATH", "hod": ("hod_math", "Vandana", "Phadke"),
        "class": ("MSc Mathematics I", 1, "A"),
        "staff": [("prof_sathe", "Rajesh", "Sathe"), ("prof_bapat", "Anjali", "Bapat")],
        "subjects": [("Linear Algebra", "MA101"), ("Real Analysis", "MA102")],
        "students": 16,
    },
]
# the three daily lecture times used for the timetable
SLOTS = [(datetime.time(9, 0), datetime.time(10, 0)), (datetime.time(10, 15), datetime.time(11, 15)), (datetime.time(11, 30), datetime.time(12, 30))]


class Command(BaseCommand):
    """`python manage.py seed_demo`: fills an empty development database with demo people and attendance."""

    help = "Create realistic demo data for local development (refuses to run when DEBUG is off)."

    def add_arguments(self, parser):
        """The only option is --password, the password that every demo account gets."""
        parser.add_argument("--password", default="Demo#Pass2026", help="Password given to every demo account.")

    @transaction.atomic
    def handle(self, *args, **opts):
        """Create everything inside one transaction, so a failure leaves no half-made demo data."""
        if not settings.DEBUG:
            raise CommandError("seed_demo only runs with DJANGO_DEBUG=true.")
        if Department.objects.filter(code__in=[d["code"] for d in DEPARTMENTS]).exists():
            raise CommandError("Demo departments already exist. Delete db.sqlite3 and migrate again for a fresh seed.")

        rng = random.Random(7)  # fixed seed: the same names and attendance every time
        password = opts["password"]
        today = timezone.localdate()
        logins = []

        def make_user(username, first, last, role, dept):
            """Create one HOD or teacher with the demo password and a staff profile, and remember the login for printing."""
            user = User(username=username, first_name=first, last_name=last, role=role, department=dept,
                        email=f"{username}@college.example")
            user.set_password(password)
            user.save()
            StaffProfile.objects.create(
                user=user, employee_id=AccountWriteSerializer.next_employee_id(dept),
                designation="Head of Department" if role == User.Role.HOD else "Assistant Professor")
            logins.append((role, username))
            return user

        for spec in DEPARTMENTS:
            dept = Department.objects.create(name=spec["name"], code=spec["code"], description=f"Department of {spec['name']}")
            hod = make_user(*spec["hod"], User.Role.HOD, dept)
            teachers = [make_user(u, f, l, User.Role.STAFF, dept) for u, f, l in spec["staff"]]
            name, semester, division = spec["class"]
            classroom = ClassRoom.objects.create(department=dept, name=name, semester=semester, division=division, academic_year=YEAR)
            subjects = [Subject.objects.create(department=dept, name=n, code=c, semester=semester) for n, c in spec["subjects"]]

            assignments = []
            for i, subject in enumerate(subjects):
                assignments.append(TeacherSubjectAssignment.objects.create(
                    teacher=teachers[i % len(teachers)], subject=subject, classroom=classroom, academic_year=YEAR))

            # Monday-Saturday: each subject gets one slot a day, rotated so slots never overlap.
            for weekday in range(6):
                for i, assignment in enumerate(assignments):
                    start, end = SLOTS[(i + weekday) % len(SLOTS)]
                    TimetableSlot.objects.create(assignment=assignment, weekday=weekday, start_time=start, end_time=end)

            names = set()  # a set, so two students never get the same full name
            while len(names) < spec["students"]:
                names.add(f"{rng.choice(FIRST)} {rng.choice(LAST)}")
            students = [
                Student.objects.create(classroom=classroom, roll_number=str(n), name=student_name,
                                       created_by=teachers[n % len(teachers)])
                for n, student_name in enumerate(sorted(names), start=1)
            ]

            # Lectures for the last 5 days up to today.
            services.generate_lectures(hod, today - datetime.timedelta(days=5), today)
            lectures = Lecture.objects.filter(teacher_subject_assignment__classroom=classroom).order_by("lecture_date", "start_time")
            for lecture in lectures:
                # (the HOD generated the lectures above; each teacher now "submits" their own)
                # Past days: everything submitted except one forgotten lecture; today: first lecture only.
                is_today = lecture.lecture_date == today
                skip = (is_today and lecture.start_time != SLOTS[0][0]) or (not is_today and rng.random() < 0.06)
                if skip:
                    continue
                teacher = lecture.teacher_subject_assignment.teacher
                # about 85 percent of students are marked present
                items = [{"student": s.id, "status": "PRESENT" if rng.random() < 0.85 else "ABSENT"} for s in students]
                services.save_attendance(lecture.id, teacher, items, submit=True)

        self.stdout.write(self.style.SUCCESS("Demo data created."))
        self.stdout.write(f"All demo accounts use the password: {password}")
        for role, username in logins:
            self.stdout.write(f"  {role:<6} {username}")
        self.stdout.write("Create the College Admin yourself:  python manage.py create_college_admin --username admin")
