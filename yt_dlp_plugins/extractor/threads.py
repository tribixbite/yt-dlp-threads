"""yt-dlp extractor plugin for Threads (threads.com / threads.net).

Threads has no upstream yt-dlp extractor (long-open requests: yt-dlp #7523,
#10133, #12021). Anonymous browser/API clients get a JS login wall with no
media. The trick this plugin uses: Meta server-renders the full post — CDN
media URLs and all — into the page's embedded JSON for link-preview crawlers,
so we fetch with a Googlebot User-Agent to bypass the wall, then parse the
post out of the `data-sjs` JSON blobs.

A post page embeds *many* posts (thread items, replies, recommendations), so
the target is selected by its shortcode (`code`). Guessing "the first video on
the page" is not safe: it silently yields an unrelated recommended clip.

A Threads post can also carry its video indirectly: a text post that embeds
an Instagram reel (`text_post_app_info.linked_inline_media`), a repost, or a
quote of another post (`text_post_app_info.share_info`). When the post itself
has no video, those embedded media are tried in that order.

Caveat: this bypass is inherently fragile. If Meta stops serving crawlers the
full payload, or reshuffles the embedded JSON, extraction will break. There is
no cookie/auth support — only publicly viewable posts work.
"""

import base64
import json
import re
from typing import ClassVar

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import (
    ExtractorError,
    float_or_none,
    int_or_none,
    parse_qs,
    str_or_none,
    url_or_none,
)
from yt_dlp.utils.traversal import traverse_obj

_GOOGLEBOT_UA = 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)'
_MEDIA_KEYS = {'video_versions', 'video_dash_manifest', 'image_versions2', 'carousel_media'}
# media_type values seen in the post JSON.
_MEDIA_TYPE_IMAGE = 1
_MEDIA_TYPE_CAROUSEL = 8
# Where a post that has no media of its own keeps the media it points at,
# as (traversal path under the post, human-readable label, take_metadata).
# Checked in order. take_metadata: the embedded post is the real content, so
# its author/caption/timestamp replace the wrapping post's.
#  - linked_inline_media: an Instagram reel/post embedded in a text post (the
#    "attached reel" card). Seen live: media_type 19 post -> media_type 2 reel.
#  - reposted_post: a plain repost; the whole post is someone else's.
#  - quoted_post / quoted_attachment_post: a quote post; the video belongs to
#    the quoted post, the text to the quoting one.
_EMBEDDED_MEDIA_PATHS = (
    (('text_post_app_info', 'linked_inline_media'), 'linked Instagram media', False),
    (('text_post_app_info', 'share_info', 'reposted_post'), 'reposted post', True),
    (('text_post_app_info', 'share_info', 'quoted_post'), 'quoted post', False),
    (('text_post_app_info', 'share_info', 'quoted_attachment_post'), 'quoted attachment', False),
)


