"""URL routes for students, class promotions and student removals."""
from rest_framework.routers import SimpleRouter

from .views import ClassPromotionViewSet, StudentRemovalViewSet, StudentViewSet

# The router builds the list/detail URLs for each viewset automatically.
router = SimpleRouter()
router.register("students", StudentViewSet, basename="student")
# Class promotion: a request that HOD and admin must approve before students move.
router.register("class-promotions", ClassPromotionViewSet, basename="class-promotion")
# Student removal: also an approval flow, nothing is hard deleted.
router.register("student-removals", StudentRemovalViewSet, basename="student-removal")

urlpatterns = router.urls
