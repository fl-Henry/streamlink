import urllib.parse
import json
import re
import time
from typing import Protocol
from dataclasses import dataclass
from enum import auto, StrEnum
from urllib.parse import urlparse

import requests

from streamlink.session.session import Streamlink

from .deno import DenoJCP
from .utils import JsChallengeRequest, JsChallengeType, NChallengeInput

INNERTUBE_CLIENTS = {
    'web': {
        'INNERTUBE_CONTEXT': {
            'client': {
                'clientName': 'WEB',
                'clientVersion': '2.20260114.08.00',
            },
        },
        'INNERTUBE_CONTEXT_CLIENT_NAME': 1,
    },
    # Safari UA returns pre-merged video+audio 144p/240p/360p/720p/1080p HLS formats
    'web_safari': {
        'INNERTUBE_CONTEXT': {
            'client': {
                'clientName': 'WEB',
                'clientVersion': '2.20260114.08.00',
                'userAgent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) '
                             'Version/15.5 Safari/605.1.15,gzip(gfe)',
            },
        },
        'INNERTUBE_CONTEXT_CLIENT_NAME': 1,
    },
    # Doesn't require a PoToken for some reason
    'android_sdkless': {
        'INNERTUBE_CONTEXT': {
            'client': {
                'clientName': 'ANDROID',
                'clientVersion': '21.02.35',
                'userAgent': 'com.google.android.youtube/21.02.35 (Linux; U; Android 11) gzip',
                'osName': 'Android',
                'osVersion': '11',
            },
        },
        'INNERTUBE_CONTEXT_CLIENT_NAME': 3,
        'REQUIRE_JS_PLAYER': False,
    },
}


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
class ExtractorResponse:
    next: NextExtractor | None = None
    hls: list[str] | None = None


class Extractor(Protocol):
    valid_url_re: str
    extractor_type: ExtractorType

    def extract(self, url: str) -> ExtractorResponse:
        ...


class TabExtractor:
    valid_url_re = r'https://www\.youtube\.com/channel/(?P<id>[^/?\#&]+)/live'
    extractor_type = ExtractorType.TAB

    def extract(self, url) -> ExtractorResponse:
        url = urllib.parse.urlunparse(urllib.parse.urlparse(url)._replace(netloc='www.youtube.com'))
        data = {}
        for retry in range(3):
            try:
                print(f"request: {url}")
                webpage = requests.get(url).text
                if match := re.findall(r'ytInitialData = (.*?);</script>', webpage):
                    data = json.loads(match[0])
                else:
                    continue
            except Exception as e:
                continue

            # Sometimes youtube returns a webpage with incomplete ytInitialData
            if not data.get('currentVideoEndpoint'):
                data = None
                continue

            break

        video_id = data.get('currentVideoEndpoint', {}).get('watchEndpoint', {}).get('videoId', None)
        if video_id:
            return ExtractorResponse(
                next=NextExtractor(
                    extractor=ExtractorType.VIDEO,
                    url=f'https://www.youtube.com/watch?v={video_id}'
                )
            )
        raise Exception('Unable to recognize tab page')


class VideoExtractor:
    valid_url_re = r'https://www\.youtube\.com/watch\?v=(?P<id>[0-9A-Za-z_-]{11})'
    extractor_type = ExtractorType.VIDEO

    def _extract_player_response(self, client, video_id, webpage_ytcfg, visitor_data):
        default_ytcfg = INNERTUBE_CLIENTS[client].copy()
        headers = {
            'X-YouTube-Client-Name': str(default_ytcfg.get('INNERTUBE_CONTEXT_CLIENT_NAME')),
            'X-YouTube-Client-Version': default_ytcfg.get('INNERTUBE_CONTEXT', {}).get('client', {}).get('clientVersion'),
            'Origin': 'https://www.youtube.com',
            'X-Goog-Visitor-Id': visitor_data,
            'User-Agent': default_ytcfg.get('INNERTUBE_CONTEXT', {}).get('client', {}).get('userAgent'),
        }
        headers = {k: v for k, v in headers.items() if v is not None}

        yt_query = {
            'videoId': video_id,
            'playbackContext': {
                'contentPlaybackContext': {
                    'html5Preference': 'HTML5_PREF_WANTS',
                    **({'signatureTimestamp': sts} if (sts := webpage_ytcfg.get('STS')) else {})
                },
            },
            'contentCheckOk': True,
            'racyCheckOk': True
        }

        context = default_ytcfg.get('INNERTUBE_CONTEXT', {})
        client_context = context.get('client', {})
        client_context.update({'hl': 'en', 'timeZone': 'UTC', 'utcOffsetMinutes': 0})  # Enforce language and tz for extraction
        data = {'context': context}
        data.update(yt_query)

        response = requests.post(
            'https://www.youtube.com/youtubei/v1/player',
            params={'prettyPrint': 'false'},
            headers={
                'content-type': 'application/json',
                **headers
            },
            data=json.dumps(data).encode('utf8'),
        )
        print(f'Requested: {response.url}')
        return response.json()

    def _extract_player_responses(self, video_id, webpage, webpage_ytcfg):
        webpage_client = 'web'
        player_responses = []
        deprioritized_prs = []
        clients = ['web_safari', 'web', 'android_sdkless']

        initial_player_response = json.loads(x[0]) if (x := re.findall(r'ytInitialPlayerResponse\s*=(.*?);</script>', webpage)) else {}

        # Extract and complete the player JS url from `player_ytcfg` or `webpage_ytcfg`
        player_url = js_url if (js_url := webpage_ytcfg.get('PLAYER_JS_URL')) else None
        if not player_url:
            player_url: str = cfgs[0] if (cfgs := [
                js_url
                for x in webpage_ytcfg.get('WEB_PLAYER_CONTEXT_CONFIGS', {}).values()
                if (js_url := x.get('jsUrl'))
            ]) else None
        player_url = f'https://www.youtube.com{player_url}' if not player_url.startswith("https://www.youtube.com") else player_url

        visitor_data = webpage_ytcfg.get('VISITOR_DATA') or webpage_ytcfg.get('INNERTUBE_CONTEXT', {}).get('client', {}).get('visitorData')

        while clients:
            client = clients.pop()

            # Extract or requests player_response
            if client == webpage_client:
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

        player_responses.extend(deprioritized_prs)

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

                    challenge_response = self.deno.solve(challenge)
                    n_result = challenge_response.output.results.get(n_challenge)
                    if n_result:
                        hls_manifest_url = hls_manifest_url.replace(f'/n/{n_challenge}', f'/n/{n_result}')
                        hls_list.append(hls_manifest_url)
                    else:
                        print(f'WARNING: Failed to solve n challenge {n_challenge}')

        return hls_list

    def extract(self, url: str) -> ExtractorResponse:

        print("url", url)
        video_id = re.match(self.VALID_URL_RE, url).group('id')
        webpage_url = f'https://www.youtube.com/watch?v={video_id}'

        webpage = requests.get(webpage_url, params={'bpctr': '9999999999', 'has_verified': '1'})
        print(f'Requested: {webpage.url}')
        webpage = webpage.text
        webpage_ytcfg = json.loads(x[0]) if (x := re.findall(r'ytcfg\.set\s*\(\s*({.+?})\s*\)\s*;', webpage)) else {}

        player_responses, player_url = self._extract_player_responses(video_id, webpage, webpage_ytcfg)

        return ExtractorResponse(
            hls=self._extract_hls(video_id, player_responses, player_url)
        )


ctx = Context()
