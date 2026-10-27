from rest_framework.routers import SimpleRouter

from .views import AssignmentViewSet, ClassRoomViewSet, SubjectViewSet

router = SimpleRouter()
router.register("classes", ClassRoomViewSet, basename="classroom")
router.register("subjects", SubjectViewSet, basename="subject")
router.register("assignments", AssignmentViewSet, basename="assignment")

urlpatterns = router.urls
