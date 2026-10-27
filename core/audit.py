from .models import AuditLog


def log_action(actor, action, entity, *, department=None, old=None, new=None, reason="", extra=None):
    """Write one audit entry. `entity` is any saved model instance."""
    return AuditLog.objects.create(
        actor=actor if getattr(actor, "pk", None) else None,
        department=department,
        action=action,
        entity_type=entity.__class__.__name__,
        entity_id=str(entity.pk),
        old_value=old,
        new_value=new,
        reason=reason or "",
        extra=extra or {},
    )
