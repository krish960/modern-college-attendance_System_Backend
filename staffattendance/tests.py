import datetime
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from core.models import AuditLog
from core.testing import BaseTestCase

from .models import StaffAttendance


class StaffAttendanceTests(BaseTestCase):
    def mark(self, user, status="PRESENT", **extra):
        return self.client.post("/api/staff-attendance/mark/", {"user": user.id, "status": status, **extra}, format="json")

    def test_teacher_checks_in_once_for_today(self):
        self.as_user(self.t1)
        r = self.client.post("/api/staff-attendance/check-in/")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["source"], "SELF")
        self.assertIn(r.data["status"], ("PRESENT", "LATE"))
        again = self.client.post("/api/staff-attendance/check-in/")
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.data["already"])
        self.assertEqual(StaffAttendance.objects.filter(user=self.t1).count(), 1)

    @override_settings(STAFF_LATE_AFTER="10:00")
    def test_check_in_after_the_cutoff_is_late(self):
        from . import services
        at = timezone.make_aware(datetime.datetime.combine(timezone.localdate(), datetime.time(10, 30)))
        self.assertEqual(services.present_or_late(at), "LATE")
        at = timezone.make_aware(datetime.datetime.combine(timezone.localdate(), datetime.time(9, 45)))
        self.assertEqual(services.present_or_late(at), "PRESENT")

    def test_only_teachers_check_in_and_only_hod_marks(self):
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.post("/api/staff-attendance/check-in/").status_code, 403)
        self.as_user(self.t1)
        self.assertEqual(self.mark(self.t2).status_code, 403)
        self.as_user(self.admin)
        self.assertEqual(self.mark(self.t2).status_code, 403)

    def test_hod_marks_corrects_and_it_is_audited(self):
        self.as_user(self.hod_cs)
        self.assertEqual(self.mark(self.t1, "ABSENT").status_code, 200)
        r = self.mark(self.t1, "LEAVE", note="Medical")
        self.assertEqual(r.data["status"], "LEAVE")
        self.assertEqual(r.data["note"], "Medical")
        self.assertEqual(StaffAttendance.objects.filter(user=self.t1).count(), 1)
        self.assertEqual(AuditLog.objects.filter(action="STAFF_ATTENDANCE_MARKED").count(), 2)

    def test_hod_cannot_mark_other_department_or_the_future(self):
        self.as_user(self.hod_cs)
        self.assertEqual(self.mark(self.t_math).status_code, 404)
        tomorrow = (timezone.localdate() + datetime.timedelta(days=1)).isoformat()
        self.assertEqual(self.mark(self.t1, date=tomorrow).status_code, 400)
        self.assertEqual(self.mark(self.hod_cs).status_code, 404)  # only teachers
        self.assertEqual(self.mark(self.t1, "NOPE").status_code, 400)

    def test_scan_marks_present_but_never_overwrites_a_checkin(self):
        self.as_user(self.hod_cs)
        r = self.mark(self.t1, "AUTO", scan=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["source"], "SCAN")
        self.assertFalse(r.data["unchanged"])
        first_at = StaffAttendance.objects.get(user=self.t1).check_in_at
        again = self.mark(self.t1, "AUTO", scan=True)
        self.assertTrue(again.data["unchanged"])
        self.assertEqual(StaffAttendance.objects.get(user=self.t1).check_in_at, first_at)

    def test_day_sheet_is_scoped_and_teachers_cannot_read_it(self):
        self.as_user(self.hod_cs)
        self.mark(self.t1)
        data = self.client.get("/api/staff-attendance/day/").data
        names = {r["user"] for r in data["results"]}
        self.assertIn(self.t1.id, names)
        self.assertNotIn(self.t_math.id, names)
        self.assertEqual(data["summary"]["present"], 1)
        self.as_user(self.t1)
        self.assertEqual(self.client.get("/api/staff-attendance/day/").status_code, 403)
        self.as_user(self.admin)
        self.assertEqual(self.client.get("/api/staff-attendance/day/").status_code, 400)  # must choose a department
        ok = self.client.get("/api/staff-attendance/day/", {"department": self.hod_cs.department_id})
        self.assertEqual(ok.status_code, 200)

    def test_my_month_lists_only_my_days(self):
        self.as_user(self.t1)
        self.client.post("/api/staff-attendance/check-in/")
        self.as_user(self.hod_cs)
        self.mark(self.t2)
        self.as_user(self.t1)
        data = self.client.get("/api/staff-attendance/me/").data
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(data["today"]["status"] is not None, True)

    def test_monthly_sheet_and_exports(self):
        self.as_user(self.hod_cs)
        self.mark(self.t1, "PRESENT")
        self.mark(self.t2, "ABSENT")
        sheet = self.client.get("/api/staff-attendance/monthly/").data
        rows = {r["user"]: r for r in sheet["results"]}
        today = str(timezone.localdate().day)
        self.assertEqual(rows[self.t1.id]["marks"][today], "P")
        self.assertEqual(rows[self.t2.id]["marks"][today], "A")
        self.assertEqual(rows[self.t1.id]["present"], 1)
        self.assertNotIn(self.t_math.id, rows)
        csv = self.client.get("/api/staff-attendance/monthly/", {"export": "csv"})
        self.assertTrue(csv.content.decode().startswith("\ufeffStaff ID,Name,"))
        pdf = self.client.get("/api/staff-attendance/monthly/", {"export": "pdf"})
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        self.assertEqual(self.client.get("/api/staff-attendance/monthly/", {"month": "2026-13"}).status_code, 400)

    def test_a_submitted_lecture_marks_the_teacher_present_automatically(self):
        lecture = self.make_lecture(self.assign_py)
        self.assertFalse(StaffAttendance.objects.filter(user=self.t1).exists())
        self.submit(lecture, self.t1, 2, self.students)
        record = StaffAttendance.objects.get(user=self.t1, date=lecture.lecture_date)
        self.assertEqual((record.status, record.source), ("PRESENT", "LECTURE"))
        self.assertTrue(AuditLog.objects.filter(action="STAFF_ATTENDANCE_AUTO").exists())

    def test_saving_a_draft_or_cancelling_does_not_count(self):
        lecture = self.make_lecture(self.assign_py)
        self.as_user(self.t1)
        self.client.post(f"/api/lectures/{lecture.id}/mark/", self.marks(self.students, []), format="json")
        self.assertFalse(StaffAttendance.objects.filter(user=self.t1).exists())

    def test_lecture_overrides_absent_and_leave_but_keeps_late_and_half_day(self):
        day = timezone.localdate()
        for old, expect in (("ABSENT", "PRESENT"), ("LEAVE", "PRESENT"), ("LATE", "LATE"), ("HALF_DAY", "HALF_DAY")):
            StaffAttendance.objects.filter(user=self.t1).delete()
            StaffAttendance.objects.create(user=self.t1, date=day, status=old, source="HOD", marked_by=self.hod_cs)
            self.submit(self.make_lecture(self.assign_py, start="1%d:00" % (1 + ["ABSENT", "LEAVE", "LATE", "HALF_DAY"].index(old)), end="1%d:50" % (1 + ["ABSENT", "LEAVE", "LATE", "HALF_DAY"].index(old))), self.t1, 2, self.students)
            self.assertEqual(StaffAttendance.objects.get(user=self.t1, date=day).status, expect, old)

    def test_staff_card_code_is_unique_and_normalised(self):
        self.as_user(self.hod_cs)
        r = self.client.patch(f"/api/staff/{self.t1.id}/", {"card_code": " st 001 "}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["card_code"], "ST001")
        clash = self.client.patch(f"/api/staff/{self.t2.id}/", {"card_code": "st001"}, format="json")
        self.assertEqual(clash.status_code, 400)
        self.assertIn("already registered", str(clash.data["card_code"]))


