"""Offline regression tests for the Threads extractor.

These use synthetic page HTML — no network — so they run in CI and pin the
behaviours that are easy to regress, above all: never return a different
post's video than the one that was requested.
"""

import base64
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yt_dlp import YoutubeDL
from yt_dlp.utils import ExtractorError

from yt_dlp_plugins.extractor.threads import ThreadsIE


def _efg(duration_s, width=720):
    """Build a CDN-style base64 `efg` param (where duration actually lives)."""
    blob = json.dumps({
        'vencode_tag': f'xpv_progressive.INSTAGRAM.FEED.C3.{width}.dash_baseline_1_v1',
        'duration_s': duration_s,
    }).encode()
    return base64.urlsafe_b64encode(blob).decode().rstrip('=')


def _video_url(name, duration_s):
    return f'https://scontent.cdninstagram.com/o1/v/{name}.mp4?efg={_efg(duration_s)}&oe=1'


def _post(code, username, *, videos=(), carousel=None, has_audio=True,
          media_type=2, caption='hello world'):
    post = {
        'code': code,
        'pk': '123',
        'user': {'username': username, 'full_name': username.title()},
        'caption': {'text': caption} if caption else None,
        'taken_at': 1782587654,
        'like_count': 42,
        'original_width': 1296,
        'original_height': 720,
        'media_type': media_type,
        'has_audio': has_audio,
        'image_versions2': {'candidates': [
            {'url': 'https://scontent.cdninstagram.com/thumb_small.jpg', 'width': 150, 'height': 150},
            {'url': 'https://scontent.cdninstagram.com/thumb_big.jpg', 'width': 640, 'height': 640},
        ]},
    }
    if videos:
        post['video_versions'] = [
            {'type': 101 + i, 'url': u, 'width': None, 'height': None}
            for i, u in enumerate(videos)
        ]
    if carousel is not None:
        post['carousel_media'] = carousel
    return post


def _page(posts, og_url=None):
    """Wrap posts in the nested shape Threads actually ships, plus noise."""
    thread = {'node': {'thread_items': [{'post': p} for p in posts]}}
    result = {'data': {'data': {'edges': [thread]}}}
    payload = {'require': [['ScheduledServerJS', 'handle', None,
                            [{'__bbox': {'result': result}}]]]}
    head = f'<meta property="og:url" content="{og_url}" />' if og_url else ''
    return (
        f'<html><head>{head}</head><body>'
        '<script type="application/json" data-sjs>{"not":"a post"}</script>'
        '<script type="application/json" data-sjs>NOT VALID JSON AT ALL</script>'
        f'<script type="application/json" data-sjs>{json.dumps(payload)}</script>'
        '</body></html>'
    )


class _FakeHandle:
    """Stand-in for the urllib response; only `.url` (post-redirect) is read."""

    def __init__(self, url):
        self.url = url


def _extract(html, url, final_url=None):
    ie = ThreadsIE(YoutubeDL({'quiet': True, 'simulate': True}))
    ie._download_webpage_handle = (
        lambda *args, **kwargs: (html, _FakeHandle(final_url or url)))
    return ie._real_extract(url)


def _text_post(code, username, caption='my take on this', **app_info):
    """A media_type 19 text post, optionally embedding media via app_info."""
    post = _post(code, username, media_type=19, caption=caption)
    post['video_versions'] = None
    post['video_dash_manifest'] = None
    post['carousel_media'] = None
    post['text_post_app_info'] = {
        'linked_inline_media': None,
        'share_info': {'quoted_post': None, 'reposted_post': None,
                       'quoted_attachment_post': None},
        **app_info,
    }
    return post


