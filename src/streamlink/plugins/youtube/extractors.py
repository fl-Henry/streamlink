import logging
import json
import re
import time

from typing import Protocol
from dataclasses import dataclass
from enum import auto, StrEnum
from urllib.parse import urlunparse, urlparse


import requests

from streamlink.session.session import Streamlink
from streamlink.utils.parse import parse_json
from streamlink.plugin.api import validate

from .deno import DenoJCP, JsChallengeRequest, JsChallengeType, NChallengeInput

log = logging.getLogger(__name__)

INNERTUBE_CLIENTS = {
    'web': {
        'INNERTUBE_CONTEXT': {
            'client': {
                'clientName': 'WEB',
                'clientVersion': '2.20260309.01.00',
                'userAgent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36,gzip(gfe)',
            },
        },
        'INNERTUBE_CONTEXT_CLIENT_NAME': 1,
        'SUPPORTS_COOKIES': True,
        'SUPPORTS_AD_PLAYBACK_CONTEXT': True,
    },
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
        'SUPPORTS_AD_PLAYBACK_CONTEXT': True,
    },
    'android_vr': {
        'INNERTUBE_CONTEXT': {
            'client': {
                'clientName': 'ANDROID_VR',
                'clientVersion': '1.71.26',
                'deviceMake': 'Oculus',
                'deviceModel': 'Quest 3',
                'androidSdkVersion': 32,
                'userAgent': 'com.google.android.apps.youtube.vr.oculus/1.71.26 (Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip',
                'osName': 'Android',
                'osVersion': '12L',
            },
        },
        'INNERTUBE_CONTEXT_CLIENT_NAME': 28,
        'REQUIRE_JS_PLAYER': False,
    },
}


def _get_data_from_regex(res, regex, descr):
    match = re.search(regex, res.text)
    if not match:
        log.debug(f"Missing {descr}")
        return
    return parse_json(match.group(1))


@dataclass
class Context:
    session: Streamlink = None
    deno: DenoJCP = None


class ExtractorType(StrEnum):
    VIDEO = auto()
    TAB = auto()


@dataclass(frozen=True)
class NextExtractor:
    extractor: ExtractorType
    url: str


@dataclass(frozen=True)
class ExtractorResult:
    next: NextExtractor | None = None
    hls: list[str] | None = None


class Extractor(Protocol):
    valid_url_re: str
    extractor_type: ExtractorType

    def extract(self, url: str) -> ExtractorResult:
        ...


class TabExtractor:
    valid_url_re = r'https://www\.youtube\.com/(?:@|c(?:hannel)?/|user/)?(?P<id>[^/?\#&]+)/live'
    extractor_type = ExtractorType.TAB
    _re_ytInitialData = re.compile(r"""var\s+ytInitialData\s*=\s*({.*?})\s*;\s*</script>""", re.DOTALL)

    @staticmethod
    def _schema_video_id(data):
        from streamlink.plugin.api import validate
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
        for _ in range(3):
            try:
                log.debug(f"TabExtractor: getting ytInitialData for {url=}")
                webpage = ctx.session.http.get(url)
                initial = _get_data_from_regex(webpage, self._re_ytInitialData, "ytInitialData")
            except Exception as e:
                log.error(f"Failed to get ytInitialData: {e}")
                continue

            # Sometimes youtube returns a webpage with incomplete ytInitialData
            if not initial.get('currentVideoEndpoint'):
                continue

            return initial
        return {}

    def extract(self, url) -> ExtractorResult:
        url = urlunparse(urlparse(url)._replace(netloc='www.youtube.com'))
        initial = self._get_initial_data(url)
        if video_id := self._schema_video_id(initial):
            return ExtractorResult(
                next=NextExtractor(
                    extractor=ExtractorType.VIDEO,
                    url=f'https://www.youtube.com/watch?v={video_id}'
                )
            )
        raise Exception('Unable to recognize tab page')


