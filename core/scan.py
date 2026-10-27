"""Card scanning helper: tells the scanner screen whether an unknown scanned code is a staff card or a student card."""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import StaffProfile, User
from students.models import Student, normalize_card_code


class IdentifyCardView(APIView):
    """
    POST /api/scan/identify/ {"code": "..."}: does a scanned code belong to a STAFF card, a STUDENT card, both or neither?
    A staff member and a student may legitimately carry the same member number, so both can be true. The scanners ask
    this only when the code is not one of the people on the screen, to say "that is a staff card" instead of guessing.
    Only yes/no is returned, never a name, so it shows nothing about anyone.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        """Return {"staff": true/false, "student": true/false} for the scanned code."""
        if request.user.role == User.Role.ADMIN:  # the Admin always gets "no" for both, and nothing is looked up
            return Response({"staff": False, "student": False})
        code = normalize_card_code(request.data.get("code"))  # same clean-up that is used when cards are saved
        return Response({
            "staff": bool(code) and StaffProfile.objects.filter(card_code=code).exists(),
            "student": bool(code) and Student.objects.filter(card_code=code).exists(),
        })
