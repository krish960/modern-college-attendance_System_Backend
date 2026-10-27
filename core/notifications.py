from .models import Notification


def notify(user, title, message):
    if user is not None:
        Notification.objects.create(user=user, title=title, message=message)


def notify_many(users, title, message):
    Notification.objects.bulk_create([Notification(user=u, title=title, message=message) for u in users])


def notify_admins(title, message):
    """The college admin(s) hear about the changes that touch recorded results or the student list."""
    from accounts.models import User
    notify_many(User.objects.filter(role=User.Role.ADMIN, is_active=True), title, message)