class VideoExtractor:
    valid_url_re = r'https://www\.youtube\.com/watch\?v=(?P<id>[0-9A-Za-z_-]{11})'
    extractor_type = ExtractorType.VIDEO
    _re_ytcfg = re.compile(r'ytcfg\.set\s*\(\s*({.+?})\s*\)\s*;')
    _re_ytInitialPlayerResponse = re.compile(r"""var\s+ytInitialPlayerResponse\s*=\s*({.+?});\s*(?:var|</script>)""")

    def _get_webpage_data(self, url):
        log.debug(f"VideoExtractor: getting webpage for {url=}")
        url, smuggled_data, webpage_url, webpage_client, video_id = url, {}, url, 'web', url.split("=")[-1]
        webpage = requests.get(url, params={'bpctr': '9999999999', 'has_verified': '1'})
        webpage_ytcfg = _get_data_from_regex(webpage, self._re_ytcfg, "ytcfg")
        initial_player_response = _get_data_from_regex(webpage, self._re_ytInitialPlayerResponse, "ytInitialPlayerResponse")
        return webpage_ytcfg, initial_player_response

    def _extract_player_response(self, client, video_id, webpage_ytcfg, visitor_data):
        default_ytcfg = INNERTUBE_CLIENTS[client].copy()
        headers = {
            'X-YouTube-Client-Name': str(default_ytcfg.get('INNERTUBE_CONTEXT_CLIENT_NAME')),
            'X-YouTube-Client-Version': default_ytcfg.get('INNERTUBE_CONTEXT', {}).get('client', {}).get('clientVersion'),
            'Origin': 'https://www.youtube.com',
            'X-Goog-Visitor-Id': visitor_data,
            'User-Agent': default_ytcfg.get('INNERTUBE_CONTEXT', {}).get('client', {}).get('userAgent'),
            'content-type': 'application/json',
        }
        headers = {k: v for k, v in headers.items() if v is not None}

        yt_query = {
            'videoId': video_id,
            'playbackContext': {
                'contentPlaybackContext': {
                    'html5Preference': 'HTML5_PREF_WANTS',
                    # **({'signatureTimestamp': sts} if (sts := webpage_ytcfg.get('STS')) else {})
                    'signatureTimestamp': 20514
                },
            },
            'contentCheckOk': True,
            'racyCheckOk': True
        }

        context = webpage_ytcfg.get('INNERTUBE_CONTEXT', {}) or default_ytcfg.get('INNERTUBE_CONTEXT', {})

        client_context = context.get('client', {})
        client_context.update({'hl': 'en', 'timeZone': 'UTC', 'utcOffsetMinutes': 0})  # Enforce language and tz for extraction
        data = {'context': context}
        data.update(yt_query)

        response = requests.post(
            'https://www.youtube.com/youtubei/v1/player',
            params={'prettyPrint': 'false'},
            headers=headers,
            data=json.dumps(data).encode('utf8'),
        )
        return response.json()

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
            validate.transform(lambda url: url if url.startswith("https://www.youtube.com") else f"https://www.youtube.com{url}"),
        )
        return schema.validate(data)

    @staticmethod
    def _schema_visitor_data(data):
        from streamlink.plugin.api import validate
        schema = validate.Schema(
            validate.any(
                validate.all(
                    {"VISITOR_DATA": str},
                    validate.get("VISITOR_DATA"),
                ),
                validate.all(
                    {
                        "INNERTUBE_CONTEXT": {
                            "client": {"visitorData": str}
                        }
                    },
                    validate.get(("INNERTUBE_CONTEXT", "client", "visitorData")),
                ),
            ),
        )
        return schema.validate(data)

    def _extract_player_responses(self, video_id, webpage_ytcfg, initial_player_response):

        webpage_client = 'web'
        player_responses = []
        clients = list(INNERTUBE_CLIENTS.keys())[::-1]

        player_url = 'https://www.youtube.com/s/player/9f4cc5e4/tv-player-ias.vflset/tv-player-ias.js'
        visitor_data = self._schema_visitor_data(webpage_ytcfg)

        while clients:
            client = clients.pop()

            # Extract or requests player_response
            if client == webpage_client and False:
                innertube_context = webpage_ytcfg.get('INNERTUBE_CONTEXT')
                player_response = initial_player_response
            else:
                try:
                    player_response = self._extract_player_response(
                        client, video_id,
                        webpage_ytcfg=webpage_ytcfg,
                        visitor_data=visitor_data,
                    )
                    innertube_context = INNERTUBE_CLIENTS[client].copy().get('INNERTUBE_CONTEXT')
                except Exception as e:
                    print(f'ERROR::Could not get player_response: {e}')
                    continue

            if player_response:
                # Save client details for introspection later
                sd = player_response.setdefault('streamingData', {})
                sd['__yt_dlp_client'] = client
                sd['__yt_dlp_innertube_context'] = innertube_context
                sd['__yt_dlp_available_at_timestamp'] = int(time.time())
                player_responses.append(player_response)

        if not player_responses:
            raise Exception('Failed to extract any player response')

        return player_responses, player_url

    def _extract_hls(self, video_id, player_responses, player_url):
        # Final pass to extract formats and solve n challenges as needed
        hls_list = []
        for player_response in player_responses:

            if not (streaming_data := player_response.get('streamingData')):
                continue

            if hls_manifest_url := streaming_data.get('hlsManifestUrl'):
                n_challenge = x[0] if (x := re.findall(r'/n/([^/]+)/', urlparse(hls_manifest_url).path)) else None
                if n_challenge:
                    challenge = JsChallengeRequest(
                        type=JsChallengeType.N,
                        video_id=video_id,
                        input=NChallengeInput(challenge=n_challenge, player_url=player_url))

                    challenge_response = ctx.deno.solve(challenge)
                    if challenge_response and (n_result := challenge_response.output.results.get(n_challenge)):
                        hls_manifest_url = hls_manifest_url.replace(f'/n/{n_challenge}', f'/n/{n_result}')
                        hls_list.append(hls_manifest_url)
                    else:
                        print(f'WARNING: Failed to solve n challenge {n_challenge}')

        return hls_list

    def extract(self, url: str) -> ExtractorResult:
        video_id = re.search(self.valid_url_re, url).group('id')
        webpage_ytcfg, initial_player_response = self._get_webpage_data(url)
        player_responses, player_url = self._extract_player_responses(video_id, webpage_ytcfg, initial_player_response)
        hls = self._extract_hls(video_id, player_responses, player_url)

        return ExtractorResult(hls=hls)


ctx = Context()
