"""Department data isolation helper used by every viewset."""


def department_scope(queryset, user, path="department_id"):
    """
    Admin sees everything. HOD and staff only see rows whose department matches
    their own. `path` is the ORM lookup that leads to the department id, e.g.
    "classroom__department_id".
    """
    if user.role == "ADMIN":
        return queryset
    if user.department_id is None:
        return queryset.none()
    return queryset.filter(**{path: user.department_id})
