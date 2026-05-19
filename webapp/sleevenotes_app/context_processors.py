from django.conf import settings


def app_branding(request):
    return {
        "APP_NAME": settings.APP_NAME,
        "ENVIRONMENT": settings.ENVIRONMENT,
    }
