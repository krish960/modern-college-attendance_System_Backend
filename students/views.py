"""API views for students: register, edit, photo, CSV import, removal requests and class promotion.

Roles: a Teacher registers students only in classes they teach. The HOD manages the whole department
and decides removal requests. The Admin can correct students and gives the final approval for a class
promotion. Students are never hard deleted, they only become inactive (soft delete), so old attendance stays valid.
Every change is written to the audit log."""
import csv
import io

from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from academics.models import TeacherSubjectAssignment
from accounts.models import User
from accounts.permissions import IsAdminOrHOD, IsAnyStaffRole, IsHOD, IsHODOrStaff, IsStaffTeacher
from core.audit import log_action
from core.notifications import notify, notify_admins, notify_many
from core.ordering import roll_order
from core.scoping import department_scope

from academics.models import ClassRoom

from .photos import process_photo
from .promotion import carry_out, plan_promotion
from core.validators import card_text, person_name, roll_text
from rest_framework.exceptions import ValidationError as DRFValidationError

from .models import ClassPromotionRequest, Student, StudentRemovalRequest, normalize_card_code, normalize_roll_number
from attendance.serializers import ReviewSerializer
from .serializers import PromotionRequestSerializer, RemovalRequestSerializer, StudentSerializer, can_access_classroom


IMPORT_MAX_BYTES = 1_000_000
IMPORT_MAX_ROWS = 2000
ROLL_HEADERS = {"roll_number", "roll", "rollno", "roll_no", "rollnumber", "roll_no."}
# the column that holds the number/code printed on the ID card (the Member ID), so the card can be scanned afterwards
CARD_HEADERS = {"member_id", "memberid", "member_no", "member_number", "member", "card_code", "card_id", "card_no", "card_number", "card",
                "id_card", "id_card_no", "id_card_number", "idcard", "identity_card", "barcode", "scan_code"}
NAME_HEADERS = {"name", "student_name", "studentname", "student", "full_name", "fullname"}


def _clean_header(value):
    """Turn a CSV column title into a simple form, e.g. 'Roll No.' becomes 'roll_no', so different spellings match."""
    return "".join(ch if ch.isalnum() else "_" for ch in (value or "").strip().lower().replace(".", "")).strip("_")


def parse_student_csv(upload):
    """Read an uploaded CSV into [(line_number, roll, name, card)]. Raises ValidationError for unusable files."""
    if upload is None:
        raise ValidationError({"file": "Choose a CSV file."})
    if upload.size > IMPORT_MAX_BYTES:
        raise ValidationError({"file": "The file is larger than 1 MB."})
    try:
        text = upload.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValidationError({"file": "The file must be UTF-8 text (save as CSV UTF-8 from Excel)."})
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        raise ValidationError({"file": "The file is empty."})
    headers = [_clean_header(h) for h in rows[0]]
    roll_col = next((i for i, h in enumerate(headers) if h in ROLL_HEADERS), None)
    name_col = next((i for i, h in enumerate(headers) if h in NAME_HEADERS), None)
    if roll_col is None or name_col is None:
        raise ValidationError({"file": "The first row must have the columns: roll_number, name."})
    card_col = next((i for i, h in enumerate(headers) if h in CARD_HEADERS), None)
    body = rows[1:]
    if len(body) > IMPORT_MAX_ROWS:
        raise ValidationError({"file": f"At most {IMPORT_MAX_ROWS} students can be imported at once."})
    out = []
    for index, row in enumerate(body, start=2):
        roll = row[roll_col] if roll_col < len(row) else ""
        name = row[name_col] if name_col < len(row) else ""
        card = row[card_col] if card_col is not None and card_col < len(row) else ""
        out.append((index, roll, name, card))
    return out


class StudentViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                     mixins.UpdateModelMixin, viewsets.GenericViewSet):
    """
    HOD: all students of the department. Staff: students of classes they teach.
    Admin: read-only across departments. Students are never hard-deleted (attendance history depends on them): the HOD
    sets `is_active` to false, and a teacher sends a removal request (StudentRemovalViewSet) for the HOD to approve.
    """

    serializer_class = StudentSerializer
    search_fields = ["roll_number", "name"]
    filterset_fields = ["classroom", "is_active", "created_by", "department"]
    ordering_fields = ["roll_number", "name", "created_at"]

    def get_permissions(self):
        """Reading is open to any signed-in role; changing a student is limited to staff roles; registering and importing is HOD or Teacher only."""
        if self.action in ("list", "retrieve") or (self.action == "photo" and self.request.method == "GET"):
            return super().get_permissions()
        if self.action == "promote":
            return [IsAnyStaffRole()]
        if self.action in ("update", "partial_update", "photo"):
            return [IsAnyStaffRole()]  # the college admin may correct a student, but never registers or imports
        return [IsHODOrStaff()]

    def get_queryset(self):
        """Students the user may see: the department scope for HOD/Admin, and only taught classes for a Teacher."""
        user = self.request.user
        qs = Student.objects.select_related("classroom", "department", "created_by")
        if self.action != "photo":
            qs = qs.defer("photo")  # the image bytes are only loaded when the photo itself is asked for
        qs = department_scope(qs, user)
        if user.role == User.Role.STAFF:
            mine = TeacherSubjectAssignment.objects.filter(teacher=user, is_active=True)
            qs = qs.filter(classroom_id__in=mine.values("classroom_id"))
        return qs.order_by("classroom_id", *roll_order())

    @action(detail=True, methods=["get", "post", "delete"], parser_classes=[MultiPartParser, FormParser])
    def photo(self, request, pk=None):
        """GET the photo (any signed-in user who can see the student), POST to set or replace it, DELETE to remove it."""
        student = self.get_object()  # same department / class scoping as the student itself
        if request.method == "GET":
            if not student.photo:
                raise NotFound("This student has no photo.")
            response = HttpResponse(bytes(student.photo), content_type="image/jpeg")
            response["Cache-Control"] = "private, max-age=86400"
            return response
        if request.method == "POST":
            if request.user.role == User.Role.STAFF and not can_access_classroom(request.user, student.classroom):
                raise NotFound()
            student.photo = process_photo(request.FILES.get("photo"))
            student.photo_updated_at = timezone.now()
            action_name = "STUDENT_PHOTO_UPDATED"
        else:
            student.photo = None
            student.photo_updated_at = None
            action_name = "STUDENT_PHOTO_REMOVED"
        student.save(update_fields=["photo", "photo_updated_at", "updated_at"])
        log_action(request.user, action_name, student, department=student.department,
                   new={"roll_number": student.roll_number, "name": student.name})
        return Response(self.get_serializer(self.get_queryset().get(pk=student.pk)).data)

    @action(detail=False, methods=["post"])
    def promote(self, request):
        """
        PREVIEW only: who would move if this class were promoted. Nothing is changed here.
        The real promotion goes through ClassPromotionViewSet (HOD approval, then admin approval).
        Body: from_classroom, to_classroom, exclude = [student ids to leave behind].
        """
        source, target = promotion_classes(request)
        moving, left, clashes = plan_promotion(source, target, promotion_ids(request.data.get("exclude")))
        return Response({
            "from": source.label, "to": target.label, "moved": len(moving),
            "left_behind": [{"id": st.id, "roll_number": st.roll_number, "name": st.name} for st in left],
            "roll_clashes": [{"id": st.id, "roll_number": st.roll_number, "name": st.name} for st in clashes],
        })

    @action(detail=False, methods=["post"], url_path="import", parser_classes=[MultiPartParser, FormParser, JSONParser])
    def import_csv(self, request):
        """
        Register many students into one class from a CSV (columns: roll_number, name, and optionally member_id = the number on the ID card).
        Send dry_run=true to only check the file. Existing roll numbers are skipped, never overwritten.
        """
        class_id = str(request.data.get("classroom", ""))
        if not class_id.isdigit():
            raise ValidationError({"classroom": "Choose a class."})
        classroom = ClassRoom.objects.filter(pk=class_id, is_active=True).select_related("department").first()
        if classroom is None or not can_access_classroom(request.user, classroom):
            raise ValidationError({"classroom": "You can only import into classes assigned to you."})
        dry_run = str(request.data.get("dry_run", "")).lower() in ("1", "true", "yes")

        parsed = parse_student_csv(request.FILES.get("file"))
        existing = {st.roll_number: st for st in Student.objects.filter(classroom=classroom).defer("photo")}
        cards_taken = set(Student.objects.exclude(card_code="").values_list("card_code", flat=True))  # one card = one student, college-wide
        seen, to_create, skipped, invalid, card_problems, add_cards = set(), [], [], [], [], []
        for line, raw_roll, raw_name, raw_card in parsed:
            roll = normalize_roll_number(raw_roll)
            name = " ".join(raw_name.split())
            card = normalize_card_code(raw_card)
            if card and len(card) > 100:
                card_problems.append({"line": line, "roll_number": roll, "card": card[:40], "reason": "The card number is longer than 100 characters."})
                card = ""
            elif card and card in cards_taken:
                holder = existing.get(roll)
                if not (holder and holder.card_code == card):  # his own card again is fine, not a problem
                    card_problems.append({"line": line, "roll_number": roll, "card": card, "reason": "This card number belongs to another student, so it was not linked."})
                card = ""
            # the same rules as the registration form: roll, name and card are checked by the shared validators
            problem = None
            try:
                roll_text(roll)
                person_name(name, "Student name", minimum=2)
                card_text(card)
            except DRFValidationError as e:
                problem = " ".join(str(m) for m in e.detail)
            if not roll or len(roll) > 20:
                invalid.append({"line": line, "roll_number": raw_roll, "error": "Roll number is missing or longer than 20 characters."})
            elif len(name) > 120:
                invalid.append({"line": line, "roll_number": roll, "error": "Student name must be 2 to 120 characters."})
            elif problem and roll not in existing:  # rows for students who are already registered are only skipped, never judged again
                invalid.append({"line": line, "roll_number": roll, "error": problem})
            elif roll in existing:
                holder = existing[roll]
                if card and not holder.card_code:  # a student registered earlier without a card: add the card, change nothing else
                    add_cards.append((holder, card))
                    cards_taken.add(card)
                    skipped.append({"line": line, "roll_number": roll, "reason": "Already registered in this class (his ID card number was added)."})
                else:
                    skipped.append({"line": line, "roll_number": roll, "reason": "Already registered in this class."})
            elif roll in seen:
                skipped.append({"line": line, "roll_number": roll, "reason": "Repeated in the file."})
            else:
                seen.add(roll)
                if card:
                    cards_taken.add(card)
                to_create.append(Student(classroom=classroom, department=classroom.department, roll_number=roll,
                                         name=name, card_code=card, created_by=request.user))
        created = len(to_create)
        if not dry_run and add_cards:
            with transaction.atomic():
                for holder, card in add_cards:
                    holder.card_code = card
                    holder.save(update_fields=["card_code", "updated_at"])
        if not dry_run and to_create:
            with transaction.atomic():
                # Another teacher may add the same roll at the same moment: the unique constraint decides.
                Student.objects.bulk_create(to_create, ignore_conflicts=True)
                created = Student.objects.filter(
                    classroom=classroom, roll_number__in=seen, created_by=request.user).count()
                log_action(request.user, "STUDENTS_IMPORTED", classroom, department=classroom.department,
                           new={"class": classroom.label, "registered": len(to_create), "skipped": len(skipped),
                                "invalid": len(invalid), "cards": sum(1 for st in to_create if st.card_code) + len(add_cards)})
        return Response({
            "dry_run": dry_run, "classroom": classroom.label, "rows": len(parsed),
            "created": created, "skipped": skipped, "invalid": invalid,
            "cards_linked": sum(1 for st in to_create if st.card_code) + len(add_cards), "card_problems": card_problems,
        })

    def create(self, request, *args, **kwargs):
        """Register one student. If the roll number already exists in the class, return the existing student (409) instead of making a duplicate."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        classroom = serializer.validated_data["classroom"]
        roll = serializer.validated_data["roll_number"]

        existing = Student.objects.filter(classroom=classroom, roll_number=roll).first()
        if existing is None:
            try:
                student = serializer.save(created_by=request.user, department=classroom.department)
            except IntegrityError:  # another teacher registered the same roll number (or ID card) a moment ago
                existing = Student.objects.filter(classroom=classroom, roll_number=roll).first()
                if existing is None:
                    raise ValidationError({"card_code": "This ID card was just registered to another student."})
            else:
                log_action(request.user, "STUDENT_REGISTERED", student, department=student.department,
                           new={"roll_number": student.roll_number, "name": student.name, "class": classroom.label})
                return Response(self.get_serializer(student).data, status=status.HTTP_201_CREATED)

        # Already registered (by this or another teacher): show it instead of duplicating.
        return Response(
            {
                "detail": f"Roll number {roll} already exists in {classroom.label}. No duplicate was created.",
                "code": "duplicate_student",
                "existing_student": self.get_serializer(existing).data,
            },
            status=status.HTTP_409_CONFLICT,
        )

    def perform_update(self, serializer):
        """Save the edit and write the old and new values to the audit log."""
        old = {"roll_number": serializer.instance.roll_number, "name": serializer.instance.name,
               "is_active": serializer.instance.is_active, "card_code": serializer.instance.card_code,
               "class": serializer.instance.classroom.label}
        student = serializer.save()
        log_action(self.request.user, "STUDENT_UPDATED", student, department=student.department, old=old,
                   new={"roll_number": student.roll_number, "name": student.name, "is_active": student.is_active, "card_code": student.card_code,
                       "class": student.classroom.label})


class StudentRemovalViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    Teacher: ask for a student of one of their classes to be removed (listed: own requests).
    HOD: sees the department's requests and approves (student becomes inactive) or rejects them.
    """

    serializer_class = RemovalRequestSerializer
    filterset_fields = ["status", "requested_by"]
    search_fields = ["student__name", "student__roll_number", "reason"]

    def get_permissions(self):
        """Only a Teacher asks for a removal; only the HOD decides on it."""
        if self.action == "create":
            return [IsStaffTeacher()]
        if self.action in ("approve", "reject"):
            return [IsHOD()]
        return super().get_permissions()

    def get_queryset(self):
        """Removal requests in the user's department; a Teacher sees only their own."""
        user = self.request.user
        qs = StudentRemovalRequest.objects.select_related("student__classroom", "requested_by", "reviewed_by")
        qs = department_scope(qs, user, "student__department_id")
        if user.role == User.Role.STAFF:
            qs = qs.filter(requested_by=user)
        return qs

    @transaction.atomic
    def perform_create(self, serializer):
        """Save the request, write it to the audit log and notify the department's HOD."""
        req = serializer.save(requested_by=self.request.user)
        student = req.student
        log_action(self.request.user, "STUDENT_REMOVAL_REQUESTED", student, department=student.department,
                   new={"roll_number": student.roll_number, "name": student.name}, reason=req.reason)
        hods = User.objects.filter(role=User.Role.HOD, department=student.department, is_active=True)
        notify_many(hods, "Student removal request",
                    f"{self.request.user.display_name} asks to remove {student.name} (roll {student.roll_number}, "
                    f"{student.classroom.label}): {req.reason}")

    @transaction.atomic
    def _review(self, request, approve):
        """Shared code for approve and reject. Approving makes the student inactive (soft delete) and tells the Admin."""
        req = StudentRemovalRequest.objects.select_for_update().select_related("student__classroom").get(pk=self.get_object().pk)
        data = ReviewSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        remarks = data.validated_data["remarks"]
        if req.status != StudentRemovalRequest.Status.PENDING:
            raise ValidationError("This request has already been reviewed.")
        student = req.student
        if approve:
            student.is_active = False
            student.save(update_fields=["is_active", "updated_at"])
            req.status = StudentRemovalRequest.Status.APPROVED
            log_action(request.user, "STUDENT_REMOVAL_APPROVED", student, department=student.department,
                       old={"is_active": True}, new={"is_active": False}, reason=req.reason, extra={"remarks": remarks, "requested_by": req.requested_by.username})
        else:
            req.status = StudentRemovalRequest.Status.REJECTED
            log_action(request.user, "STUDENT_REMOVAL_REJECTED", student, department=student.department,
                       reason=req.reason, extra={"remarks": remarks, "requested_by": req.requested_by.username})
        req.reviewed_by = request.user
        req.reviewed_at = timezone.now()
        req.remarks = remarks
        req.save(update_fields=["status", "reviewed_by", "reviewed_at", "remarks"])
        verdict = "approved" if approve else "rejected"
        notify(req.requested_by, f"Removal {verdict}",
               f"Your request to remove {student.name} (roll {student.roll_number}) was {verdict}. HOD: {remarks}")
        if approve:
            notify_admins("Student removed",
                          f"{student.department.name}: {student.name} (roll {student.roll_number}, {student.classroom.label}) was made inactive. "
                          f"Reason: {req.reason}")
        return Response(self.get_serializer(self.get_queryset().get(pk=req.pk)).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """HOD approves: the student becomes inactive."""
        return self._review(request, True)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        """HOD rejects: the student stays active."""
        return self._review(request, False)


# ----------------------------------------------------------------------------
# Class promotion: teacher or HOD asks, HOD approves, admin approves, then the students move
# ----------------------------------------------------------------------------


def promotion_ids(value):
    """The list of student ids sent by the browser, as clean integers (anything else is ignored)."""
    return sorted({int(x) for x in (value or []) if str(x).isdigit()})


def promotion_classes(request):
    """
    Read and check from_classroom / to_classroom of a promotion. They must be two different classes of the
    SAME department, the new class must be active, and the person must be allowed to touch the old class
    (HOD: own department; teacher: a class he teaches; admin: any).
    """
    def one(key):
        """Find one class from the request body or fail with a friendly message."""
        value = str(request.data.get(key, ""))
        room = ClassRoom.objects.filter(pk=value).select_related("department").first() if value.isdigit() else None
        if room is None:
            raise ValidationError({key: "Choose a class."})
        return room

    source, target = one("from_classroom"), one("to_classroom")
    user = request.user
    if user.role != User.Role.ADMIN and not can_access_classroom(user, source):
        raise ValidationError({"from_classroom": "Choose a class of your own department or a class you teach."})
    if source.pk == target.pk:
        raise ValidationError({"to_classroom": "Choose a different class to move the students to."})
    if source.department_id != target.department_id:
        raise ValidationError({"to_classroom": "Both classes must belong to the same department."})
    if not target.is_active:
        raise ValidationError({"to_classroom": "The new class is not active."})
    return source, target


class ClassPromotionViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    Teacher or HOD creates a request. Order of approval: HOD (only for a teacher's request), then the admin.
    Only the admin's approval moves the students. Anyone in the chain can reject, and the person who asked can cancel
    while it is still open. Every step is written to the audit log and the next person is notified.
    """

    serializer_class = PromotionRequestSerializer
    filterset_fields = ["status", "requested_by"]
    search_fields = ["from_classroom__name", "to_classroom__name", "requested_by__first_name", "requested_by__last_name"]

    def get_permissions(self):
        """Teacher or HOD may ask; any staff role (including Admin) may read and decide."""
        if self.action == "create":
            return [IsHODOrStaff()]
        return [IsAnyStaffRole()]

    def get_queryset(self):
        """Promotion requests in the user's department; a Teacher sees only their own."""
        user = self.request.user
        qs = ClassPromotionRequest.objects.select_related(
            "from_classroom", "to_classroom", "requested_by", "hod_by", "admin_by", "department")
        qs = department_scope(qs, user)
        if user.role == User.Role.STAFF:
            qs = qs.filter(requested_by=user)
        return qs

    @action(detail=False, methods=["get"])
    def targets(self, request):
        """Classes the chosen old class can move to: the other active classes of the same department (a teacher only teaches the old one)."""
        value = str(request.query_params.get("from", ""))
        source = ClassRoom.objects.filter(pk=value).first() if value.isdigit() else None
        if source is None or (request.user.role != User.Role.ADMIN and not can_access_classroom(request.user, source)):
            raise ValidationError({"from": "Choose a class."})
        rooms = ClassRoom.objects.filter(department_id=source.department_id, is_active=True).exclude(pk=source.pk)
        return Response([{"id": c.id, "label": c.label, "academic_year": c.academic_year} for c in rooms])

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        """Start a promotion request. A teacher's request waits for the HOD first; an HOD's request skips straight to the Admin."""
        source, target = promotion_classes(request)
        if ClassPromotionRequest.objects.filter(from_classroom=source, status__in=ClassPromotionRequest.OPEN).exists():
            raise ValidationError("A promotion request for this class is already waiting for approval.")
        user = request.user
        req = ClassPromotionRequest(
            department=source.department, from_classroom=source, to_classroom=target,
            leave_behind=promotion_ids(request.data.get("exclude")), note=str(request.data.get("note", ""))[:1000].strip(),
            requested_by=user)
        if user.role == User.Role.HOD:  # the HOD asking himself counts as his approval
            req.status = ClassPromotionRequest.Status.WAITING_ADMIN
            req.hod_by, req.hod_at, req.hod_remarks = user, timezone.now(), "Requested by the HOD."
        req.save()
        log_action(user, "CLASS_PROMOTION_REQUESTED", source, department=source.department,
                   new={"from": source.label, "to": target.label, "left_behind": len(req.leave_behind)})
        text = f"{user.display_name} asks to move {source.label} to {target.label}."
        if req.status == ClassPromotionRequest.Status.WAITING_HOD:
            notify_many(User.objects.filter(role=User.Role.HOD, department=source.department, is_active=True), "Class promotion request", text)
        else:
            notify_admins("Class promotion waiting for you", text)
        return Response(self.get_serializer(req).data, status=status.HTTP_201_CREATED)

    def _open_request(self, request, pk, role_step):
        """Lock the request and make sure it is this person's turn (HOD step or admin step)."""
        req = ClassPromotionRequest.objects.select_for_update().select_related("from_classroom", "to_classroom", "department").get(pk=self.get_object().pk)
        data = ReviewSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        waiting = {"HOD": ClassPromotionRequest.Status.WAITING_HOD, "ADMIN": ClassPromotionRequest.Status.WAITING_ADMIN}[role_step]
        if request.user.role != role_step:
            raise PermissionDenied("It is not your turn to decide on this request.")
        if req.status != waiting:
            raise ValidationError("This request is not waiting for your decision.")
        return req, data.validated_data["remarks"]

    @transaction.atomic
    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """HOD approves a teacher's request (it goes to the admin). The admin's approval carries out the promotion."""
        step = "ADMIN" if request.user.role == User.Role.ADMIN else "HOD"
        req, remarks = self._open_request(request, pk, step)
        now = timezone.now()
        label = f"{req.from_classroom.label} to {req.to_classroom.label}"
        if step == "HOD":
            req.status = ClassPromotionRequest.Status.WAITING_ADMIN
            req.hod_by, req.hod_at, req.hod_remarks = request.user, now, remarks
            req.save()
            log_action(request.user, "CLASS_PROMOTION_HOD_APPROVED", req.from_classroom, department=req.department, extra={"remarks": remarks})
            notify_admins("Class promotion waiting for you", f"{request.user.display_name} approved moving {label}. Your approval moves the students.")
            notify(req.requested_by, "Promotion approved by HOD", f"Your request to move {label} was approved by the HOD. It now waits for the admin.")
        else:
            moving, left, clashes = carry_out(req.from_classroom, req.to_classroom, req.leave_behind)
            req.status = ClassPromotionRequest.Status.DONE
            req.admin_by, req.admin_at, req.admin_remarks = request.user, now, remarks
            req.moved_count, req.clash_count = len(moving), len(clashes)
            req.save()
            log_action(request.user, "CLASS_PROMOTED", req.to_classroom, department=req.department,
                       new={"from": req.from_classroom.label, "to": req.to_classroom.label, "moved": len(moving),
                            "left_behind": len(left), "roll_clashes": len(clashes)}, extra={"remarks": remarks})
            done = f"{len(moving)} students moved from {label}." + (f" {len(clashes)} not moved: roll number already used." if clashes else "")
            notify(req.requested_by, "Class promoted", done)
            if req.hod_by_id and req.hod_by_id != req.requested_by_id:
                notify(req.hod_by, "Class promoted", done)
        return Response(self.get_serializer(req).data)

    @transaction.atomic
    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        """HOD (at his step) or admin (at his step) says no. Nobody moves."""
        step = "ADMIN" if request.user.role == User.Role.ADMIN else "HOD"
        req, remarks = self._open_request(request, pk, step)
        req.status = ClassPromotionRequest.Status.REJECTED
        req.rejected_by_role = step
        if step == "HOD":
            req.hod_by, req.hod_at, req.hod_remarks = request.user, timezone.now(), remarks
        else:
            req.admin_by, req.admin_at, req.admin_remarks = request.user, timezone.now(), remarks
        req.save()
        log_action(request.user, "CLASS_PROMOTION_REJECTED", req.from_classroom, department=req.department, extra={"remarks": remarks, "by": step})
        notify(req.requested_by, "Promotion rejected", f"Your request to move {req.from_classroom.label} was rejected by the {step.lower()}: {remarks}")
        return Response(self.get_serializer(req).data)

    @transaction.atomic
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """The person who asked can withdraw the request while it is still open."""
        req = ClassPromotionRequest.objects.select_for_update().get(pk=self.get_object().pk)
        if req.requested_by_id != request.user.id:
            raise PermissionDenied("Only the person who asked can cancel.")
        if req.status not in ClassPromotionRequest.OPEN:
            raise ValidationError("This request is already decided.")
        req.status = ClassPromotionRequest.Status.CANCELLED
        req.save(update_fields=["status"])
        log_action(request.user, "CLASS_PROMOTION_CANCELLED", req.from_classroom, department=req.department)
        return Response(self.get_serializer(req).data)