class LeaveRequestTests(BaseTestCase):
    def ask(self, user=None, frm=None, to=None, reason="Family function out of town", leave_type="CASUAL"):
        self.as_user(user or self.t1)
        today = timezone.localdate()
        frm = frm or today + datetime.timedelta(days=1)
        to = to or frm + datetime.timedelta(days=2)
        return self.client.post("/api/leave-requests/", {"leave_type": leave_type, "from_date": str(frm), "to_date": str(to), "reason": reason}, format="json")

    def review(self, rid, verb="approve", remarks="Ok"):
        self.as_user(self.hod_cs)
        return self.client.post(f"/api/leave-requests/{rid}/{verb}/", {"remarks": remarks}, format="json")

    def test_medical_leave_is_for_permanent_staff_only(self):
        from accounts.models import StaffProfile
        StaffProfile.objects.update_or_create(user=self.t1, defaults={"employment": "PROVISIONAL"})
        self.t1.refresh_from_db()
        r = self.ask(leave_type="SICK")
        self.assertEqual(r.status_code, 400)
        self.assertIn("leave_type", r.data)
        self.assertEqual(self.ask(leave_type="CASUAL").status_code, 201)
        StaffProfile.objects.filter(user=self.t1).update(employment="PERMANENT")
        self.t1.refresh_from_db()
        self.assertEqual(self.ask(leave_type="SICK", frm=timezone.localdate() + datetime.timedelta(days=20)).status_code, 201)

    def test_request_needs_a_reason_and_sane_dates(self):
        self.assertEqual(self.ask(reason="ok").status_code, 400)
        today = timezone.localdate()
        self.assertEqual(self.ask(frm=today + datetime.timedelta(days=3), to=today + datetime.timedelta(days=1)).status_code, 400)
        self.assertEqual(self.ask(frm=today - datetime.timedelta(days=30), to=today).status_code, 400)
        self.assertEqual(self.ask(frm=today, to=today + datetime.timedelta(days=40)).status_code, 400)

    def test_hod_cannot_request_and_overlap_is_blocked(self):
        self.assertEqual(self.ask(self.hod_cs).status_code, 403)
        self.assertEqual(self.ask().status_code, 201)
        self.assertEqual(self.ask().status_code, 400)

    def test_hod_gets_notified_and_approval_marks_leave_skipping_sundays(self):
        today = timezone.localdate()
        start = today + datetime.timedelta(days=1)
        r = self.ask(frm=start, to=start + datetime.timedelta(days=6))
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(self.hod_cs.notifications.filter(title="Leave request").exists())
        ok = self.review(r.data["id"])
        self.assertEqual(ok.status_code, 200, ok.data)
        self.assertEqual(ok.data["status"], "APPROVED")
        days = StaffAttendance.objects.filter(user=self.t1, status="LEAVE")
        self.assertEqual(days.count(), 6)  # 7 calendar days, one of them a Sunday
        self.assertFalse(any(d.date.weekday() == 6 for d in days))
        self.assertEqual(days.first().source, "LEAVEREQ")
        self.assertTrue(self.t1.notifications.filter(title="Leave approved").exists())
        self.assertTrue(AuditLog.objects.filter(action="LEAVE_APPROVED").exists())

    def test_approval_keeps_days_the_teacher_actually_came(self):
        day = timezone.localdate() + datetime.timedelta(days=1)
        while day.weekday() == 6:
            day += datetime.timedelta(days=1)
        StaffAttendance.objects.create(user=self.t1, date=day, status="PRESENT", source="SELF", marked_by=self.t1)
        r = self.ask(frm=day, to=day)
        self.review(r.data["id"])
        self.assertEqual(StaffAttendance.objects.get(user=self.t1, date=day).status, "PRESENT")

    def test_reject_marks_nothing_and_needs_remarks(self):
        r = self.ask()
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.post(f"/api/leave-requests/{r.data['id']}/reject/", {}, format="json").status_code, 400)
        self.assertEqual(self.review(r.data["id"], "reject", "Exams that week").data["status"], "REJECTED")
        self.assertFalse(StaffAttendance.objects.filter(user=self.t1).exists())
        self.assertEqual(self.review(r.data["id"]).status_code, 400)  # already reviewed

    def test_visibility_and_who_may_review(self):
        r = self.ask()
        self.as_user(self.t2)
        self.assertEqual(self.client.get("/api/leave-requests/").data["count"], 0)
        self.assertEqual(self.client.post(f"/api/leave-requests/{r.data['id']}/approve/", {"remarks": "x"}, format="json").status_code, 403)
        self.as_user(self.hod_math)
        self.assertEqual(self.client.get("/api/leave-requests/").data["count"], 0)
        self.assertEqual(self.client.post(f"/api/leave-requests/{r.data['id']}/approve/", {"remarks": "x"}, format="json").status_code, 404)
        self.as_user(self.admin)
        self.assertEqual(self.client.get("/api/leave-requests/").data["count"], 1)
        self.assertEqual(self.client.post(f"/api/leave-requests/{r.data['id']}/approve/", {"remarks": "x"}, format="json").status_code, 403)

    def test_teacher_can_cancel_only_while_pending(self):
        r = self.ask()
        self.as_user(self.t1)
        self.assertEqual(self.client.post(f"/api/leave-requests/{r.data['id']}/cancel/").data["status"], "CANCELLED")
        self.assertEqual(self.client.post(f"/api/leave-requests/{r.data['id']}/cancel/").status_code, 400)
        r2 = self.ask()
        self.review(r2.data["id"])
        self.as_user(self.t1)
        self.assertEqual(self.client.post(f"/api/leave-requests/{r2.data['id']}/cancel/").status_code, 400)