class TestThreadsExtractor(unittest.TestCase):
    def test_selects_requested_post_not_first_on_page(self):
        """The requested post wins even when another video precedes it.

        This is the regression guard for the worst failure mode: a Threads page
        embeds many posts, and taking the first video returns a stranger's clip.
        """
        html = _page([
            _post('OTHERPOST1', 'someone_else', videos=[_video_url('wrong', 55)]),
            _post('TARGETPOST', 'kfury', videos=[_video_url('right', 11)]),
        ])
        info = _extract(html, 'https://www.threads.com/@kfury/post/TARGETPOST')
        self.assertEqual(info['id'], 'TARGETPOST')
        self.assertEqual(info['uploader_id'], 'kfury')
        self.assertEqual(info['duration'], 11)
        self.assertIn('right.mp4', info['formats'][0]['url'])

    def test_missing_post_raises_instead_of_returning_another(self):
        """A deleted/gated post must error, never fall back to a random video."""
        html = _page([_post('SOMEONEELSE', 'stranger', videos=[_video_url('wrong', 55)])])
        with self.assertRaises(ExtractorError) as ctx:
            _extract(html, 'https://www.threads.com/@zuck/post/GONE12345')
        self.assertIn('GONE12345', str(ctx.exception))

    def test_share_link_resolves_via_og_url(self):
        html = _page(
            [_post('REALCODE99', 'hunter', videos=[_video_url('shared', 36)])],
            og_url='https://www.threads.com/@hunter/post/REALCODE99')
        info = _extract(html, 'https://www.threads.com/share/_abc123/')
        self.assertEqual(info['id'], 'REALCODE99')
        self.assertEqual(info['duration'], 36)

    def test_share_link_resolves_via_redirect_without_og_url(self):
        html = _page([
            _post('DECOY00001', 'decoy', videos=[_video_url('wrong', 3)]),
            _post('REDIRECT01', 'hunter', videos=[_video_url('shared', 36)]),
        ])
        info = _extract(
            html, 'https://www.threads.com/share/_abc123/',
            final_url='https://www.threads.com/@hunter/post/REDIRECT01?xmt=x&slof=1')
        self.assertEqual(info['id'], 'REDIRECT01')

    def test_text_post_with_linked_instagram_reel(self):
        """Live shape of threads.com/share/BBkykdNbKs (2026-11): a media_type 19
        text post whose video is an Instagram reel in linked_inline_media."""
        reel = _post('IGREEL0001', 'reel_author', videos=[_video_url('reel', 99)],
                     caption='reel caption')
        reel['video_duration'] = 128.4
        reel['permalink'] = 'https://www.instagram.com/reel/IGREEL0001/'
        post = _text_post('TEXTPOST01', 'poster', linked_inline_media=reel)
        # The reel is also collected as a post of its own; it must not shadow
        # the requested one.
        html = _page([post], og_url='https://www.threads.com/@poster/post/TEXTPOST01')
        info = _extract(html, 'https://www.threads.com/share/BBkykdNbKs')
        self.assertEqual(info['id'], 'TEXTPOST01')
        self.assertEqual(info['uploader_id'], 'poster')
        self.assertEqual(info['title'], 'my take on this')
        self.assertEqual(info['duration'], 128)  # video_duration beats efg
        self.assertIn('reel.mp4', info['formats'][0]['url'])

    def test_text_post_without_caption_uses_embedded_caption(self):
        reel = _post('IGREEL0002', 'reel_author', videos=[_video_url('reel', 9)],
                     caption='reel caption\nsecond line')
        post = _text_post('TEXTPOST02', 'poster', caption=None, linked_inline_media=reel)
        info = _extract(_page([post]), 'https://www.threads.com/@poster/post/TEXTPOST02')
        self.assertEqual(info['title'], 'reel caption')
        self.assertEqual(info['uploader_id'], 'poster')

    def test_repost_takes_reposted_metadata(self):
        original = _post('ORIGINAL01', 'creator', videos=[_video_url('orig', 20)],
                         caption='the original')
        repost = _text_post('REPOST0001', 'resharer', caption=None,
                            share_info={'reposted_post': original})
        info = _extract(_page([repost]), 'https://www.threads.com/@resharer/post/REPOST0001')
        self.assertEqual(info['id'], 'ORIGINAL01')
        self.assertEqual(info['uploader_id'], 'creator')
        self.assertEqual(info['title'], 'the original')

    def test_quote_post_keeps_quoting_metadata(self):
        quoted = _post('QUOTED0001', 'creator', videos=[_video_url('quoted', 20)])
        quote = _text_post('QUOTING001', 'commenter', caption='look at this',
                           share_info={'quoted_post': quoted})
        info = _extract(_page([quote]), 'https://www.threads.com/@commenter/post/QUOTING001')
        self.assertEqual(info['id'], 'QUOTING001')
        self.assertEqual(info['uploader_id'], 'commenter')
        self.assertIn('quoted.mp4', info['formats'][0]['url'])

    def test_repost_of_text_post_with_linked_reel(self):
        reel = _post('IGREEL0003', 'reel_author', videos=[_video_url('deep', 4)])
        original = _text_post('ORIGTEXT01', 'creator', caption='orig text',
                              linked_inline_media=reel)
        repost = _text_post('REPOST0002', 'resharer', caption=None,
                            share_info={'reposted_post': original})
        info = _extract(_page([repost]), 'https://www.threads.com/@resharer/post/REPOST0002')
        self.assertEqual(info['id'], 'ORIGTEXT01')
        self.assertEqual(info['uploader_id'], 'creator')
        self.assertIn('deep.mp4', info['formats'][0]['url'])

    def test_image_only_carousel_raises_clean_error(self):
        carousel = [_post('I1', 'photog', media_type=1), _post('I2', 'photog', media_type=1)]
        html = _page([_post('IMGCAROUS1', 'photog', carousel=carousel, media_type=8)])
        with self.assertRaises(ExtractorError) as ctx:
            _extract(html, 'https://www.threads.com/@photog/post/IMGCAROUS1')
        self.assertIn('images are not supported', str(ctx.exception))

    def test_text_only_post_raises_clean_error(self):
        post = _text_post('TEXTONLY01', 'writer')
        with self.assertRaises(ExtractorError) as ctx:
            _extract(_page([post]), 'https://www.threads.com/@writer/post/TEXTONLY01')
        self.assertIn('text-only', str(ctx.exception))

    def test_duplicate_copies_of_target_post_are_all_tried(self):
        """A bare copy of the post earlier on the page must not mask the full one."""
        bare = _text_post('DUPPOST001', 'poster')
        full = _post('DUPPOST001', 'poster', videos=[_video_url('full', 5)])
        info = _extract(_page([bare, full]), 'https://www.threads.com/@poster/post/DUPPOST001')
        self.assertIn('full.mp4', info['formats'][0]['url'])

    def test_image_only_post_raises_clean_error(self):
        html = _page([_post('IMGPOST123', 'photog', media_type=1, caption='a photo')])
        with self.assertRaises(ExtractorError) as ctx:
            _extract(html, 'https://www.threads.com/@photog/post/IMGPOST123')
        self.assertIn('image post', str(ctx.exception))

    def test_carousel_returns_multi_video_playlist(self):
        carousel = [
            _post('C1', 'creator', videos=[_video_url('part1', 5)]),
            _post('C2', 'creator', videos=[_video_url('part2', 7)]),
        ]
        html = _page([_post('CAROUSEL01', 'creator', carousel=carousel, media_type=8)])
        info = _extract(html, 'https://www.threads.com/@creator/post/CAROUSEL01')
        self.assertEqual(info['_type'], 'multi_video')
        entries = list(info['entries'])
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]['id'], 'CAROUSEL01_1')
        self.assertIn('part2.mp4', entries[1]['formats'][0]['url'])

    def test_silent_video_marked_acodec_none(self):
        html = _page([_post('SILENT0001', 'nosound',
                            videos=[_video_url('quiet', 9)], has_audio=False)])
        info = _extract(html, 'https://www.threads.com/@nosound/post/SILENT0001')
        self.assertEqual(info['formats'][0]['acodec'], 'none')

    def test_duplicate_renditions_are_deduped(self):
        """type 101/102/103 are usually the same asset re-signed."""
        same = 'https://scontent.cdninstagram.com/o1/v/same.mp4'
        post = _post('DEDUP00001', 'user', videos=[f'{same}?a=1', f'{same}?a=2'])
        info = _extract(_page([post]), 'https://www.threads.com/@user/post/DEDUP00001')
        self.assertEqual(len(info['formats']), 1)

    def test_metadata_and_thumbnails(self):
        html = _page([_post('META000001', 'kfury', videos=[_video_url('v', 11)])])
        info = _extract(html, 'https://www.threads.com/@kfury/post/META000001')
        self.assertEqual(info['uploader_url'], 'https://www.threads.com/@kfury')
        self.assertEqual(info['timestamp'], 1782587654)
        self.assertEqual(info['like_count'], 42)
        self.assertEqual(info['title'], 'hello world')
        self.assertEqual(len(info['thumbnails']), 2)

    def test_efg_decoding_is_resilient(self):
        self.assertEqual(ThreadsIE._efg_info('https://x/y.mp4'), {})
        self.assertEqual(ThreadsIE._efg_info('https://x/y.mp4?efg=!!!notbase64!!!'), {})

    def test_valid_url_matching(self):
        for url in (
            'https://www.threads.com/@user/post/ABC123',
            'https://www.threads.net/@user/post/ABC123',
            'https://threads.com/@user/post/ABC123',
            'https://www.threads.com/t/ABC123',
            'https://www.threads.com/share/ABC123/',
            'https://www.threads.com/@user/post/ABC123?xmt=tracking&slof=1',
        ):
            self.assertTrue(ThreadsIE.suitable(url), url)
        for url in ('https://www.threads.com/@user', 'https://example.com/@u/post/X'):
            self.assertFalse(ThreadsIE.suitable(url), url)


if __name__ == '__main__':
    unittest.main()
