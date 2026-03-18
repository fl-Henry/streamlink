"""YouTube data extractors for video and channel pages.

Provides extractors that handle:
- Tab pages (channel/live pages) -> video URLs
- Video pages -> HLS manifest URLs with n-challenge solving
"""

import json
import logging
import re
import time
from urllib.parse import urlparse, urlunparse

import requests
from requests import Response

from streamlink.plugin.api import validate
from streamlink.utils.parse import parse_json

from .structures import JsChallengeRequest, JsChallengeType, NChallengeInput, ExtractorType, ExtractorResult, NextExtractor, ctx

log = logging.getLogger(__name__)


CLIENTS = {
    # 'web': {
    #     'INNERTUBE_CONTEXT': {
    #         'client': {
    #             'clientName': 'WEB',
    #             'clientVersion': '2.20260309.01.00',
    #             'userAgent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
    #         },
    #     },
    #     'INNERTUBE_CONTEXT_CLIENT_NAME': 1,
    #     'SUPPORTS_COOKIES': True,
    # },
    'web_safari': {
        'INNERTUBE_CONTEXT': {
            'client': {
                'clientName': 'WEB',
                'clientVersion': '2.20260114.08.00',
                'userAgent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.5 Safari/605.1.15,gzip(gfe)',
            },
        },
        'INNERTUBE_CONTEXT_CLIENT_NAME': 1,
        'SUPPORTS_COOKIES': True,
    },
    'android_vr': {
        'INNERTUBE_CONTEXT': {
            'client': {
                'clientName': 'ANDROID_VR',
                'clientVersion': '1.65.10',
                'deviceMake': 'Oculus',
                'deviceModel': 'Quest 3',
                'androidSdkVersion': 32,
                'userAgent': 'com.google.android.apps.youtube.vr.oculus/1.65.10 (Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip',
                'osName': 'Android',
                'osVersion': '12L',
            },
        },
        'INNERTUBE_CONTEXT_CLIENT_NAME': 28,
        'REQUIRE_JS_PLAYER': False,
    },
}


def _get_data_from_regex(res: Response, regex, descr):
    """Extract and parse JSON data from Response using regex."""
    if match := re.search(regex, res.text):
        return parse_json(match.group(1))
    log.debug(f"Missing {descr}")


class TabExtractor:
    """Extracts video ID from YouTube channel/live pages."""
    valid_url_re = r'https://www\.youtube\.com/(?:@|c(?:hannel)?/|user/)?(?P<id>[^/?\#&]+)/live'
    extractor_type: ExtractorType = ExtractorType.TAB
    _re_ytInitialData = re.compile(r"""var\s+ytInitialData\s*=\s*({.*?})\s*;\s*</script>""", re.DOTALL)

    @staticmethod
    def _schema_video_id(data):
        """Validate and extract video ID from ytInitialData."""
        schema = validate.Schema(
            {
                "currentVideoEndpoint": {
                    "watchEndpoint": {
                        "videoId": str,
                    },
                },
            },
            validate.get(("currentVideoEndpoint", "watchEndpoint", "videoId")),
        )
        return schema.validate(data)

    def _get_initial_data(self, url):
        """Fetch ytInitialData from channel page with retries."""
        for _ in range(3):
            try:
                log.debug(f"TabExtractor: getting ytInitialData for {url=}")
                webpage = ctx.session.http.get(url)
                initial = _get_data_from_regex(webpage, self._re_ytInitialData, "ytInitialData")
                # YouTube sometimes returns incomplete data
                if initial and initial.get('currentVideoEndpoint'):
                    return initial
            except Exception as e:
                log.error(f"Failed to get ytInitialData: {e}")
        return {}

    def extract(self, url) -> ExtractorResult:
        """Extract video URL from channel/live page."""
        url = urlunparse(urlparse(url)._replace(netloc='www.youtube.com'))
        initial = self._get_initial_data(url)
        if video_id := self._schema_video_id(initial):
            return ExtractorResult(
                next=NextExtractor(
                    extractor=ExtractorType.VIDEO,
                    url=f'https://www.youtube.com/watch?v={video_id}'
                )
            )
        raise ValueError('Unable to extract video ID from tab page')


