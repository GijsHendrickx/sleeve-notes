"""Cross-tenant safety net: user B must never reach user A's data.

Multi-tenancy here is a convention (every query filters on the user), so
one forgotten filter is a leak. This test is what turns that convention
into something CI enforces:

- Every route with a `<uuid:...>` in its path must be listed in
  `UUID_ROUTES`. A new route without an entry fails the coverage test, so
  it can't ship untested.
- Logged in as B, every listed route is hit with A's ids, by GET and by
  POST. Anything other than 403/404/405 fails, and A's rows must be
  untouched afterwards.
- Routes that take ids in the body or query string can't be found by
  walking the URLconf; they're covered one by one in `BodyIdTests`. Add
  new ones there.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from print_runs.models import PrintRun
from records.models import BpmCache, Override, Release, Track


User = get_user_model()

# A string that only ever appears in A's data, so any response containing
# it is a leak no matter which view produced it.
A_MARKER = "Zzyzx Tenant Alpha"
A_DISCOGS_ID = 9_000_001
B_DISCOGS_ID = 9_000_002

ALLOWED = {403, 404, 405}

# route name → kwargs builder, given the fixture
UUID_ROUTES = {
    "records:release_detail": lambda f: {"release_id": f.a_release.id},
    "print_runs:detail": lambda f: {"run_id": f.a_saved.id},
    "print_runs:delete": lambda f: {"run_id": f.a_saved.id},
    "print_runs:duplicate": lambda f: {"run_id": f.a_saved.id},
    "print_runs:pdf_download": lambda f: {"run_id": f.a_saved.id},
    "print_runs:step": lambda f: {"run_id": f.a_draft.id, "step": 1},
    "print_runs:step2_status": lambda f: {"run_id": f.a_draft.id},
    "print_runs:step2_lookup": lambda f: {"run_id": f.a_draft.id},
    "print_runs:step3_preview": lambda f: {"run_id": f.a_draft.id},
}


def _walk(patterns, prefix="", namespace=""):
    for p in patterns:
        route = prefix + str(p.pattern)
        if isinstance(p, URLResolver):
            ns = namespace
            if p.namespace:
                ns = f"{namespace}{p.namespace}:"
            yield from _walk(p.url_patterns, route, ns)
        elif isinstance(p, URLPattern):
            yield route, f"{namespace}{p.name}" if p.name else None


class TenantFixture(TestCase):
    def setUp(self):
        self.alice = User.objects.create(username="alice")
        self.bob = User.objects.create(username="bob")

        self.a_release = Release.objects.create(
            user=self.alice, discogs_release_id=A_DISCOGS_ID,
            artist=A_MARKER, title=f"{A_MARKER} LP",
        )
        Track.objects.create(
            release=self.a_release, position="A1", title=f"{A_MARKER} Track",
            spotify_track_id="alicespotifyid",
        )
        Override.objects.create(
            user=self.alice, release=self.a_release, position="A1", bpm=124,
        )
        BpmCache.objects.create(
            user=self.alice, cache_key="a" * 64, artist=A_MARKER, title="x",
        )
        self.a_saved = PrintRun.objects.create(
            user=self.alice, name=A_MARKER, settings={},
            release_ids=[A_DISCOGS_ID], status=PrintRun.STATUS_SAVED,
        )
        self.a_draft = PrintRun.objects.create(
            user=self.alice, name=A_MARKER, settings={},
            release_ids=[A_DISCOGS_ID], status=PrintRun.STATUS_DRAFT,
        )

        b_release = Release.objects.create(
            user=self.bob, discogs_release_id=B_DISCOGS_ID, artist="Bob", title="Bob LP",
        )
        Track.objects.create(release=b_release, position="A1", title="Bob Track")

        self.client.force_login(self.bob)

    def snapshot_alice(self):
        return {
            "releases": list(Release.objects.filter(user=self.alice).values_list("id", "title")),
            "tracks": list(Track.objects.filter(release__user=self.alice).values_list("id", "title")),
            "overrides": list(Override.objects.filter(user=self.alice).values_list("id", "bpm")),
            "cache": list(BpmCache.objects.filter(user=self.alice).values_list("cache_key", flat=True)),
            "runs": list(
                PrintRun.objects.filter(user=self.alice)
                .values_list("id", "name", "status", "release_ids", "settings")
                .order_by("id")
            ),
        }

    def assert_no_leak(self, response, label):
        self.assertNotIn(A_MARKER.encode(), response.content, f"{label} leaked A's data")


class UuidRouteTests(TenantFixture):
    def test_every_uuid_route_is_covered(self):
        uncovered = sorted(
            name or route
            for route, name in _walk(get_resolver().url_patterns)
            if "<uuid:" in route and name not in UUID_ROUTES
        )
        self.assertEqual(
            uncovered, [],
            "Routes with a <uuid:...> param missing from UUID_ROUTES in "
            "core/tests_tenant_isolation.py — add them so the isolation test covers them.",
        )

    def test_other_user_gets_404_on_every_uuid_route(self):
        before = self.snapshot_alice()
        bob_runs_before = PrintRun.objects.filter(user=self.bob).count()

        for name, kwargs in UUID_ROUTES.items():
            url = reverse(name, kwargs=kwargs(self))
            for method in ("get", "post"):
                with self.subTest(route=name, method=method):
                    response = getattr(self.client, method)(url)
                    self.assertIn(
                        response.status_code, ALLOWED,
                        f"{method.upper()} {url} returned {response.status_code} for another user",
                    )
                    self.assert_no_leak(response, f"{method.upper()} {name}")

        self.assertEqual(self.snapshot_alice(), before)
        self.assertEqual(PrintRun.objects.filter(user=self.bob).count(), bob_runs_before)


class BodyIdTests(TenantFixture):
    def test_tracks_sync_rejects_other_users_release(self):
        before = self.snapshot_alice()
        response = self.client.post(
            reverse("records:tracks_sync"),
            {"release_id": A_DISCOGS_ID, "position": "A1"},
        )
        self.assertEqual(response.status_code, 404)
        self.assert_no_leak(response, "tracks_sync")
        self.assertEqual(self.snapshot_alice(), before)

    def test_track_listen_does_not_resolve_other_users_track(self):
        # An unknown track deliberately falls back to a YouTube search on the
        # position alone, so a redirect is fine — as long as it isn't A's.
        response = self.client.get(
            reverse("records:track_listen"),
            {"release_id": A_DISCOGS_ID, "position": "A1"},
        )
        location = response.get("Location", "")
        self.assertNotIn("alicespotifyid", location)
        self.assertNotIn("Zzyzx", location)

    def test_tracks_save_cannot_write_overrides_on_other_users_release(self):
        before = self.snapshot_alice()
        self.client.post(reverse("records:tracks_save"), {
            f"bpm_{A_DISCOGS_ID}_A1": "99",
            f"key_{A_DISCOGS_ID}_A1": "1A",
            f"note_{A_DISCOGS_ID}_A1": "",
        })
        self.assertEqual(self.snapshot_alice(), before)
        self.assertFalse(Override.objects.filter(user=self.bob, release=self.a_release).exists())

    def test_flow_selection_with_other_users_ids_stays_inert(self):
        draft = PrintRun.objects.create(
            user=self.bob, settings={}, release_ids=[], status=PrintRun.STATUS_DRAFT,
        )
        self.client.post(
            reverse("print_runs:step", kwargs={"run_id": draft.id, "step": 1}),
            {"release_id": [A_DISCOGS_ID, B_DISCOGS_ID]},
        )
        for step in (2, 3, 4):
            response = self.client.get(
                reverse("print_runs:step", kwargs={"run_id": draft.id, "step": step})
            )
            with self.subTest(step=step):
                self.assert_no_leak(response, f"flow step {step}")
        response = self.client.get(reverse("print_runs:pdf_download", kwargs={"run_id": draft.id}))
        self.assert_no_leak(response, "pdf_download")
