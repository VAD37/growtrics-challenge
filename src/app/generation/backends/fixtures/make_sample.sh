#!/bin/sh
# Regenerate the committed sample lesson video.
#
# The fixture is a real, playable MP4: h264 baseline in an isomp4 container with an AAC track,
# 320x240, 4 seconds, about 16 KB. Small enough to commit, well formed enough that a browser
# plays it and that `container_is_mp4` passes over its actual ftyp box rather than over a
# renamed text file.
#
# It is deliberately NOT 720p and deliberately not a chemistry lesson. The mock backend proves
# the pipeline, not the product: `resolution_at_least_720p` is a deferred check
# (`app/custody/verifier.py`), and this file would fail it if it ran.
set -eu
cd "$(dirname "$0")"
ffmpeg -y -v error \
    -f lavfi -i "color=c=0x101820:s=320x240:d=4:r=12" \
    -f lavfi -i "sine=frequency=330:duration=4" \
    -c:v libx264 -profile:v baseline -level 3.0 -pix_fmt yuv420p -preset veryslow -crf 30 \
    -c:a aac -b:a 24k -ac 1 -ar 22050 \
    -movflags +faststart -shortest \
    sample_lesson.mp4