class ThreadsIE(InfoExtractor):
    IE_NAME = 'threads'
    _VALID_URL = r'''(?x)
        https?://(?:www\.)?threads\.(?:net|com)/
        (?:
            (?:@(?P<user>[^/?#]+)/post|t)/(?P<id>[\w-]+)
            |share/(?P<share>[\w-]+)
        )'''
    _TESTS: ClassVar[list[dict]] = [{
        'url': 'https://www.threads.com/@kfury/post/DaGcWDwj8tW',
        'info_dict': {
            'id': 'DaGcWDwj8tW',
            'ext': 'mp4',
            'title': str,
            'uploader_id': 'kfury',
            'uploader_url': 'https://www.threads.com/@kfury',
            'timestamp': int,
            'upload_date': str,
        },
        'params': {'skip_download': True},
    }, {
        # Text post whose video is an embedded Instagram reel
        # (text_post_app_info.linked_inline_media)
        'url': 'https://www.threads.com/share/BBkykdNbKs',
        'info_dict': {
            'id': 'DgArqLFkbE2',
            'ext': 'mp4',
            'title': str,
            'uploader_id': 'gjarca17',
            'timestamp': int,
            'upload_date': str,
            'duration': int,
        },
        'params': {'skip_download': True},
    }, {
        # /share/<code>/ short links resolve via the canonical og:url
        'url': 'https://www.threads.com/share/_srJIVx6G/',
        'only_matching': True,
    }, {
        'url': 'https://www.threads.net/@zuck/post/C8Xw3vLxabc',
        'only_matching': True,
    }, {
        'url': 'https://www.threads.com/t/C8Xw3vLxabc',
        'only_matching': True,
    }]

    def _collect_posts(self, obj, out):
        """Recursively gather every media-bearing post dict from parsed JSON."""
        if isinstance(obj, dict):
            if obj.get('code') and (obj.keys() & _MEDIA_KEYS):
                out.append(obj)
            for value in obj.values():
                self._collect_posts(value, out)
        elif isinstance(obj, list):
            for value in obj:
                self._collect_posts(value, out)

    def _extract_posts(self, webpage, video_id):
        posts = []
        for block in re.findall(
                r'<script type="application/json"[^>]*>(.*?)</script>', webpage, re.DOTALL):
            # errnote=False: pages carry unrelated JSON blobs; a parse failure
            # in one is normal and must not spam warnings.
            data = self._parse_json(block, video_id, fatal=False, errnote=False)
            if data is not None:
                self._collect_posts(data, posts)
        return posts

    @staticmethod
    def _efg_info(url):
        """Decode the CDN URL's base64 `efg` param.

        It carries {'vencode_tag': 'xpv_progressive...C3.<width>...',
        'duration_s': <int>, ...} — the only place the delivered rendition's
        duration is exposed, since Threads posts (unlike Instagram's) have no
        `video_duration` field.
        """
        efg = traverse_obj(parse_qs(url), ('efg', 0, {str}))
        if not efg:
            return {}
        try:
            return json.loads(base64.urlsafe_b64decode(efg + '=' * (-len(efg) % 4)))
        except (ValueError, TypeError):
            return {}

    def _formats_from_media(self, media, video_id):
        formats = []

        # 1. DASH manifest, when present: precise per-representation
        #    width/height/bitrate/codecs straight from the MPD.
        manifest = traverse_obj(media, ('video_dash_manifest', {str}))
        if manifest:
            mpd_doc = self._parse_xml(manifest, video_id, fatal=False)
            if mpd_doc is not None:
                formats.extend(self._parse_mpd_formats(mpd_doc, mpd_id='dash'))

        # 2. Progressive muxed mp4s. Per-version width/height is usually null,
        #    so fall back to the post's native size. Dedupe by URL path: the
        #    type 101/102/103 entries are often the same asset re-signed.
        fallback_w = int_or_none(media.get('original_width'))
        fallback_h = int_or_none(media.get('original_height'))
        # has_audio is False for silent clips; None/absent means "unknown".
        acodec = 'none' if media.get('has_audio') is False else None
        seen = set()
        for version in media.get('video_versions') or []:
            url = url_or_none(version.get('url'))
            if not url:
                continue
            path = url.split('?')[0]
            if path in seen:
                continue
            seen.add(path)
            fmt = {
                'url': url,
                'format_id': str_or_none(version.get('type')) or f'http-{len(formats)}',
                'ext': 'mp4',
                'width': int_or_none(version.get('width')) or fallback_w,
                'height': int_or_none(version.get('height')) or fallback_h,
                'http_headers': {'Referer': 'https://www.threads.com/'},
            }
            if acodec:
                fmt['acodec'] = acodec
            formats.append(fmt)
        return formats

    def _media_entry(self, media, item_id):
        formats = self._formats_from_media(media, item_id)
        if not formats:
            return None
        # Instagram media (e.g. a linked reel) carries video_duration; native
        # Threads video only exposes it in the CDN URL's efg blob.
        duration = int_or_none(float_or_none(media.get('video_duration')))
        for fmt in formats if not duration else ():
            duration = int_or_none(self._efg_info(fmt['url']).get('duration_s'))
            if duration:
                break
        return {
            'id': item_id,
            'formats': formats,
            'duration': duration,
            'thumbnails': traverse_obj(media, (
                'image_versions2', 'candidates', lambda _, v: url_or_none(v['url']), {
                    'url': 'url',
                    'width': ('width', {int_or_none}),
                    'height': ('height', {int_or_none}),
                })),
        }

    def _real_extract(self, url):
        mobj = self._match_valid_url(url)
        matched_id = mobj.group('id') or mobj.group('share')

        webpage, urlh = self._download_webpage_handle(
            url, matched_id, note='Downloading post webpage (crawler UA)',
            headers={'User-Agent': _GOOGLEBOT_UA})

        # Determine the target shortcode. Canonical URLs carry it; /share/ and
        # /t/ links only reveal it through the canonical og:url on the page,
        # or through the URL the share link redirected to.
        target_code = mobj.group('id')
        if not target_code:
            for candidate in (self._og_search_property('url', webpage, default=None),
                              getattr(urlh, 'url', None)):
                target_code = self._search_regex(
                    r'/post/([\w-]+)', candidate or '', 'post id', default=None)
                if target_code:
                    break

        posts = self._extract_posts(webpage, matched_id)
        if target_code:
            # The same post can be embedded more than once (e.g. a bare copy in
            # a related-posts list); every copy is tried, in page order.
            candidates = [p for p in posts if p.get('code') == target_code]
            if not candidates:
                # Never fall back here: the page is full of unrelated
                # recommended posts and picking one yields the wrong video.
                raise ExtractorError(
                    f'Post "{target_code}" was not found in the page data. It may be '
                    'deleted, private, login-gated, or Threads changed its layout.',
                    expected=True)
        else:
            candidates = [next((p for p in posts if p.get('video_versions')
                                or p.get('video_dash_manifest')), None)]
            if candidates[0] is None:
                raise ExtractorError(
                    'No video post found on this page.', expected=True)
            self.report_warning(
                'Could not determine which post this link points to; using the first '
                'video on the page. Pass the canonical @user/post/<id> URL to be sure.')

        for post in candidates:
            result = self._resolve_post(post, target_code or matched_id, url, mobj.group('user'))
            if result:
                return result

        post = candidates[0]
        video_id = post.get('code') or target_code or matched_id
        kind = ('an image post' if post.get('media_type') == _MEDIA_TYPE_IMAGE
                else 'a carousel with no videos; images are not supported'
                if post.get('media_type') == _MEDIA_TYPE_CAROUSEL
                else 'text-only or unsupported')
        raise ExtractorError(
            f'Post "{video_id}" has no downloadable video ({kind})', expected=True)

    def _resolve_post(self, post, fallback_id, url, url_user):
        """Return the info dict for `post`'s own video, else for embedded media.

        Returns None when neither the post nor anything it embeds has a video.
        """
        video_id = post.get('code') or fallback_id
        result = self._post_result(post, video_id, url, url_user)
        if result:
            return result

        # The post has no media of its own; it may embed someone else's.
        for path, label, take_metadata in _EMBEDDED_MEDIA_PATHS:
            embedded = traverse_obj(post, (*path, {dict}))
            if not embedded:
                continue
            if take_metadata:
                # A repost is the original post wholesale, so resolve it as
                # one: it may itself be a text post with a linked reel.
                result = self._resolve_post(embedded, video_id, url, url_user)
            else:
                result = self._post_result(
                    post, video_id, url, url_user, media_post=embedded)
            if result:
                self.to_screen(
                    f'{video_id}: Post has no video of its own; downloading its {label}'
                    + (f' ({embedded["code"]})' if embedded.get('code') else ''))
                return result
        return None

    def _post_result(self, post, video_id, url, url_user, media_post=None):
        """Build the info dict for `post`, taking the video from `media_post`.

        media_post defaults to the post itself. For embedded media (linked
        reel, quote) the Threads post supplies the metadata and the embedded
        media only the video; caption falls back to the embedded one.
        Returns None when media_post holds no downloadable video (including an
        image-only carousel).
        """
        media_post = media_post or post
        user = post.get('user') or {}
        caption = (traverse_obj(post, ('caption', 'text'))
                   or post.get('accessibility_caption')
                   or traverse_obj(media_post, ('caption', 'text')))
        uploader_id = user.get('username') or url_user
        common = {
            'description': caption,
            'uploader': user.get('full_name') or uploader_id,
            'uploader_id': uploader_id,
            'uploader_url': f'https://www.threads.com/@{uploader_id}' if uploader_id else None,
            'timestamp': int_or_none(post.get('taken_at')),
            'like_count': int_or_none(post.get('like_count')),
            'webpage_url': url_or_none(post.get('canonical_url')) or url,
        }
        title = (caption or '').split('\n')[0][:72] or (
            f'Threads video by {uploader_id}' if uploader_id else f'Threads video {video_id}')

        carousel = media_post.get('carousel_media')
        if isinstance(carousel, list) and carousel:
            entries = []
            for idx, item in enumerate(carousel):
                entry = self._media_entry(item, f'{video_id}_{idx + 1}')
                if entry:
                    entry.update(common)
                    entry['title'] = f'{title} (part {idx + 1})'
                    entries.append(entry)
            if not entries:
                return None  # image-only carousel; the caller reports it
            if len(entries) == 1:
                return entries[0]
            return self.playlist_result(
                entries, playlist_id=video_id, playlist_title=title,
                playlist_description=caption, multi_video=True)

        entry = self._media_entry(media_post, video_id)
        if entry is None:
            return None
        entry.update(common)
        entry['title'] = title
        return entry
