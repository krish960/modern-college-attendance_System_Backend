"""API views for class notes.

Teachers share notes (text and/or one file) with a class they teach, and can switch a secret public link on or off for
each class. Students open that link without signing in (the Public* views at the bottom); they can only read and download."""
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.http import FileResponse, Http404
from django.utils import timezone
from rest_framework import mixins, serializers, viewsets
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from academics.models import ClassRoom, Subject, TeacherSubjectAssignment
from accounts.models import User
from accounts.permissions import IsStaffTeacher
from core.audit import log_action
from core.scoping import department_scope
from students.serializers import can_access_classroom

from . import files, public_page
from .models import ClassShare, Note, new_token


class PublicThrottle(AnonRateThrottle):
    """Rate limit for people who are not signed in, so the public link cannot be hammered."""
    scope = "public"


class NoteSerializer(serializers.ModelSerializer):
    """Checks and saves a note. `upload` is only for writing; the file name and size are filled in by the server."""
    classroom = serializers.PrimaryKeyRelatedField(queryset=ClassRoom.objects.filter(is_active=True))
    subject = serializers.PrimaryKeyRelatedField(queryset=Subject.objects.filter(is_active=True), required=False, allow_null=True)
    classroom_name = serializers.CharField(source="classroom.label", read_only=True)
    subject_name = serializers.CharField(source="subject.name", read_only=True, default=None)
    uploaded_by_name = serializers.CharField(source="uploaded_by.display_name", read_only=True)
    upload = serializers.FileField(write_only=True, required=False)
    title = serializers.CharField(max_length=120, trim_whitespace=True)
    body = serializers.CharField(required=False, allow_blank=True, max_length=5000)

    class Meta:
        model = Note
        fields = ["id", "classroom", "classroom_name", "subject", "subject_name", "title", "body", "upload", "file_name",
                  "file_size", "uploaded_by", "uploaded_by_name", "created_at"]
        read_only_fields = ["id", "file_name", "file_size", "uploaded_by", "created_at"]

    def validate_classroom(self, classroom):
        """A teacher may share notes only with a class they teach (HOD/Admin follow the same access rule)."""
        if not can_access_classroom(self.context["request"].user, classroom):
            raise serializers.ValidationError("You can share notes only with classes you teach.")
        return classroom

    def validate_upload(self, upload):
        """Check the file type and size; the cleaned file name is kept to be saved in create()."""
        self._name, _ = files.check_upload(upload, settings.NOTE_MAX_MB)
        return upload

    def validate(self, attrs):
        """Cross-field checks: the subject must belong to the class's department, a teacher must teach it, and the note must have a file or text."""
        user = self.context["request"].user
        subject, classroom = attrs.get("subject"), attrs["classroom"]
        if subject:
            if subject.department_id != classroom.department_id:
                raise serializers.ValidationError({"subject": "This subject belongs to another department."})
            if user.role == User.Role.STAFF and not TeacherSubjectAssignment.objects.filter(
                    teacher=user, classroom=classroom, subject=subject, is_active=True).exists():
                raise serializers.ValidationError({"subject": "You do not teach this subject in this class."})
        if not attrs.get("upload") and not (attrs.get("body") or "").strip():
            raise serializers.ValidationError("Add a file or write the note text.")
        return attrs

    def create(self, validated_data):
        """Save the note with the logged-in user as uploader and store the file if there is one."""
        upload = validated_data.pop("upload", None)
        note = Note(**validated_data, uploaded_by=self.context["request"].user)
        if upload:
            note.file_name, note.file_size = self._name, upload.size
            note.file.save(upload.name, upload, save=False)
        note.save()
        return note


class NoteViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """Teachers only: share notes with a class they teach, list them, delete their own."""

    permission_classes = [IsStaffTeacher]
    serializer_class = NoteSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    filterset_fields = ["classroom", "subject"]
    search_fields = ["title", "body"]

    def get_queryset(self):
        """Notes in the user's department; a teacher sees only notes of classes they teach."""
        user = self.request.user
        qs = Note.objects.select_related("classroom", "subject", "uploaded_by")
        qs = department_scope(qs, user, "classroom__department_id")
        if user.role == User.Role.STAFF:
            taught = TeacherSubjectAssignment.objects.filter(teacher=user, is_active=True).values_list("classroom_id", flat=True)
            qs = qs.filter(classroom_id__in=list(taught))
        return qs

    @transaction.atomic
    def perform_create(self, serializer):
        """Save the note and write it to the audit log (both succeed or both are undone)."""
        note = serializer.save()
        log_action(self.request.user, "NOTE_SHARED", note, department=note.classroom.department,
                   new={"title": note.title, "class": note.classroom.label, "file": note.file_name})

    def destroy(self, request, *args, **kwargs):
        """Delete a note and its stored file. Only the teacher who shared it may do this."""
        note = self.get_object()
        if note.uploaded_by_id != request.user.id:
            raise ValidationError("Only the teacher who shared a note can remove it.")
        log_action(request.user, "NOTE_REMOVED", note, department=note.classroom.department, old={"title": note.title, "class": note.classroom.label})
        if note.file:
            note.file.delete(save=False)
        note.delete()
        return Response(status=204)


MIN_MINUTES, MAX_MINUTES = 5, 30 * 24 * 60  # a link stays open for 5 minutes up to 30 days


