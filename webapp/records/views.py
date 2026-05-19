"""Records app views — collection (records list) and tracks list.

Both are placeholder stubs during fase 5: the URL resolves, the page
renders within the sidebar layout, and a "coming soon" body explains
that the listing UI lands in a follow-up commit.
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from records.models import Release, Track


@login_required
def collection(request):
    count = Release.objects.filter(user=request.user).count()
    return render(request, "records/collection.html", {
        "active": "collection",
        "release_count": count,
    })


@login_required
def tracks(request):
    count = Track.objects.filter(release__user=request.user).count()
    return render(request, "records/tracks.html", {
        "active": "tracks",
        "track_count": count,
    })
