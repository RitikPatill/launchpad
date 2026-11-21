"""
launchpad motion engine: storyboard -> per-scene voice -> timeline ->
frame-perfect HTML render (headless Chromium) -> mp4.

  timeline.build(...)   voice per scene, padded audio track, global word timings
  render.render_video() deterministic frame capture piped into ffmpeg
  capture.repo_screens() real screenshots of the project's pages (headless)
  thumb.render_thumbnail() 1280x720 custom thumbnail from an HTML template
"""
