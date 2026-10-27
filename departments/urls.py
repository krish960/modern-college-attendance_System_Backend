from rest_framework.routers import SimpleRouter

from .views import DepartmentViewSet

router = SimpleRouter()
router.register("departments", DepartmentViewSet, basename="department")

urlpatterns = router.urls
