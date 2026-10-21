from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from feedback.models import Feedback
from feedback.services import prune_old_feedback
from print_runs.models import PrintRun


User = get_user_model()


class FeedbackViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="tester")
        self.client.force_login(self.user)
        self.url = reverse("feedback:feedback")

    def test_records_message_with_flow_context_and_no_query_string(self):
        run = PrintRun.objects.create(user=self.user, settings={}, release_ids=[])
        page = "http://testserver" + reverse(
            "print_runs:step", kwargs={"run_id": run.id, "step": 2}
        ) + "?q=secret+search"

        response = self.client.post(
            self.url, {"message": "  BPM field loses focus  "}, HTTP_HX_CURRENT_URL=page,
        )

        self.assertContains(response, "your feedback is in")
        fb = Feedback.objects.get()
        self.assertEqual(fb.message, "BPM field loses focus")
        self.assertEqual(fb.view_name, "print_runs:step")
        self.assertEqual(fb.print_run_id, run.id)
        self.assertEqual(fb.step, 2)
        self.assertNotIn("secret", fb.path)
        self.assertEqual(fb.user, self.user)

    def test_empty_message_is_rejected_inside_the_modal(self):
        response = self.client.post(self.url, {"message": "   "})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Write something first.")
        self.assertFalse(Feedback.objects.exists())

    def test_unknown_page_still_saves(self):
        self.client.post(self.url, {"message": "hi"}, HTTP_HX_CURRENT_URL="http://x/nope")
        fb = Feedback.objects.get()
        self.assertEqual((fb.path, fb.view_name, fb.step), ("/nope", "", None))

    @override_settings(FEEDBACK_ENABLED=False)
    def test_disabled_flag_hides_button_and_endpoint(self):
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertNotContains(self.client.get(reverse("home")), reverse("feedback:feedback"))

    def test_button_is_in_top_bar(self):
        self.assertContains(self.client.get(reverse("home")), reverse("feedback:feedback"))

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.post(self.url, {"message": "x"}).status_code, 302)
        self.assertFalse(Feedback.objects.exists())


class RetentionTests(TestCase):
    @override_settings(FEEDBACK_RETENTION_DAYS=90)
    def test_prune_deletes_only_expired_rows(self):
        user = User.objects.create(username="tester")
        old = Feedback.objects.create(user=user, message="old", environment="local")
        Feedback.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=91))
        Feedback.objects.create(user=user, message="new", environment="local")

        self.assertEqual(prune_old_feedback(), 1)
        self.assertEqual(list(Feedback.objects.values_list("message", flat=True)), ["new"])