class VideoExtractor:
    """Extracts HLS manifest URLs from YouTube video pages."""
    valid_url_re = r'https://www\.youtube\.com/watch\?v=(?P<id>[0-9A-Za-z_-]{11})'
    extractor_type = ExtractorType.VIDEO
    _re_ytcfg = re.compile(r'ytcfg\.set\s*\(\s*({.+?})\s*\)\s*;')
    _re_ytInitialPlayerResponse = re.compile(r"""var\s+ytInitialPlayerResponse\s*=\s*({.+?});\s*(?:var|</script>)""")
    video_id: str = None

    def _get_webpage_data(self, url):
        """Fetch and parse YouTube video page data."""
        log.debug(f"VideoExtractor: getting webpage for {url=}")
        ctx.session.http.headers['User-Agent'] = CLIENTS['web_safari']['INNERTUBE_CONTEXT']['client']['userAgent']
        # webpage = ctx.session.http.get(url, params={'bpctr': '9999999999', 'has_verified': '1'})
        webpage = requests.get(url, params={'bpctr': '9999999999', 'has_verified': '1'})
        webpage_ytcfg = _get_data_from_regex(webpage, self._re_ytcfg, "ytcfg")
        return webpage_ytcfg

    @staticmethod
    def _build_headers(client_config, visitor_data, client_context):
        """Build request headers for InnerTube API."""
        client_version = client_context.get('clientVersion')
        if not client_version:
            client_version = client_config.get('INNERTUBE_CONTEXT', {}).get('client', {}).get('clientVersion'),
        ua = client_context.get('userAgent') or client_config.get('INNERTUBE_CONTEXT', {}).get('client', {}).get('userAgent')
        return {
            k: v for k, v in {
                'X-YouTube-Client-Name': str(client_config.get('INNERTUBE_CONTEXT_CLIENT_NAME')),
                'X-YouTube-Client-Version': client_version,
                'Origin': 'https://www.youtube.com',
                'X-Goog-Visitor-Id': visitor_data,
                'User-Agent': ua,
                'content-type': 'application/json',
            }.items() if v is not None
        }

    def _build_player_request_payload(self, client_context, webpage_ytcfg):
        """Build InnerTube player request payload."""

        return {
            'context': client_context,
            'videoId': self.video_id,
            'playbackContext': {
                'contentPlaybackContext': {
                    'html5Preference': 'HTML5_PREF_WANTS',
                    # 'signatureTimestamp': 20514
                    **({'signatureTimestamp': sts} if (sts := webpage_ytcfg.get('STS')) else {})
                },
            },
            'contentCheckOk': True,
            'racyCheckOk': True
        }

    def _extract_player_response(self, client, webpage_ytcfg, visitor_data):
        """Request player response from InnerTube API."""
        client_config = CLIENTS[client].copy()
        context = webpage_ytcfg.get('INNERTUBE_CONTEXT', {}) or client_config.get('INNERTUBE_CONTEXT', {})
        client_context = context.get('client', {})
        client_context.update({'hl': 'en', 'timeZone': 'UTC', 'utcOffsetMinutes': 0})

        headers = self._build_headers(client_config, visitor_data, client_context)
        print(f"{headers=}")
        data = self._build_player_request_payload(context, webpage_ytcfg)
        print(f"{data=}")
        response = ctx.session.http.post(
            'https://www.youtube.com/youtubei/v1/player',
            params={'prettyPrint': 'false'},
            headers=headers,
            data=json.dumps(data).encode('utf8'),
        )
        return response.json()

    @staticmethod
    def _schema_visitor_data(data):
        """Validate and extract visitor data."""
        schema = validate.Schema(
            validate.any(
                validate.all(
                    {"VISITOR_DATA": str},
                    validate.get("VISITOR_DATA"),
                ),
                validate.all(
                    {"INNERTUBE_CONTEXT": {"client": {"visitorData": str}}},
                    validate.get(("INNERTUBE_CONTEXT", "client", "visitorData")),
                ),
            ),
        )
        return schema.validate(data)

    @staticmethod
    def _schema_player_url(data):

        schema = validate.Schema(
            validate.any(
                validate.all(
                    {"PLAYER_JS_URL": str},
                    validate.get("PLAYER_JS_URL"),
                ),
                validate.all(
                    {
                        "WEB_PLAYER_CONTEXT_CONFIGS": {
                            str: {"jsUrl": str}
                        }
                    },
                    validate.get("WEB_PLAYER_CONTEXT_CONFIGS"),
                    validate.transform(lambda x: next(iter(x.values()))),
                    validate.get("jsUrl"),
                ),
            ),
            validate.transform(
                lambda url: url if url.startswith("https://www.youtube.com") else f"https://www.youtube.com{url}"),
        )
        return schema.validate(data)

    def _extract_player_responses(self, webpage_ytcfg):
        """Extract player responses from multiple InnerTube clients."""
        player_responses = []
        visitor_data = self._schema_visitor_data(webpage_ytcfg)

        player_url = self._schema_player_url(webpage_ytcfg)

        # Try clients in reverse order
        for client in reversed(list(CLIENTS.keys())):
            try:
                player_response = self._extract_player_response(
                    client,
                    webpage_ytcfg=webpage_ytcfg,
                    visitor_data=visitor_data,
                )
            except Exception as e:
                log.error(f'Could not get player_response for {client}: {e}')

                continue

            if player_response:
                # Annotate response with client metadata
                if not player_response.get('streamingData'):
                    log.warning(f'No streamingData in player_response for {client}')
                    continue
                player_responses.append(player_response)

        if not player_responses:
            raise ValueError('Failed to extract any player response')

        return player_responses, player_url

    def _solve_n_challenge(self, n_challenge, player_url):
        """Solve YouTube n-parameter challenge using Deno."""
        challenge = JsChallengeRequest(
            type=JsChallengeType.N,
            video_id=self.video_id,
            input=NChallengeInput(challenge=n_challenge, player_url=player_url)
        )
        return ctx.deno.solve(challenge)

    def _extract_hls(self, player_responses, player_url):
        """Extract HLS manifest URLs and solve n-challenges."""
        hls_list = []

        for player_response in player_responses:
            print("player_response")
            streaming_data = player_response.get('streamingData')
            if not streaming_data:
                print("if not streaming_data:")
                continue

            hls_manifest_url = streaming_data.get('hlsManifestUrl')
            if not hls_manifest_url:
                print("if not hls_manifest_url:")
                print("streamingData")
                print(streaming_data)
                continue

            # Extract and solve n-challenge if present
            if matches := re.findall(r'/n/([^/]+)/', urlparse(hls_manifest_url).path):
                n_challenge = matches[0]
                print(f"{n_challenge=}")
                challenge_response = self._solve_n_challenge(n_challenge, player_url)

                if challenge_response and (n_result := challenge_response.output.results.get(n_challenge)):
                    hls_manifest_url = hls_manifest_url.replace(f'/n/{n_challenge}', f'/n/{n_result}')
                    hls_list.append(hls_manifest_url)
                else:
                    log.warning(f'Failed to solve n challenge: {n_challenge}')
            else:
                hls_list.append(hls_manifest_url)

        return hls_list

    def extract(self, url: str) -> ExtractorResult:
        """Extract HLS manifest URLs from video page."""
        hls = []
        for _ in range(3):
            time.sleep(1)
            self.video_id = re.search(self.valid_url_re, url).group('id')
            webpage_ytcfg = self._get_webpage_data(url)

            try:
                player_responses, player_url = self._extract_player_responses(webpage_ytcfg)
            except ValueError as e:
                log.error(f"Failed to extract player responses: {e}")
                continue
            hls = self._extract_hls(player_responses, player_url)
            if not hls:
                continue
            break

        return ExtractorResult(hls=hls)
