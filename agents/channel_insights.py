"""
Live, evidence-based summary of what has worked on the channel, built from
video_stats (views) and video_analytics (retention). Injected into every
creative prompt so the writers learn from real numbers, not folklore.
"""
from __future__ import annotations

import statistics as st

import state


def summary(limit: int = 8) -> str:
    with state.connect() as con:
        rows = list(con.execute(
            """SELECT v.id, v.title, v.source_orchestrator AS src, s.kind, s.views
               FROM video_stats s JOIN videos v ON v.id = s.video_id
               WHERE s.id IN (SELECT MAX(id) FROM video_stats GROUP BY yt_video_id)"""))
        ret = {r["yt"]: r["p"] for r in con.execute(
            """SELECT a.yt_video_id AS yt, a.average_view_percentage AS p FROM video_analytics a
               WHERE a.id IN (SELECT MAX(id) FROM video_analytics GROUP BY yt_video_id)""")}
        ids = {(r["id"], "long"): r["youtube_video_id"] for r in con.execute("SELECT id, youtube_video_id FROM videos")}
        ids.update({(r["id"], "short"): r["short_video_id"] for r in con.execute("SELECT id, short_video_id FROM videos")})
    if not rows:
        return "(no channel stats yet)"
    lines = []
    for kind in ("short", "long"):
        rs = [r for r in rows if r["kind"] == kind and r["views"] is not None]
        if not rs:
            continue
        views = [r["views"] for r in rs]
        lines.append(f"{kind.upper()}: {len(rs)} videos, {sum(views)} total views, median {st.median(views):.0f}")
        def fmt(r):
            p = ret.get(ids.get((r["id"], kind)))
            return f'  {r["views"]:>4} views{f" | {p:.0f}% avg viewed" if p else ""} | "{r["title"]}"'
        top = sorted(rs, key=lambda r: -r["views"])[:limit]
        bottom = sorted(rs, key=lambda r: r["views"])[:max(3, limit // 2)]
        lines.append(" best:")
        lines += [fmt(r) for r in top]
        lines.append(" worst:")
        lines += [fmt(r) for r in bottom]
    return "\n".join(lines)