class CardMixTests(BaseTestCase):
    """A teacher and a student may carry the same member number; the scanners must still never mix them up."""

    def test_a_teacher_and_a_student_may_have_the_same_number(self):
        self.as_user(self.hod_cs)
        s = self.students[0]
        self.assertEqual(self.client.patch(f"/api/students/{s.id}/", {"card_code": "018822"}, format="json").status_code, 200)
        r = self.client.patch(f"/api/staff/{self.t1.id}/", {"card_code": " 018822 "}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        s.refresh_from_db(); self.t1.profile.refresh_from_db()
        self.assertEqual((s.card_code, self.t1.profile.card_code), ("018822", "018822"))
        # ...but two students (or two teachers) still cannot share one card
        self.assertEqual(self.client.patch(f"/api/students/{self.students[1].id}/", {"card_code": "018822"}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(f"/api/staff/{self.t2.id}/", {"card_code": "018822"}, format="json").status_code, 400)

    def test_identify_says_which_kinds_of_card_a_code_belongs_to(self):
        self.t1.profile.card_code = "EMP-ST1"
        self.t1.profile.save()
        s = self.students[0]
        s.card_code = "S-55"
        s.save()
        self.students[1].card_code = "BOTH1"
        self.students[1].save()
        self.t2.profile.card_code = "BOTH1"
        self.t2.profile.save()
        self.as_user(self.t2)
        ask = lambda code: self.client.post("/api/scan/identify/", {"code": code}, format="json").data
        self.assertEqual(ask("emp-st1"), {"staff": True, "student": False})
        self.assertEqual(ask(" s-55 "), {"staff": False, "student": True})
        self.assertEqual(ask("both1"), {"staff": True, "student": True})
        self.assertEqual(ask("nothing"), {"staff": False, "student": False})
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.post("/api/scan/identify/", {"code": "S-55"}, format="json").status_code, 401)


class OnLeaveListTests(BaseTestCase):
    def test_admin_sees_everyone_on_leave_today_and_the_dashboard_counts_them(self):
        from .models import LeaveRequest
        StaffAttendance.objects.create(user=self.t1, date=self.today, status="LEAVE", source="LEAVEREQ", marked_by=self.hod_cs)
        StaffAttendance.objects.create(user=self.t_math, date=self.today, status="LEAVE", source="HOD", marked_by=self.hod_math)
        StaffAttendance.objects.create(user=self.t2, date=self.today, status="PRESENT", source="SELF")
        LeaveRequest.objects.create(user=self.t1, from_date=self.today, to_date=self.today + datetime.timedelta(days=2),
                                    reason="Wedding in the family", status="APPROVED", reviewed_by=self.hod_cs)
        self.as_user(self.admin)
        r = self.client.get("/api/staff-attendance/on-leave/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["count"], 2)
        self.assertEqual({x["department"] for x in r.data["results"]}, {"Computer Science", "Mathematics"})
        mine = next(x for x in r.data["results"] if x["user"] == self.t1.id)
        self.assertEqual(mine["reason"], "Wedding in the family")
        self.assertEqual(self.client.get("/api/dashboard/").data["cards"]["staff_on_leave"], 2)
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.get("/api/staff-attendance/on-leave/").data["count"], 1)
        self.as_user(self.t1)
        self.assertEqual(self.client.get("/api/staff-attendance/on-leave/").status_code, 403)


class LeaveTypeAndDashboardTests(BaseTestCase):
    def ask(self, **kw):
        self.as_user(self.t1)
        day = timezone.localdate() + datetime.timedelta(days=1)
        while day.weekday() == 6:  # a leave never marks a Sunday, so do not test on one
            day += datetime.timedelta(days=1)
        body = {"from_date": str(day), "to_date": str(day), "reason": "Medical check up", **kw}
        return self.client.post("/api/leave-requests/", body, format="json")

    def test_type_is_required_and_must_be_one_of_the_list(self):
        self.assertEqual(self.ask().status_code, 400)
        self.assertEqual(self.ask(leave_type="HOLIDAY").status_code, 400)
        r = self.ask(leave_type="SICK")
        self.assertEqual(r.status_code, 201)
        self.assertEqual((r.data["leave_type"], r.data["leave_type_label"]), ("SICK", "ML – Medical leave"))

    def test_type_shows_in_notification_filter_and_attendance_note(self):
        from core.models import Notification
        rid = self.ask(leave_type="DUTY").data["id"]
        self.assertIn("duty leave", Notification.objects.filter(user=self.hod_cs).latest("id").message.lower())
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.get("/api/leave-requests/?leave_type=DUTY").data["count"], 1)
        self.assertEqual(self.client.get("/api/leave-requests/?leave_type=SICK").data["count"], 0)
        self.client.post(f"/api/leave-requests/{rid}/approve/", {"remarks": "ok"}, format="json")
        self.assertIn("DL – Duty leave", StaffAttendance.objects.get(user=self.t1).note)

    def test_leave_download_by_role(self):
        self.ask(leave_type="SICK")
        self.as_user(self.t1)
        t = self.client.get("/api/downloads/leave/?export=csv").content.decode("utf-8")
        self.assertIn("ML – Medical leave", t)
        self.assertTrue(t.startswith("\ufeff"))
        self.as_user(self.t2)
        self.assertNotIn("Sick", self.client.get("/api/downloads/leave/?export=csv").content.decode("utf-8"))
        self.as_user(self.hod_math)
        self.assertNotIn("Sick", self.client.get("/api/downloads/leave/?export=csv").content.decode("utf-8"))
        self.as_user(self.hod_cs)
        self.assertIn("Medical", self.client.get("/api/downloads/leave/?export=csv&leave_type=SICK").content.decode("utf-8"))
        self.assertNotIn("Sick", self.client.get("/api/downloads/leave/?export=csv&leave_type=DUTY").content.decode("utf-8"))
        self.as_user(self.admin)
        self.assertEqual(self.client.get("/api/downloads/leave/?export=pdf").content[:4], b"%PDF")

    def test_admin_dashboard_extra_boxes(self):
        today = timezone.localdate()
        from staffattendance.models import LeaveRequest
        LeaveRequest.objects.create(user=self.t1, leave_type="SICK", from_date=today, to_date=today, reason="Fever", status="APPROVED")
        LeaveRequest.objects.create(user=self.t2, leave_type="CASUAL", from_date=today, to_date=today, reason="Work", status="PENDING")
        StaffAttendance.objects.create(user=self.t1, date=today, status="LEAVE", source="LEAVEREQ")
        StaffAttendance.objects.create(user=self.t2, date=today, status="PRESENT", source="HOD")
        self.as_user(self.admin)
        d = self.client.get("/api/dashboard/").data
        self.assertEqual((d["staff_today"]["leave"], d["staff_today"]["present"]), (1, 1))
        self.assertEqual(d["staff_today"]["not_marked"], 1)  # the Maths teacher
        self.assertEqual([r["type"] for r in d["leave_month"]], ["SICK"])
        self.assertEqual(d["waiting"]["leave_requests"], 1)
        self.assertIn("students", d["low_attendance"])
        self.assertIsInstance(d["recent_activity"], list)
        self.as_user(self.hod_cs)
        self.assertNotIn("staff_today", self.client.get("/api/dashboard/").data)


