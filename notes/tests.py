import shutil
import tempfile
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from core.models import AuditLog
from core.testing import BaseTestCase

from .models import ClassShare, Note

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


class NotesTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.override = override_settings(UPLOAD_DIR=Path(self.tmp), REST_FRAMEWORK={**__import__("django.conf").conf.settings.REST_FRAMEWORK,
                                          "DEFAULT_THROTTLE_RATES": {**__import__("django.conf").conf.settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "public": "1000/min"}})
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def upload(self, user=None, name="unit1.pdf", data=PDF, title="Unit 1 notes", classroom=None, **extra):
        self.as_user(user or self.t1)
        body = {"classroom": (classroom or self.class_cs).id, "title": title, **extra}
        if name:
            body["upload"] = SimpleUploadedFile(name, data)
        return self.client.post("/api/notes/", body, format="multipart")

    def enable(self, user=None):
        self.as_user(user or self.t1)
        return self.client.post("/api/notes/share/", {"classroom": self.class_cs.id, "action": "enable", "minutes": 60}, format="json")

    def test_teacher_shares_a_pdf_and_it_is_audited(self):
        r = self.upload()
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["file_name"], "unit1.pdf")
        self.assertTrue(AuditLog.objects.filter(action="NOTE_SHARED").exists())
        stored = list(Path(self.tmp).rglob("*"))
        self.assertTrue(any(f.is_file() and "unit1" not in f.name for f in stored))  # stored under a random name

    def test_text_only_note_works_but_empty_note_does_not(self):
        self.assertEqual(self.upload(name=None, body="Read chapter 3 before Monday").status_code, 201)
        self.assertEqual(self.upload(name=None).status_code, 400)

    def test_bad_files_are_refused(self):
        self.assertEqual(self.upload(name="run.exe", data=b"MZ...").status_code, 400)
        self.assertEqual(self.upload(name="fake.pdf", data=b"<html>not a pdf</html>").status_code, 400)
        self.assertEqual(self.upload(name="empty.pdf", data=b"").status_code, 400)
        with override_settings(NOTE_MAX_MB=0):
            self.assertEqual(self.upload().status_code, 400)
        self.assertEqual(self.upload(name="../../evil.pdf").status_code, 201)  # path part is dropped, not followed
        self.assertEqual(Note.objects.get().file_name, "evil.pdf")

    def test_only_teachers_of_the_class_can_share_or_manage_the_link(self):
        self.assertEqual(self.upload(user=self.t_math).status_code, 400)  # prof_m does not teach class_cs
        self.as_user(self.t_math)
        self.assertEqual(self.client.post("/api/notes/share/", {"classroom": self.class_cs.id, "action": "enable", "minutes": 60}, format="json").status_code, 404)
        self.as_user(self.admin)
        self.assertEqual(self.client.get("/api/notes/").status_code, 403)
        self.as_user(self.hod_math)
        self.assertEqual(self.client.post("/api/notes/share/", {"classroom": self.class_cs.id, "action": "enable", "minutes": 60}, format="json").status_code, 403)

    def test_the_hod_has_no_notes_pages(self):
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.get("/api/notes/").status_code, 403)
        self.assertEqual(self.client.get(f"/api/notes/share/?classroom={self.class_cs.id}").status_code, 403)
        self.assertEqual(self.client.post("/api/notes/share/", {"classroom": self.class_cs.id, "action": "enable", "minutes": 60}, format="json").status_code, 403)

    def test_students_open_the_link_without_signing_in(self):
        self.upload()
        token = self.enable().data["token"]
        self.client.force_authenticate(user=None)
        page = self.client.get(f"/s/{token}/")
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertIn(self.class_cs.label, html)
        self.assertIn("Unit 1 notes", html)
        self.assertIn("unit1.pdf", html)
        for s in self.students:
            self.assertNotIn(s.name, html)  # no student data on the public page
        # the page gives nothing away about the system behind it
        for word in ("/api/", "login", "Django", "React", "token", "Sign in", "admin"):
            self.assertNotIn(word.lower(), html.replace(token, "").lower(), word)
        self.assertIn("default-src 'none'", page.headers["Content-Security-Policy"])
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertIn("noindex", page.headers["X-Robots-Tag"])
        note_id = Note.objects.get().id
        dl = self.client.get(f"/s/{token}/f/{note_id}/")
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(b"".join(dl.streaming_content), PDF)
        self.assertIn("attachment", dl.headers["Content-Disposition"])
        self.assertEqual(dl.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(dl.headers["Content-Type"], "application/pdf")

    def test_the_teacher_sets_how_long_the_link_stays_open(self):
        self.upload()
        r = self.enable()
        token = r.data["token"]
        self.assertTrue(r.data["enabled"])
        self.assertTrue(3500 < r.data["seconds_left"] <= 3600)
        note_id = Note.objects.get().id
        self.as_user(self.t1)
        for bad in ({}, {"minutes": 2}, {"minutes": 99999}, {"minutes": "soon"}):
            r = self.client.post("/api/notes/share/", {"classroom": self.class_cs.id, "action": "enable", **bad}, format="json")
            self.assertEqual(r.status_code, 400, bad)
        # time runs out: page and file both stop, and the teacher sees it expired
        ClassShare.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.client.force_authenticate(user=None)
        closed = self.client.get(f"/s/{token}/")
        self.assertEqual(closed.status_code, 410)
        self.assertNotIn("Unit 1 notes", closed.content.decode())
        self.assertEqual(self.client.get(f"/s/{token}/f/{note_id}/").status_code, 404)
        self.as_user(self.t1)
        state = self.client.get(f"/api/notes/share/?classroom={self.class_cs.id}").data
        self.assertFalse(state["enabled"])
        self.assertTrue(state["expired"])
        self.assertIsNone(state["token"])
        # opening it again gives a NEW address; the old one never wakes up
        again = self.enable().data
        self.assertTrue(again["enabled"])
        self.assertNotEqual(again["token"], token)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(f"/s/{token}/").status_code, 404)
        self.assertEqual(self.client.get(f"/s/{again['token']}/").status_code, 200)

    def test_extending_an_open_link_keeps_the_same_address(self):
        token = self.enable().data["token"]
        self.as_user(self.t1)
        longer = self.client.post("/api/notes/share/", {"classroom": self.class_cs.id, "action": "enable", "minutes": 1440}, format="json").data
        self.assertEqual(longer["token"], token)
        self.assertTrue(86000 < longer["seconds_left"] <= 86400)

    def test_wrong_disabled_or_replaced_link_shows_nothing(self):
        self.upload()
        token = self.enable().data["token"]
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get("/s/not-a-real-token/").status_code, 404)
        self.as_user(self.t1)
        new = self.client.post("/api/notes/share/", {"classroom": self.class_cs.id, "action": "new_link", "minutes": 60}, format="json").data["token"]
        self.assertNotEqual(new, token)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(f"/s/{token}/").status_code, 404)  # old link is dead
        self.assertEqual(self.client.get(f"/s/{new}/").status_code, 200)
        self.as_user(self.t1)
        self.client.post("/api/notes/share/", {"classroom": self.class_cs.id, "action": "disable"}, format="json")
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(f"/s/{new}/").status_code, 404)

    def test_a_link_never_shows_another_classes_files(self):
        self.upload()
        token = self.enable().data["token"]
        other = Note.objects.create(classroom=self.class_math, title="Math", uploaded_by=self.t_math, file_name="m.pdf")
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(f"/s/{token}/f/{other.id}/").status_code, 404)

    def test_note_text_cannot_inject_html_into_the_student_page(self):
        self.upload(title="<script>alert(1)</script>", body="<img src=x onerror=alert(2)>")
        token = self.enable().data["token"]
        self.client.force_authenticate(user=None)
        html = self.client.get(f"/s/{token}/").content.decode()
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;script&gt;alert(1)", html)

    def test_public_link_cannot_change_anything(self):
        token = self.enable().data["token"]
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.post(f"/s/{token}/", {}).status_code, 405)
        self.assertEqual(self.client.delete(f"/s/{token}/").status_code, 405)
        self.assertEqual(self.client.get("/api/notes/").status_code, 401)
        self.assertEqual(self.client.get(f"/api/public/notes/{token}/").status_code, 404)  # the old JSON address is gone

    def test_remove_note_deletes_the_file_and_only_the_owner_may(self):
        r = self.upload()
        path = next(p for p in Path(self.tmp).rglob("*") if p.is_file())
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.delete(f"/api/notes/{r.data['id']}/").status_code, 403)
        self.as_user(self.t1)
        self.assertEqual(self.client.delete(f"/api/notes/{r.data['id']}/").status_code, 204)
        self.assertFalse(path.exists())
        r2 = self.upload()
        self.assertEqual(Note.objects.count(), 1)
        self.as_user(self.t1)
        self.assertEqual(self.client.delete(f"/api/notes/{r2.data['id']}/").status_code, 204)
