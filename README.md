# yt-dlp-threads

A [yt-dlp](https://github.com/yt-dlp/yt-dlp) extractor plugin for **Threads**
(`threads.com` / `threads.net`).

yt-dlp has no built-in Threads extractor — support has been requested for years
without a merged implementation ([#7523](https://github.com/yt-dlp/yt-dlp/issues/7523),
[#10133](https://github.com/yt-dlp/yt-dlp/issues/10133),
[#12021](https://github.com/yt-dlp/yt-dlp/issues/12021)) — and Threads serves
anonymous clients a JavaScript login wall with no media in the HTML.

## How it works

Meta server-renders the full post — including the CDN media URLs — for
link-preview crawlers. This plugin fetches the post page with a **Googlebot
User-Agent** to bypass the login wall, then extracts the progressive (muxed)
mp4 URL(s) from the embedded JSON. Handles canonical `@user/post/<id>` links,
`/share/<code>/` short links, multi-video (carousel) posts, and posts whose
video is embedded rather than uploaded: a text post with a linked Instagram
reel, a repost, or a quote of a video post.

> **Caveat:** this bypass is inherently fragile. If Meta stops serving crawlers
> the full payload or changes the embedded JSON keys, extraction will break.
> There is no cookie/auth support — only publicly viewable posts work.

## Install

Requires yt-dlp with plugin support (2023.01.02+) and Python 3.10+.

**As a pip package** (installs into the same environment as yt-dlp):

```bash
pip install -U yt-dlp-threads
# or, if yt-dlp is a uv/pipx tool:
uv tool install yt-dlp --with yt-dlp-threads
pipx inject yt-dlp yt-dlp-threads
```

**As a manual plugin** (no install): drop the `yt_dlp_plugins` folder into a
yt-dlp plugin directory, e.g.

```bash
mkdir -p ~/.config/yt-dlp/plugins/yt-dlp-threads
cp -r yt_dlp_plugins ~/.config/yt-dlp/plugins/yt-dlp-threads/
```

## Usage

```bash
yt-dlp "https://www.threads.com/@someone/post/ABC123"
yt-dlp -F "https://www.threads.com/share/xxxxxxxx/"   # list formats (hd/sd)
```

Verify it loaded: `yt-dlp --verbose <url>` prints `[debug] Extractor Plugins: ThreadsIE`.

## Notes

- A Threads page embeds **many** posts (thread items, replies, recommendations).
  The extractor selects the one whose shortcode matches your URL; if that post
  isn't in the page data (deleted, private, login-gated) it raises an error
  rather than returning some other post's video.
- `duration` is decoded from the CDN URL's `efg` parameter — Threads posts have
  no `video_duration` field.
- Embedded media: when a post has no video of its own, the extractor tries, in
  order, a linked Instagram reel (`text_post_app_info.linked_inline_media`), a
  reposted post, then a quoted post. For a linked reel or a quote, the
  metadata (id, author, caption) is the Threads post's; for a repost it is the
  original post's. A line in the output names which one was downloaded.
- Image-only and text-only posts are not supported (video only).

## Development

```bash
python -m unittest discover -s tests -v   # offline tests, no network
ruff check .   # rule set is pinned in pyproject.toml; CI pins the ruff version
```

## License

Unlicense (public domain).
