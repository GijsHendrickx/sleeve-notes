from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from print_runs.models import PrintRun


User = get_user_model()


class SavedRunIsReadOnlyInFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="alice")
        self.client.force_login(self.user)

    def test_flow_redirects_saved_set_to_detail_and_leaves_it_untouched(self):
        saved = PrintRun.objects.create(
            user=self.user, name="Printed", settings={}, release_ids=[1],
            status=PrintRun.STATUS_SAVED,
        )
        url = reverse("print_runs:step", kwargs={"run_id": saved.id, "step": 1})

        for response in (self.client.get(url), self.client.post(url, {"release_id": [2], "name": "x"})):
            self.assertRedirects(
                response, reverse("print_runs:detail", kwargs={"run_id": saved.id}),
                fetch_redirect_response=False,
            )
        saved.refresh_from_db()
        self.assertEqual((saved.release_ids, saved.name), ([1], "Printed"))

    def test_finishing_an_empty_draft_sends_back_to_step_one(self):
        draft = PrintRun.objects.create(
            user=self.user, settings={}, release_ids=[], status=PrintRun.STATUS_DRAFT, step=4,
        )
        response = self.client.post(reverse("print_runs:step", kwargs={"run_id": draft.id, "step": 4}))

        self.assertRedirects(
            response, reverse("print_runs:step", kwargs={"run_id": draft.id, "step": 1}),
            fetch_redirect_response=False,
        )
        draft.refresh_from_db()
        self.assertEqual(draft.status, PrintRun.STATUS_DRAFT)
