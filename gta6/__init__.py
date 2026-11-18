"""GTA 6 Clip Agent by Damon Dynamics: finds, cuts, captions and posts GTA 6 clips, with your approval.

  start.bat / start.sh   (or: python -m gta6)    opens its window and starts working
  python -m gta6 --selftest                       checks its logic with fake data (no network, nothing posted)

Every 20 minutes (08:00-22:00) it:
  1. finds footage: new videos in your capture folders first, then the channels and campaigns you have listed,
  2. cuts vertical, captioned clips (transcribe, pick the moments, render),
  3. writes captions with hashtags from its own research (YouTube search suggestions + what the top GTA 6 Shorts use),
  4. asks you to approve each clip, in its window or on Telegram,
  5. posts what you approve on Instagram, TikTok and YouTube, spaced out through the day.

It only takes footage from places you list. A channel is used only when you tick "this creator allows clipping".
"""
