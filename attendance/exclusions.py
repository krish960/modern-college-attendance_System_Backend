"""Electives: mark which students do NOT take a subject (so he is not on that subject's attendance list)."""
from django.db import transaction
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from academics.models import ClassRoom, Subject, TeacherSubjectAssignment
from accounts.permissions import IsStaffTeacher
from core.audit import log_action
from core.ordering import roll_order
from students.models import Student

from .models import SubjectExclusion


def _assignment_for(user, classroom, subject):
    """The teacher's own active assignment of this subject to this class (only he may change the list)."""
    # Match on teacher=user so a teacher cannot edit another teacher's subject list.
    qs = TeacherSubjectAssignment.objects.filter(classroom=classroom, subject=subject, is_active=True, teacher=user)
    return qs.select_related("classroom__department", "subject").first()


def _ids(request, source):
    """Read and check classroom + subject ids from the query string or body.

    Returns (classroom, subject). Raises 400 for bad input and 403 if the logged-in
    teacher does not teach this subject in this class.
    """
    c, s = source.get("classroom"), source.get("subject")
    if not (str(c).isdigit() and str(s).isdigit()):
        raise ValidationError("Choose a class and a subject.")
    classroom = ClassRoom.objects.filter(pk=c).select_related("department").first()
    subject = Subject.objects.filter(pk=s).first()
    if not classroom or not subject:
        raise ValidationError("Class or subject not found.")
    if _assignment_for(request.user, classroom, subject) is None:
        raise PermissionDenied("This subject is not assigned to you in this class.")
    return classroom, subject


# Elective support: SubjectExclusion rows are the students who do NOT take a subject.
# Only the assigned teacher can use this screen (IsStaffTeacher + _assignment_for).
class SubjectStudentsView(APIView):
    """
    GET  ?classroom=&subject=  -> every active student of the class with `takes_subject` true/false
    POST {classroom, subject, student, takes_subject}  -> change one student (idempotent)
    """

    permission_classes = [IsStaffTeacher]

    def get(self, request):
        """List the class students and whether each one takes this subject."""
        classroom, subject = _ids(request, request.query_params)
        out = set(SubjectExclusion.objects.filter(subject=subject, student__classroom=classroom).values_list("student_id", flat=True))
        students = Student.objects.defer("photo").filter(classroom=classroom, is_active=True).order_by(*roll_order())
        rows = [{"id": s.id, "roll_number": s.roll_number, "name": s.name, "takes_subject": s.id not in out} for s in students]
        return Response({"classroom": classroom.label, "subject": subject.name, "students": rows,
                         "taking": sum(r["takes_subject"] for r in rows), "not_taking": sum(not r["takes_subject"] for r in rows)})

    @transaction.atomic
    def post(self, request):
        """Add or remove one SubjectExclusion. Safe to call twice (idempotent).

        Every real change is written to the audit log.
        """
        classroom, subject = _ids(request, request.data)
        takes = request.data.get("takes_subject")
        if not isinstance(takes, bool):
            raise ValidationError({"takes_subject": "Send true or false."})
        student = Student.objects.defer("photo").filter(pk=request.data.get("student") if str(request.data.get("student", "")).isdigit() else 0,
                                                       classroom=classroom, is_active=True).first()
        if student is None:
            raise ValidationError({"student": "This student is not an active student of the class."})
        # takes=True removes the exclusion row; takes=False creates it. Anything else is a no-op.
        existing = SubjectExclusion.objects.filter(student=student, subject=subject).first()
        if takes and existing:
            log_action(request.user, "STUDENT_ADDED_TO_SUBJECT", existing, department=classroom.department,
                       new={"student": student.name, "roll": student.roll_number, "subject": subject.name})
            existing.delete()
        elif not takes and not existing:
            ex = SubjectExclusion.objects.create(student=student, subject=subject, created_by=request.user)
            log_action(request.user, "STUDENT_LEFT_OUT_OF_SUBJECT", ex, department=classroom.department,
                       new={"student": student.name, "roll": student.roll_number, "subject": subject.name})
        return Response({"student": student.id, "takes_subject": takes}, status=status.HTTP_200_OK)