class LeaveInProcessTests(BaseTestCase):
    ask = LeaveRequestTests.ask
    def act(self, rid, verb, user=None, body=None):
        self.as_user(user or self.hod_cs)
        return self.client.post(f"/api/leave-requests/{rid}/{verb}/", body or {}, format="json")

    def test_hod_can_mark_in_process_back_to_pending_then_approve(self):
        from core.models import Notification
        rid = self.ask().data["id"]
        r = self.act(rid, "processing", body={"remarks": "Finding a substitute"})
        self.assertEqual((r.status_code, r.data["status"]), (200, "PROCESSING"))
        self.assertIn("Finding a substitute", Notification.objects.filter(user=self.t1).latest("id").message)
        self.assertEqual(StaffAttendance.objects.filter(user=self.t1).count(), 0)  # nothing marked yet
        self.assertEqual(self.act(rid, "processing").status_code, 400)             # already in process
        self.assertEqual(self.act(rid, "pending").data["status"], "PENDING")
        self.assertEqual(self.act(rid, "pending").status_code, 400)
        self.act(rid, "processing")
        self.assertEqual(self.act(rid, "approve", body={"remarks": "Done"}).data["status"], "APPROVED")
        self.assertTrue(StaffAttendance.objects.filter(user=self.t1, status="LEAVE").exists())
        self.assertEqual(self.act(rid, "processing").status_code, 400)             # decided: cannot go back

    def test_in_process_can_be_rejected_and_cancelled_and_still_blocks_a_second_request(self):
        rid = self.ask().data["id"]
        self.act(rid, "processing")
        self.assertEqual(self.ask().status_code, 400)  # same days again
        self.as_user(self.t1)
        self.assertEqual(self.client.post(f"/api/leave-requests/{rid}/cancel/").data["status"], "CANCELLED")
        rid2 = self.ask().data["id"]
        self.act(rid2, "processing")
        self.assertEqual(self.act(rid2, "reject", body={"remarks": "Exams"}).data["status"], "REJECTED")

    def test_only_the_own_hod_may_and_open_filter_lists_both(self):
        a, b = self.ask(frm=timezone.localdate() + datetime.timedelta(days=1)).data["id"], self.ask(frm=timezone.localdate() + datetime.timedelta(days=10)).data["id"]
        self.assertEqual(self.act(a, "processing", user=self.t1).status_code, 403)
        self.assertEqual(self.act(a, "processing", user=self.hod_math).status_code, 404)
        self.assertEqual(self.act(a, "processing", user=self.admin).status_code, 403)
        self.act(a, "processing")
        self.as_user(self.hod_cs)
        get = lambda q: self.client.get(f"/api/leave-requests/?status={q}").data["count"]
        self.assertEqual((get("OPEN"), get("PENDING"), get("PROCESSING"), get("APPROVED"), get("nonsense")), (2, 1, 1, 0, 0))