def share_payload(share):
    """The state of a class link as JSON: is it open now, its token, when it ends and how many seconds are left."""
    is_open = bool(share and share.is_open)
    return {
        "enabled": is_open,                                   # the link works right now
        "token": share.token if is_open else None,
        "expires_at": share.expires_at if share else None,
        "expired": bool(share and share.is_active and share.expires_at and not is_open),
        "seconds_left": max(0, int((share.expires_at - timezone.now()).total_seconds())) if is_open else 0,
    }


class ShareView(APIView):
    """GET ?classroom=ID -> the class's link state.  POST {classroom, action: enable | new_link | disable}."""

    permission_classes = [IsStaffTeacher]

    def _classroom(self, request, value):
        """Find the active class from an id and make sure this teacher may use it (otherwise "No such class")."""
        try:
            classroom = ClassRoom.objects.get(pk=int(value), is_active=True)
        except (ValueError, TypeError, ClassRoom.DoesNotExist):
            raise NotFound("No such class.")
        if not can_access_classroom(request.user, classroom):
            raise NotFound("No such class.")
        return classroom

    def get(self, request):
        """Show the current link state of one class."""
        classroom = self._classroom(request, request.query_params.get("classroom"))
        return Response(share_payload(ClassShare.objects.filter(classroom=classroom).first()))

    @transaction.atomic
    def post(self, request):
        """Turn the link on (enable), replace it with a new address (new_link) or turn it off (disable)."""
        classroom = self._classroom(request, request.data.get("classroom"))
        action = request.data.get("action")
        if action not in ("enable", "new_link", "disable"):
            raise ValidationError({"action": "Use enable, new_link or disable."})
        # lock the row so two teachers cannot change the same class link at the same moment
        share, _ = ClassShare.objects.select_for_update().get_or_create(classroom=classroom, defaults={"created_by": request.user})
        minutes = None  # how long the link stays open; not needed when switching it off
        if action != "disable":
            try:
                minutes = int(request.data.get("minutes"))
            except (TypeError, ValueError):
                raise ValidationError({"minutes": "Choose how long the link stays open."})
            if not MIN_MINUTES <= minutes <= MAX_MINUTES:
                raise ValidationError({"minutes": "Choose between 5 minutes and 30 days."})
        if action == "enable":
            # a link that is open is only extended; one that was off or has run out gets a fresh address (an old, leaked one never wakes up)
            if not share.is_open:
                share.token, share.rotated_at = new_token(), timezone.now()
            share.is_active, share.expires_at = True, timezone.now() + timedelta(minutes=minutes)
        elif action == "new_link":  # the old link stops working at once
            share.token, share.is_active, share.rotated_at = new_token(), True, timezone.now()
            share.expires_at = timezone.now() + timedelta(minutes=minutes)
        else:
            share.is_active = False
        share.save()
        log_action(request.user, f"NOTES_LINK_{action.upper()}", classroom, department=classroom.department,
                   new={"class": classroom.label, **({"minutes": minutes} if minutes else {})})
        return Response(share_payload(share))


class PublicBase(APIView):
    """
    No sign-in. Anyone who has the secret link can read until it runs out; nothing can be changed and no student data is
    shown. The student pages are plain HTML (no app, no script files, no address of the staff system anywhere).
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [PublicThrottle]

    def share(self, token):
        """Find the active link for this secret token (or None)."""
        return ClassShare.objects.select_related("classroom__department").filter(token=str(token), is_active=True).first()

    def handle_exception(self, exc):  # a throttled or odd request gets the same plain page, never JSON or a stack trace
        from rest_framework.exceptions import Throttled
        if isinstance(exc, Throttled):
            return public_page.message_page("Please wait a moment", "Too many requests. Try again in a minute.", 429)
        if isinstance(exc, (NotFound, Http404)):
            return public_page.message_page("Link not available", "This link is not available. Ask your teacher for the current link.", 404)
        return super().handle_exception(exc)


class PublicNotesView(PublicBase):
    """The page students open with the secret link: shows the class notes (latest 200) while the link is open."""

    def get(self, request, token):
        share = self.share(token)
        if share is None or not share.classroom.is_active:  # unknown, switched-off or inactive class
            return public_page.message_page("Link not available", "This link is not available. Ask your teacher for the current link.", 404)
        if not share.is_open:  # the link existed but its time is over (HTTP 410 Gone)
            return public_page.message_page("This link has closed", "The time for this link is over. Ask your teacher if the notes are shared again.", 410)
        notes = list(Note.objects.filter(classroom=share.classroom).select_related("subject", "uploaded_by")[:200])
        return public_page.notes_page(share, notes)


class PublicFileView(PublicBase):
    """Lets a student download one note's file through the secret link, only while the link is open."""

    def get(self, request, token, note_id):
        share = self.share(token)
        if share is None or not share.is_open or not share.classroom.is_active:
            return public_page.message_page("Link not available", "This link is not available or has closed. Ask your teacher.", 404)
        # the note must belong to the class of this link, so one link cannot open another class's files
        note = Note.objects.filter(pk=note_id, classroom=share.classroom).exclude(file="").first()
        if note is None:
            raise NotFound()
        try:
            handle = note.file.open("rb")
        except FileNotFoundError:
            raise Http404
        # these headers stop the browser from running or indexing the file and keep it out of caches
        response = FileResponse(handle, as_attachment=True, filename=note.file_name, content_type=files.content_type_for(note.file_name))
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = "sandbox; default-src 'none'"
        response["Cache-Control"] = "private, no-store"
        response["X-Robots-Tag"] = "noindex, nofollow"
        return response
