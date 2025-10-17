from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from flask import current_app, url_for


@dataclass(frozen=True)
class Chapter:
    time: float
    title: str


@dataclass(frozen=True)
class VideoAsset:
    id: int
    slug: str
    title: str
    subtitle: str
    video_filename: str
    poster_filename: str
    thumbnail_filename: str
    caption_basename: str
    duration_seconds: Optional[float] = None
    views: int = 0
    languages: Iterable[str] = field(default_factory=lambda: ("en",))
    chapters: Iterable[Chapter] = field(default_factory=list)

    def to_manifest(self) -> Dict[str, Any]:
        """Build the client manifest representation for this asset."""
        poster_url = url_for("static", filename=f"images/{self.poster_filename}")
        thumb_url = url_for("static", filename=f"images/{self.thumbnail_filename}")
        manifest: Dict[str, Any] = {
            "id": self.id,
            "slug": self.slug,
            "title": self.title,
            "subtitle": self.subtitle,
            "src": url_for("static", filename=f"videos/{self.video_filename}"),
            "poster": poster_url,
            "thumbnail": thumb_url,
            "duration": self.duration_seconds,
            "views": self.views,
            "transcriptEndpoint": url_for("video.get_transcript", video_id=self.id),
            "captions": url_for("static", filename=f"captions/{self.caption_basename}"),
            "chapters": [asdict(c) for c in self.chapters],
            "languages": list(self.languages),
        }
        return manifest

    def transcript_filename(self, language: str = "en") -> str:
        safe_lang = language.lower()
        return f"video-{self.id}_{safe_lang}.json"

    def transcript_path(self, language: str = "en", base_dir: Path | None = None) -> Path:
        base = Path(base_dir) if base_dir is not None else Path(current_app.instance_path) / "transcripts"
        return base / self.transcript_filename(language)


VIDEO_LIBRARY: List[VideoAsset] = [
    VideoAsset(
        id=1,
        slug="brainstormx-intro",
        title="Introducing BrainStormX",
        subtitle="Innovative brainstorming and collaborative workspace",
        video_filename="brainstormx-overview.mp4",
        poster_filename="video-poster.jpg",
        thumbnail_filename="video-poster.jpg",
        caption_basename="video-1_en.vtt",
        duration_seconds=None,
        views=1245,
        chapters=(
            Chapter(time=0, title="Problem Identification"),
            Chapter(time=12, title="Cost of Inefficiency"),
            Chapter(time=22, title="Specific Challenges"),
            Chapter(time=37, title="AI Transformation"),
            Chapter(time=59, title="End"),
        ),
    ),
    VideoAsset(
        id=2,
        slug="brainstormx-workshop-flow",
        title="BrainStormX Guided Workshop",
        subtitle="From framing to feasibility scoring in under two minutes",
        video_filename="brainstormx-features.mp4",
        poster_filename="video-poster.jpg",
        thumbnail_filename="video-poster.jpg",
        caption_basename="video-2_en.vtt",
        duration_seconds=119.0,
        views=892,
        chapters=(
            Chapter(time=0, title="Welcome & Value"),
            Chapter(time=20, title="Framing Agent"),
            Chapter(time=45, title="Equitable Participation"),
            Chapter(time=70, title="Clustering & Voting"),
            Chapter(time=85, title="Feasibility Signals"),
            Chapter(time=110, title="Guardian Deliverables"),
        ),
    ),
    VideoAsset(
        id=3,
        slug="brainstormx-intelligence-stack",
        title="Inside the BrainStormX Intelligence Stack",
        subtitle="Automations, analytics, and orchestration that scale every workshop",
        video_filename="brainstormx-experience.mp4",
        poster_filename="video-poster.jpg",
        thumbnail_filename="video-poster.jpg",
        caption_basename="video-3_en.vtt",
        duration_seconds=225.0,
        views=764,
        chapters=(
            Chapter(time=0, title="Intelligence Overview"),
            Chapter(time=10, title="Agenda Automation"),
            Chapter(time=32, title="Equitable Capture"),
            Chapter(time=56, title="Real-time Clustering"),
            Chapter(time=80, title="Adaptive Voting"),
            Chapter(time=104, title="Feasibility Analytics"),
            Chapter(time=128, title="Action Orchestration"),
            Chapter(time=148, title="Insights Hub"),
            Chapter(time=172, title="Performance Dashboards"),
            Chapter(time=190, title="Extensible Agents"),
            Chapter(time=210, title="Call to Action"),
        ),
    ),
]


def get_video_manifest() -> Dict[str, Any]:
    """Return a JSON-serialisable manifest of published videos."""
    videos = [asset.to_manifest() for asset in VIDEO_LIBRARY]
    return {
        "videos": videos,
        "count": len(videos),
    }


def get_video_asset(video_id: int) -> Optional[VideoAsset]:
    for asset in VIDEO_LIBRARY:
        if asset.id == video_id:
            return asset
    return None