"""
$description Global live-streaming and video hosting social platform owned by Google.
$url youtube.com
$url youtu.be
$type live
$metadata id
$metadata author
$metadata category
$metadata title
$notes VOD content and protected videos are not supported
"""


from dataclasses import dataclass, field
from enum import StrEnum, auto
from typing import Protocol

import re
import json
import time
from urllib.parse import urlparse, urlunparse

from requests import Response

from streamlink.options import Options
from streamlink.session.session import Streamlink
from streamlink.logger import getLogger
from streamlink.plugin import PluginError, Plugin, pluginmatcher, pluginargument
from streamlink.plugin.api import useragents, validate
from streamlink.stream.ffmpegmux import MuxedStream
from streamlink.stream.http import HTTPStream
from streamlink.stream.hls import HLSStream
from streamlink.utils.parse import parse_json
from streamlink.utils.data import search_dict
from streamlink.utils.deno import Deno
import streamlink.solvers.youtube as solver

log = getLogger(__name__)

# Default client configurations
# Each entry supplies default INNERTUBE_CONTEXT and the numeric client-name
CLIENTS = {
    "web_safari": {
        "INNERTUBE_CONTEXT": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20260114.08.00",
                "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                             "(KHTML, like Gecko) Version/15.5 Safari/605.1.15,gzip(gfe)",
            },
        },
        "INNERTUBE_CONTEXT_CLIENT_NAME": 1,
    },
    "android_vr": {
        "INNERTUBE_CONTEXT": {
            "client": {
                "clientName": "ANDROID_VR",
                "clientVersion": "1.65.10",
                "deviceMake": "Oculus",
                "deviceModel": "Quest 3",
                "androidSdkVersion": 32,
                "userAgent": "com.google.android.apps.youtube.vr.oculus/1.65.10 (Linux; U; Android 12L; "
                             "eureka-user Build/SQ3A.220605.009.A1) gzip",
                "osName": "Android",
                "osVersion": "12L",
            },
        },
        "INNERTUBE_CONTEXT_CLIENT_NAME": 28,
    },
}

_re_ytInitialData = re.compile(
    r"""var\s+ytInitialData\s*=\s*({.*?})\s*;\s*</script>""",
    re.DOTALL,
)



class StreamPick(StrEnum):
    """Named ordering options for stream selection."""
    FIRST = auto()
    LAST = auto()
    POPULAR = auto()


@dataclass(frozen=True)
class StreamSelection:
    """Represents a stream selection option from the /streams page.

    Can be ``first``, ``last``, ``popular``, or a 1-based position number.

    Args:
        value: A :class:`StreamPick` value, or a positive :class:`int` position.
    """
    value: StreamPick | int

    def __post_init__(self):
        try:
            if isinstance(self.value, str) and self.value.isdigit():
                object.__setattr__(self, "value", int(self.value))
            elif isinstance(self.value, str):
                object.__setattr__(self, "value", StreamPick(self.value))
            if isinstance(self.value, int) and self.value < 1:
                raise ValueError()
        except ValueError:
            log.warning("Invalid stream selection option %r, defaulting to %r", self.value, StreamPick.POPULAR.value)
            object.__setattr__(self, "value", StreamPick.POPULAR)


@dataclass(frozen=True)
class NChallengeInput:
    """Input data for a YouTube n-parameter challenge.

    Args:
        player_url: URL of the YouTube player JS bundle containing the solvers.
        token: The raw ``n`` query parameter value to be transformed.
    """
    player_url: str
    token: str


@dataclass(frozen=True)
class NChallengeOutput:
    """Result of a solved n-parameter challenge.

    Args:
        results: Mapping of original ``n`` token -> solved token.
    """
    results: dict[str, str] = field(default_factory=dict)


class ExtractorType(StrEnum):
    """Discriminator for the YouTube Extractors."""
    VIDEO = auto()  # youtube.com/watch?v=<id>
    LIVE = auto()  # youtube.com/@handle/live, /channel/<id>/live, etc.
    STREAMS = auto()  # youtube.com/@handle/streams, /channel/<id>/streams, etc.


@dataclass(frozen=True)
class NextExtractor:
    """Redirect instruction returned when one extractor defers to another.

    Args:
        extractor: Target extractor type to invoke next.
        url: Resolved URL to pass to that extractor.
    """
    extractor: ExtractorType
    url: str


@dataclass(frozen=True)
class ExtractorResult:
    """Return value from any extractor.

    Exactly one field should be set per result:

    Args:
        next: Populated when the extractor delegates to another extractor.
        hls:  Populated when the extractor has resolved final HLS manifest URLs.
    """
    next: NextExtractor | None = None
    hls: list[str] | None = None


class Extractor(Protocol):
    """Structural interface that every YouTube extractor must implement."""

    valid_url_re: str
    """Regex pattern used to decide whether this extractor owns a given URL."""

    extractor_type: ExtractorType
    """Enum value that identifies this extractor."""

    def extract(self, url: str) -> ExtractorResult:
        """Run extraction for *url* and return a result or a redirect.

        Args:
            url: The URL to extract streams from.

        Returns:
            An :class:`ExtractorResult` with either ``next`` or ``hls`` set.
        """
        ...


@dataclass
class Context:
    """Shared runtime state injected into extractors and solvers.

    Args:
        session: Active :class:`~streamlink.session.Streamlink` session.
        deno:    JS solvers instance (e.g. Deno-backed) for n-challenges.
    """
    session: Streamlink = None
    options: Options = None


# Module-level singleton — populated by the plugin entry point before
# any extractor or solvers is invoked.
ctx = Context()


class DenoSolver(Deno):
    """Solves YouTube n-parameter challenges by executing JS inside Deno."""

    def __init__(self):
        # player_url -> raw JS source text
        super().__init__()
        self._code_cache: dict[str, str] = {}

    @staticmethod
    def validate_response(response: NChallengeOutput, request: NChallengeInput) -> bool | str:
        """Validate that *response* is a well-formed, successful challenge result.

        A result is considered invalid when:

        - *response* is not an :class:`NChallengeOutput` instance.
        - Any key or value in ``results`` is not a plain string.
        - The original token is absent from ``results``.
        - A result value ends with the original challenge token, which indicates
          the YouTube JS solver function raised an internal exception and echoed
          the input back as the output.

        Args:
            response: Output produced by the Deno subprocess.
            request:  Original challenge input used to generate *response*.

        Returns:
            ``True`` when the response is valid, or an error message string
            describing the first problem found.
        """
        if not isinstance(response, NChallengeOutput):
            return "Response is not an NChallengeOutput"

        if not (
            all(isinstance(k, str) and isinstance(v, str) for k, v in response.results.items())
            and request.token in response.results
        ):
            return "Invalid NChallengeOutput: missing token or non-string entries"

        # When the JS solver throws internally it returns the input token as the
        # result, so a result that ends with the original challenge is a failure.
        for challenge, result in response.results.items():
            if result.endswith(challenge):
                return f"n result is invalid for {challenge!r}: {result!r}"

        return True

    @staticmethod
    def _get_script(script_type: str) -> str:
        """Load a bundled solver script by name.

        Args:
            script_type: Either ``"core"`` or ``"lib"``.

        Returns:
            JS source string for the requested script.

        Raises:
            ValueError: If the script cannot be loaded from the package.
        """
        try:
            return solver.core() if script_type == "core" else solver.lib()
        except Exception as exc:
            raise ValueError(
                f'Failed to load solver "{script_type}" script from package: {exc}'
            ) from exc

    def _construct_stdin(self, player: str, request: NChallengeInput) -> str:
        """Build the JS source string that is piped to the Deno process.

        Inlines the ``lib`` and ``core`` solver scripts, then calls the
        exported ``jsc`` function with a JSON-serialized request payload.

        Args:
            player:  Raw YouTube player JS source code.
            request: Challenge input containing the token to solve.

        Returns:
            Multi-line JS string ready to be written to the subprocess stdin.
        """
        data = {
            "type": "player",
            "player": player,
            "requests": [{"type": "n", "challenges": [request.token]}],
            "output_preprocessed": True,
        }
        return (
            f"{self._get_script('lib')}\n"
            f"Object.assign(globalThis, lib);\n"
            f"{self._get_script('core')}\n"
            f"console.log(JSON.stringify(jsc({json.dumps(data)})));\n"
        )

    def _get_player(self, player_url: str) -> str | None:
        """Return the player JS source for *player_url*, fetching and caching on first access.

        Args:
            player_url: Absolute URL of the YouTube player JS bundle.

        Returns:
            JS source string, or ``None`` if the response body was empty.
        """
        if player_url not in self._code_cache:
            log.debug("Fetching player JS: %s", player_url)
            code = ctx.session.http.get(player_url).text
            if code:
                self._code_cache[player_url] = code
                log.debug("Player JS cached (%d chars)", len(code))
            else:
                log.warning("Empty response for player JS URL: %s", player_url)
        return self._code_cache.get(player_url)

    def solve(self, challenge: NChallengeInput) -> NChallengeOutput | None:
        """Solve a single YouTube n-parameter challenge using Deno.

        Fetches (or retrieves from cache) the player JS, runs the bundled
        solver inside a sandboxed Deno subprocess, and validates the result
        before returning it.

        Args:
            challenge: Input containing the player URL and the raw ``n`` token.

        Returns:
            :class:`NChallengeOutput` with the solved token mapping,
            or ``None`` if an error occurs at any stage.
        """
        log.debug("Solving n-challenge token %r via Deno", challenge.token)
        try:
            player = self._get_player(challenge.player_url)
            if not player:
                log.error("Could not retrieve player JS for URL: %s", challenge.player_url)
                return None

            stdin = self._construct_stdin(player, challenge)
            stdout = self.execute(stdin)
            output = json.loads(stdout)

            if output.get("type") == "error":
                raise Exception(f"Solver top-level error: {output['error']}")

            response_data = output["responses"][0]
            if response_data.get("type") == "error":
                raise Exception(
                    f"Solver response error for challenge {challenge!r}: {response_data['error']}"
                )

            response = NChallengeOutput(response_data["data"])
            log.debug("Raw solver response: %s", response)

            if (validation_msg := self.validate_response(response, challenge)) is not True:
                log.warning("Invalid n-challenge response from Deno: %s", validation_msg)

            return response

        except Exception as exc:
            log.error("n-challenge solving failed for token %r: %s", challenge.token, exc)
            if 'The system cannot find the file specified' in str(exc):
                raise Exception("Deno not found. "
                                "Please install Deno from https://deno.land/manual/getting_started/installation")
            return NChallengeOutput(results={})


def _get_data_from_regex(res: Response, regex, descr: str):
    """Search *res.text* with *regex* and parse the first capture group as JSON.

    Args:
        res:   HTTP response whose text body is searched.
        regex: Compiled or string pattern with one capture group.
        descr: Human-readable label used in the debug log when no match is found.

    Returns:
        Parsed JSON object, or ``None`` if the pattern did not match.
    """
    if match := re.search(regex, res.text):
        return parse_json(match.group(1))
    log.debug("Pattern not found in response body: %s", descr)


class StreamsExtractor:
    """Resolves a YouTube ``/streams`` page to a watch URL.

    Fetches the channel streams page, extracts ``ytInitialData``, filters
    active (non-upcoming) streams, and redirects to the selected video.
    """

    valid_url_re = r"https://www\.youtube\.com/(?:@|c(?:hannel)?/|user/)?(?P<id>[^/?\\#&]+)/streams"
    extractor_type: ExtractorType = ExtractorType.LIVE

    @staticmethod
    def _get_initial_data(url) -> dict:
        """Fetch and return ``ytInitialData`` from *url*, retrying up to 3 times.

        Args:
            url: Channel streams page URL.

        Returns:
            Parsed ``ytInitialData`` dict, or ``{}`` if all attempts fail.
        """
        for attempt in range(1, 4):
            try:
                log.debug("Fetching ytInitialData (attempt %d): %s", attempt, url)
                webpage = ctx.session.http.get(url)
                return _get_data_from_regex(webpage, _re_ytInitialData, "ytInitialData")
            except Exception as exc:
                log.error("Error fetching ytInitialData (attempt %d): %s", attempt, exc)
        log.warning("All attempts to fetch ytInitialData failed for: %s", url)
        return {}

    @staticmethod
    def _schema_tab_data(data) -> dict | None:
        """Extract the contents list from the currently selected tab.

        Navigates ``ytInitialData`` to find the active ``richGridRenderer``
        tab and returns its ``contents`` list.

        Args:
            data: Parsed ``ytInitialData`` dict.

        Returns:
            The ``contents`` list of the selected tab, or ``None`` if not found.
        """
        return validate.Schema(
            {"contents": {"twoColumnBrowseResultsRenderer": {"tabs": list}}},
            validate.get(("contents", "twoColumnBrowseResultsRenderer", "tabs")),
            validate.filter(lambda tab: (
                tab.get("tabRenderer", {}).get("selected")
                and tab.get("tabRenderer", {}).get("content", {}).get("richGridRenderer", {}).get("contents")
            )),
            validate.get((0, "tabRenderer", "content", "richGridRenderer", "contents")),
        ).validate(data)

    @staticmethod
    def _schema_active_streams(data) -> list[tuple] | None:
        """Extract active (non-upcoming) video IDs and their viewer count runs.

        Skips non-video items (e.g. ``continuationItemRenderer``) and videos
        with ``upcomingEventData`` (scheduled but not yet live).

        Args:
            data: Raw ``richGridRenderer.contents`` list from ``ytInitialData``.

        Returns:
            List of ``(videoId, runs)`` tuples, where ``runs`` is the ``viewCountText.runs`` list.
        """
        return validate.Schema(
            [
                validate.any(
                    validate.all(
                        {"richItemRenderer": {"content": {"videoRenderer": dict}}},
                        validate.get(("richItemRenderer", "content", "videoRenderer")),
                    ),
                    validate.transform(lambda _: None),
                )
            ],
            validate.filter(lambda v: v is not None),
            # Keep only active streams: must have a viewer count and not be scheduled
            validate.filter(lambda v: v.get("viewCountText", {}).get("runs") and not v.get("upcomingEventData")),
            validate.map(lambda v: (v["videoId"], v["viewCountText"]["runs"])),
        ).validate(data)

    @staticmethod
    def _pick_stream(active_streams) -> str:
        """Select a video ID from *active_streams* according to the ``stream`` plugin option.

        Args:
            active_streams: List of ``(videoId, runs)`` tuples as returned by
                :meth:`_schema_active_streams`.

        Returns:
            The selected video ID string.
        """
        stream_pick = StreamSelection(ctx.options.get("stream")).value
        log.debug("Stream pick option: %r, %d candidate(s)", stream_pick, len(active_streams))

        if isinstance(stream_pick, int):
            # Clamp to last if position exceeds available streams
            index = min(stream_pick - 1, len(active_streams) - 1)
            video_id = active_streams[index][0]
        elif stream_pick == StreamPick.FIRST:
            video_id = active_streams[0][0]
        elif stream_pick == StreamPick.LAST:
            video_id = active_streams[-1][0]
        else:
            # StreamPick.POPULAR: rank by viewer count, pick the highest
            # Clean /runs/.../text from non-digits and get first number for every stream
            ranked = [
                (vid, int(re.sub(r"\D", "", next(r["text"] for r in runs if re.search(r"\d", r["text"])))))
                for vid, runs in active_streams
            ]
            video_id = max(ranked, key=lambda x: x[1])[0]

        log.debug("Selected video ID: %s", video_id)
        return video_id

    def extract(self, url: str) -> ExtractorResult:
        """Extract the selected live video ID from a ``/streams`` page and redirect.

        Args:
            url: YouTube channel streams URL.

        Returns:
            :class:`ExtractorResult` with ``next`` set to a
            :class:`NextExtractor` pointing at the watch URL.
        """
        initial = self._get_initial_data(url)
        tab_data = self._schema_tab_data(initial)
        active_streams = self._schema_active_streams(tab_data)
        log.debug("Active streams found: %d", len(active_streams) if active_streams else 0)
        video_id = self._pick_stream(active_streams)
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        log.debug("Redirecting to: %s", watch_url)
        return ExtractorResult(
            next=NextExtractor(
                extractor=ExtractorType.VIDEO,
                url=watch_url,
            )
        )


class LiveExtractor:
    """Resolves a YouTube channel/live URL to a watch URL.

    Fetches the channel page, extracts ``ytInitialData``, and returns a
    :class:`NextExtractor` redirect pointing at the live video.
    """

    valid_url_re = r"https://www\.youtube\.com/(?:@|c(?:hannel)?/|user/)?(?P<id>[^/?\\#&]+)/live"
    extractor_type: ExtractorType = ExtractorType.LIVE

    @staticmethod
    def _get_initial_data(url) -> dict:
        """Fetch and return ``ytInitialData`` from *url*, retrying up to 3 times.

        YouTube occasionally returns a page without ``currentVideoEndpoint``,
        so each attempt checks for that key before accepting the result.

        Args:
            url: Channel live page URL.

        Returns:
            Parsed ``ytInitialData`` dict, or ``{}`` if all attempts fail.
        """
        for attempt in range(1, 4):
            try:
                log.debug("Fetching ytInitialData (attempt %d): %s", attempt, url)
                webpage = ctx.session.http.get(url)
                initial = _get_data_from_regex(webpage, _re_ytInitialData, "ytInitialData")
                if initial and initial.get("currentVideoEndpoint"):
                    log.debug("ytInitialData obtained on attempt %d", attempt)
                    return initial
                log.debug("ytInitialData missing currentVideoEndpoint on attempt %d", attempt)
            except Exception as exc:
                log.error("Error fetching ytInitialData (attempt %d): %s", attempt, exc)
        log.warning("All attempts to fetch ytInitialData failed for: %s", url)
        return {}

    @staticmethod
    def _schema_video_id(data) -> str | None:
        """Extract the live video ID from a parsed ``ytInitialData`` object.

        Args:
            data: Parsed ``ytInitialData`` dict.

        Returns:
            Video ID string, or ``None`` if the expected keys are absent.
        """
        return validate.Schema(
            {
                "currentVideoEndpoint": {
                    "watchEndpoint": {"videoId": str},
                },
            },
            validate.get(("currentVideoEndpoint", "watchEndpoint", "videoId")),
        ).validate(data)

    def extract(self, url: str) -> ExtractorResult:
        """Extract the live video ID from a channel/live page and redirect.

        Args:
            url: YouTube channel/live URL (any supported variant).

        Returns:
            :class:`ExtractorResult` with ``next`` set to a
            :class:`NextExtractor` pointing at the watch URL.

        Raises:
            ValueError: If no video ID could be found after all retries.
        """
        url = urlunparse(urlparse(url)._replace(netloc="www.youtube.com"))
        log.debug("TabExtractor.extract: normalised URL -> %s", url)
        initial = self._get_initial_data(url)
        if video_id := self._schema_video_id(initial):
            watch_url = f"https://www.youtube.com/watch?v={video_id}"
            log.debug("Resolved video ID %s, redirecting to %s", video_id, watch_url)
            return ExtractorResult(
                next=NextExtractor(
                    extractor=ExtractorType.VIDEO,
                    url=watch_url
                )
            )
        raise ValueError("Unable to extract video ID from /live page")


class VideoExtractor:
    """Extracts HLS manifest URLs from a YouTube watch page.

    Queries the ``/player`` endpoint for each configured client,
    collects ``hlsManifestUrl`` values, and solves any ``n``-parameter
    challenges via the injected :class:`JsSolver`.
    """

    valid_url_re = r"https://www\.youtube\.com/watch\?v=(?P<id>[0-9A-Za-z_-]{11})"
    extractor_type = ExtractorType.VIDEO

    _re_ytcfg = re.compile(r"ytcfg\.set\s*\(\s*({.+?})\s*\)\s*;")
    _re_ytInitialPlayerResponse = re.compile(r"var\s+ytInitialPlayerResponse\s*=\s*({.+?});\s*(?:var|</script>)")

    video_id: str = None

    def _get_webpage_data(self, url: str) -> dict:
        """Fetch the watch page and return the parsed ``ytcfg`` object.

        Sets the session ``User-Agent`` to the ``web_safari`` value so that
        YouTube serves the standard desktop page layout.

        Args:
            url: Full watch URL including the ``v=`` parameter.

        Returns:
            Parsed ``ytcfg`` dict extracted from the page source.
        """
        log.debug("Fetching watch page: %s", url)
        ctx.session.http.headers["User-Agent"] = CLIENTS["web_safari"]["INNERTUBE_CONTEXT"]["client"]["userAgent"]
        webpage = ctx.session.http.get(url, params={"bpctr": "9999999999", "has_verified": "1"})
        return _get_data_from_regex(webpage, self._re_ytcfg, "ytcfg")

    @staticmethod
    def _build_headers(client_config: dict, visitor_data: str, client_context: dict) -> dict:
        """Assemble HTTP headers for an InnerTube ``/player`` request.

        Falls back to the static client config when the live page context does
        not carry ``clientVersion`` or ``userAgent``.

        Args:
            client_config:  Entry from :data:`CLIENTS` for the current client.
            visitor_data:   ``X-Goog-Visitor-Id`` token extracted from ytcfg.
            client_context: ``client`` sub-dict from the resolved InnerTube context.

        Returns:
            Dict of HTTP headers with ``None`` values removed.
        """
        default_client = client_config.get("INNERTUBE_CONTEXT", {}).get("client", {})
        client_version = client_context.get("clientVersion") or default_client.get("clientVersion")
        ua = client_context.get("userAgent") or default_client.get("userAgent")
        return {
            k: v for k, v in {
                "X-YouTube-Client-Name": str(client_config.get("INNERTUBE_CONTEXT_CLIENT_NAME")),
                "X-YouTube-Client-Version": client_version,
                "Origin": "https://www.youtube.com",
                "X-Goog-Visitor-Id": visitor_data,
                "User-Agent": ua,
                "content-type": "application/json",
            }.items() if v is not None
        }

    def _build_player_request_payload(self, client_context: dict, webpage_ytcfg: dict) -> dict:
        """Build the JSON body for ``/player`` POST request.

        Args:
            client_context: ``client`` sub-dict from the resolved context.
            webpage_ytcfg:  Parsed ``ytcfg`` from the webpage.

        Returns:
            Dict ready to be serialized as the POST body.
        """
        return {
            "context": client_context,
            "videoId": self.video_id,
            "playbackContext": {
                "contentPlaybackContext": {
                    "html5Preference": "HTML5_PREF_WANTS",
                    **({"signatureTimestamp": sts} if (sts := webpage_ytcfg.get("STS")) else {}),
                },
            },
            "contentCheckOk": True,
            "racyCheckOk": True,
        }

    def _extract_player_response(self, client: str, webpage_ytcfg: dict, visitor_data: str) -> dict:
        """Call the ``/player`` endpoint for *client* and return the response.

        Prefers the context embedded in *webpage_ytcfg* over default values
        then overrides locale/timezone fields to ensure
        consistent responses.

        Args:
            client:        Key into :data:`CLIENTS` (e.g. ``"web_safari"``).
            webpage_ytcfg: Parsed ``ytcfg`` from the watch page.
            visitor_data:  Visitor token for the ``X-Goog-Visitor-Id`` header.

        Returns:
            Parsed JSON response from the ``/player`` endpoint.
        """

        # Prefer live page context; fall back to static client config.
        client_config = CLIENTS[client].copy()
        context = webpage_ytcfg.get("INNERTUBE_CONTEXT") or client_config.get("INNERTUBE_CONTEXT", {})
        client_context = context.get("client", {})
        client_context.update({"hl": "en", "timeZone": "UTC", "utcOffsetMinutes": 0})

        headers = self._build_headers(client_config, visitor_data, client_context)
        payload = self._build_player_request_payload(context, webpage_ytcfg)
        response = ctx.session.http.post(
            "https://www.youtube.com/youtubei/v1/player",
            params={"prettyPrint": "false"},
            headers=headers,
            data=json.dumps(payload).encode("utf-8"),
        )
        return response.json()

    @staticmethod
    def _schema_visitor_data(data) -> str:
        """Extract the visitor-data token from a parsed ``ytcfg`` object.

        Accepts either the top-level ``VISITOR_DATA`` key or the nested
        ``INNERTUBE_CONTEXT.client.visitorData`` path.

        Args:
            data: Parsed ``ytcfg`` dict.

        Returns:
            Visitor-data string.
        """
        return validate.Schema(
            validate.any(
                validate.all({"VISITOR_DATA": str}, validate.get("VISITOR_DATA")),
                validate.all(
                    {"INNERTUBE_CONTEXT": {"client": {"visitorData": str}}},
                    validate.get(("INNERTUBE_CONTEXT", "client", "visitorData")),
                ),
            ),
        ).validate(data)

    @staticmethod
    def _schema_player_url(data) -> str:
        """Extract and normalize the JS player URL from a parsed ``ytcfg`` object.

        Accepts either ``PLAYER_JS_URL`` or the first ``jsUrl`` found inside
        ``WEB_PLAYER_CONTEXT_CONFIGS``, and prepends the YouTube origin when
        the URL is relative.

        Args:
            data: Parsed ``ytcfg`` dict.

        Returns:
            Absolute player JS URL.
        """
        return validate.Schema(
            validate.any(
                validate.all({"PLAYER_JS_URL": str}, validate.get("PLAYER_JS_URL")),
                validate.all(
                    {"WEB_PLAYER_CONTEXT_CONFIGS": {str: {"jsUrl": str}}},
                    validate.get("WEB_PLAYER_CONTEXT_CONFIGS"),
                    validate.transform(lambda x: next(iter(x.values()))),
                    validate.get("jsUrl"),
                ),
            ),
            validate.transform(
                lambda url: url if url.startswith("https://www.youtube.com")
                else f"https://www.youtube.com{url}"
            ),
        ).validate(data)

    def _extract_player_responses(self, webpage_ytcfg: dict) -> tuple[list[dict], str]:
        """Query all configured clients and collect valid player responses.

        Iterates `CLIENTS` in reverse insertion order (``android_vr`` first)
        so that the client most likely to return unthrottled streams is tried first.

        Args:
            webpage_ytcfg: Parsed ``ytcfg`` from the watch page.

        Returns:
            Tuple of ``(player_responses, player_url)`` where *player_responses*
            is a list of dicts that each contain a ``streamingData`` key.

        Raises:
            ValueError: If no client returns a response with ``streamingData``.
        """
        player_responses = []
        visitor_data = self._schema_visitor_data(webpage_ytcfg)
        player_url = self._schema_player_url(webpage_ytcfg)
        log.debug("Player JS URL: %s", player_url)

        for client in reversed(list(CLIENTS)):
            try:
                response = self._extract_player_response(client, webpage_ytcfg, visitor_data)
            except Exception as exc:
                log.error("Player request failed for client %s: %s", client, exc)
                continue

            if not response:
                log.debug("Empty player response for client %s, skipping", client)
                continue

            if not response.get("streamingData"):
                log.warning("No streamingData in player response for client %s", client)
                continue

            log.debug("Valid player response received for client %s", client)
            player_responses.append(response)

        if not player_responses:
            raise ValueError("Failed to extract any player response with streamingData")

        return player_responses, player_url

    @staticmethod
    def _extract_hls(player_responses: list[dict], player_url: str) -> list[str]:
        """Collect HLS manifest URLs from *player_responses*, solving n-challenges.

        For each manifest URL that contains an ``/n/<token>/`` path segment,
        the token is submitted to the injected `JsSolver`.
        URLs whose challenge cannot be solved are dropped with a warning.

        Args:
            player_responses: List of InnerTube player response dicts.
            player_url:       Absolute URL of the player JS bundle used by the solvers.

        Returns:
            List of HLS manifest URLs.
        """
        hls_list = []
        deno_solver = DenoSolver()
        for response in player_responses:
            streaming_data = response.get("streamingData")
            if not streaming_data:
                log.debug("Skipping player response with no streamingData")
                continue

            hls_manifest_url = streaming_data.get("hlsManifestUrl")
            if not hls_manifest_url:
                log.debug("No hlsManifestUrl in streamingData: %s", streaming_data)
                continue

            # Solve the n-parameter challenge embedded in the manifest path.
            if n_matches := re.findall(r"/n/([^/]+)/", urlparse(hls_manifest_url).path):
                n_token = n_matches[0]
                log.debug("Solving n-challenge token: %s", n_token)
                result = deno_solver.solve(NChallengeInput(token=n_token, player_url=player_url))

                if result and (solved := result.results.get(n_token)):
                    hls_manifest_url = hls_manifest_url.replace(f"/n/{n_token}", f"/n/{solved}")
                    log.debug("n-challenge solved: %s -> %s", n_token, solved)
                    hls_list.append(hls_manifest_url)
                else:
                    log.warning("Failed to solve n-challenge token: %s", n_token)
            else:
                hls_list.append(hls_manifest_url)

        log.debug("Collected %d HLS manifest URL(s)", len(hls_list))
        return hls_list

    def extract(self, url: str) -> ExtractorResult:
        """Extract HLS manifest URLs from a YouTube watch URL, retrying up to 3 times.

        Args:
            url: Full ``/watch?v=<id>`` URL.

        Returns:
            :class:`ExtractorResult` with ``hls`` set to the list of resolved manifest URLs.
        """
        hls: list[str] = []

        for attempt in range(1, 4):
            time.sleep(1)
            self.video_id = re.search(self.valid_url_re, url).group("id")
            log.debug("VideoExtractor.extract attempt %d — video_id: %s", attempt, self.video_id)

            webpage_ytcfg = self._get_webpage_data(url)

            try:
                player_responses, player_url = self._extract_player_responses(webpage_ytcfg)
            except ValueError as exc:
                log.error("Player response extraction failed (attempt %d): %s", attempt, exc)
                continue

            hls = self._extract_hls(player_responses, player_url)
            if hls:
                log.debug("HLS extraction succeeded")
                break
            log.warning("No HLS URLs on attempt %d, retrying", attempt)

        return ExtractorResult(hls=hls)





@pluginmatcher(
    name="default",
    pattern=re.compile(
        r"https?://(?:\w+\.)?youtube\.com/(?:v/|live/|watch\?(?:.*&)?v=)(?P<video_id>[\w-]{11})",
    ),
)
@pluginmatcher(
    name="channel",
    pattern=re.compile(
        r"https?://(?:\w+\.)?youtube\.com/(?:@|c(?:hannel)?/|user/)?(?P<channel>[^/?]+)(?P<tab>/(?:live|streams))?/?$",
    ),
)
@pluginmatcher(
    name="embed",
    pattern=re.compile(
        r"https?://(?:\w+\.)?youtube\.com/embed/(?:live_stream\?channel=(?P<live>[^/?&]+)|(?P<video_id>[\w-]{11}))",
    ),
)
@pluginmatcher(
    name="shorthand",
    pattern=re.compile(
        r"https?://youtu\.be/(?P<video_id>[\w-]{11})",
    ),
)
@pluginargument(
    "stream",
    default="popular",
    metavar="first|last|popular|N",
    help="""
        Select which stream to play when opening a channel page or /streams listing.
        Can be `first`, `last`, `popular`, or a position number of the active stream (e.g. `1`, `2`, `3`).
        Defaults to `popular`.
    """,
)
class YouTube(Plugin):
    """Streamlink plugin for YouTube. Resolves live and VOD streams via an extractor chain."""

    _EXTRACTORS: list[type[Extractor]] = [VideoExtractor, LiveExtractor, StreamsExtractor]

    _url_canonical: str = "https://www.youtube.com/watch?v={video_id}"
    _url_channelid_live: str = "https://www.youtube.com/channel/{channel_id}/live"

    @pluginmatcher(
        name="default",
        pattern=re.compile(
            r"https?://(?:\w+\.)?youtube\.com/(?:v/|live/|watch\?(?:.*&)?v=)(?P<video_id>[\w-]{11})",
        ),
    )
    @pluginmatcher(
        name="channel",
        pattern=re.compile(
            r"https?://(?:\w+\.)?youtube\.com/(?:@|c(?:hannel)?/|user/)?(?P<channel>[^/?]+)(?P<live>/live)?/?$",
        ),
    )
    @pluginmatcher(
        name="embed",
        pattern=re.compile(
            r"https?://(?:\w+\.)?youtube\.com/embed/(?:live_stream\?channel=(?P<live>[^/?&]+)|(?P<video_id>[\w-]{11}))",
        ),
    )
    @pluginmatcher(
        name="shorthand",
        pattern=re.compile(
            r"https?://youtu\.be/(?P<video_id>[\w-]{11})",
        ),
    )
    class YouTubeFallback(Plugin):
        _re_ytInitialData = re.compile(r"""var\s+ytInitialData\s*=\s*({.*?})\s*;\s*</script>""", re.DOTALL)
        _re_ytInitialPlayerResponse = re.compile(r"""var\s+ytInitialPlayerResponse\s*=\s*({.*?});\s*var\s+\w+\s*=""", re.DOTALL)

        _url_canonical = "https://www.youtube.com/watch?v={video_id}"
        _url_channelid_live = "https://www.youtube.com/channel/{channel_id}/live"

        # There are missing itags
        adp_video = {
            137: "1080p",
            299: "1080p60",  # HFR
            264: "1440p",
            308: "1440p60",  # HFR
            266: "2160p",
            315: "2160p60",  # HFR
            138: "2160p",
            302: "720p60",  # HFR
            135: "480p",
            133: "240p",
            160: "144p",
        }
        adp_audio = {
            140: 128,
            141: 256,
            171: 128,
            249: 48,
            250: 64,
            251: 160,
            256: 256,
            258: 258,
        }

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            parsed = urlparse(self.url)

            # translate input URLs to be able to find embedded data and to avoid unnecessary HTTP redirects
            if parsed.netloc == "gaming.youtube.com":
                self.url = urlunparse(parsed._replace(scheme="https", netloc="www.youtube.com"))
            elif self.matches["shorthand"]:
                self.url = self._url_canonical.format(video_id=self.match["video_id"])
            elif self.matches["embed"] and self.match["video_id"]:
                self.url = self._url_canonical.format(video_id=self.match["video_id"])
            elif self.matches["embed"] and self.match["live"]:
                self.url = self._url_channelid_live.format(channel_id=self.match["live"])
            elif parsed.scheme != "https":
                self.url = urlunparse(parsed._replace(scheme="https"))

            self.session.http.headers.update({"User-Agent": useragents.CHROME})

        @classmethod
        def stream_weight(cls, stream: str) -> tuple[float, str]:
            match_3d = re.match(r"(\w+)_3d", stream)
            match_hfr = re.match(r"(\d+p)(\d+)", stream)
            if match_3d:
                weight, group = Plugin.stream_weight(match_3d.group(1))
                weight -= 1
                group = "youtube_3d"
            elif match_hfr:
                weight, group = Plugin.stream_weight(match_hfr.group(1))
                weight += 1
                group = "high_frame_rate"
            else:
                weight, group = Plugin.stream_weight(stream)

            return weight, group

        @staticmethod
        def _schema_consent(data):
            schema_consent = validate.Schema(
                validate.parse_html(),
                validate.any(
                    validate.xml_find(".//form[@action='https://consent.youtube.com/s']"),
                    validate.all(
                        validate.xml_xpath(".//form[@action='https://consent.youtube.com/save']"),
                        validate.filter(lambda elem: elem.xpath(".//input[@type='hidden'][@name='set_ytc'][@value='true']")),
                        validate.get(0),
                    ),
                ),
                validate.union((
                    validate.get("action"),
                    validate.xml_xpath(".//input[@type='hidden']"),
                )),
            )
            return schema_consent.validate(data)

        def _schema_canonical(self, data):
            schema_canonical = validate.Schema(
                validate.parse_html(),
                validate.xml_xpath_string(".//link[@rel='canonical'][1]/@href"),
                validate.regex(self.matchers["default"].pattern),
                validate.get("video_id"),
            )
            return schema_canonical.validate(data)

        @classmethod
        def _schema_playabilitystatus(cls, data):
            schema = validate.Schema(
                {
                    "playabilityStatus": {
                        "status": str,
                        validate.optional("reason"): validate.any(str, None),
                    },
                },
                validate.get("playabilityStatus"),
                validate.union_get("status", "reason"),
            )
            return schema.validate(data)

        @classmethod
        def _schema_videodetails(cls, data):
            schema = validate.Schema(
                {
                    "videoDetails": {
                        "videoId": str,
                        "author": str,
                        "title": str,
                        validate.optional("isLive"): validate.transform(bool),
                        validate.optional("isLiveContent"): validate.transform(bool),
                        validate.optional("isLiveDvrEnabled"): validate.transform(bool),
                        validate.optional("isLowLatencyLiveStream"): validate.transform(bool),
                        validate.optional("isPrivate"): validate.transform(bool),
                    },
                    "microformat": validate.all(
                        validate.any(
                            validate.all(
                                {"playerMicroformatRenderer": dict},
                                validate.get("playerMicroformatRenderer"),
                            ),
                            validate.all(
                                {"microformatDataRenderer": dict},
                                validate.get("microformatDataRenderer"),
                            ),
                        ),
                        {
                            "category": str,
                        },
                    ),
                },
                validate.union_get(
                    ("videoDetails", "videoId"),
                    ("videoDetails", "author"),
                    ("microformat", "category"),
                    ("videoDetails", "title"),
                    ("videoDetails", "isLive"),
                ),
            )
            videoDetails = schema.validate(data)
            log.trace("videoDetails = %r", videoDetails)
            return videoDetails

        @classmethod
        def _schema_streamingdata(cls, data):
            schema = validate.Schema(
                {
                    "streamingData": {
                        validate.optional("hlsManifestUrl"): str,
                        validate.optional("formats"): [
                            validate.all(
                                {
                                    "itag": int,
                                    "qualityLabel": str,
                                    validate.optional("url"): validate.url(scheme="http"),
                                },
                                validate.union_get("url", "qualityLabel"),
                            ),
                        ],
                        validate.optional("adaptiveFormats"): [
                            validate.all(
                                {
                                    "itag": int,
                                    "mimeType": validate.all(
                                        str,
                                        validate.regex(
                                            re.compile(r"""^(?P<type>\w+)/(?P<container>\w+); codecs="(?P<codecs>.+)"$"""),
                                        ),
                                        validate.union_get("type", "codecs"),
                                    ),
                                    validate.optional("url"): validate.url(scheme="http"),
                                    validate.optional("qualityLabel"): str,
                                },
                                validate.union_get("url", "qualityLabel", "itag", "mimeType"),
                            ),
                        ],
                    },
                },
                validate.get("streamingData"),
                validate.union_get("hlsManifestUrl", "formats", "adaptiveFormats"),
            )
            hls_manifest, formats, adaptive_formats = schema.validate(data)
            return hls_manifest, formats or [], adaptive_formats or []

        def _create_adaptive_streams(self, adaptive_formats):
            streams = {}
            adaptive_streams = {}
            audio_streams = {}
            best_audio_itag = None

            # Extract audio streams from the adaptive format list
            for url, _label, itag, mimeType in adaptive_formats:
                if url is None:
                    continue

                # extract any high quality streams only available in adaptive formats
                adaptive_streams[itag] = url
                stream_type, stream_codec = mimeType
                stream_codec = re.sub(r"^(\w+).*$", r"\1", stream_codec)

                if stream_type == "audio" and itag in self.adp_audio:
                    audio_bitrate = self.adp_audio[itag]
                    if stream_codec not in audio_streams or audio_bitrate > self.adp_audio[audio_streams[stream_codec]]:
                        audio_streams[stream_codec] = itag

                    # find the best quality audio stream m4a, opus or vorbis
                    if best_audio_itag is None or audio_bitrate > self.adp_audio[best_audio_itag]:
                        best_audio_itag = itag

            if (
                not best_audio_itag
                or self.session.http.head(adaptive_streams[best_audio_itag], raise_for_status=False).status_code >= 400
            ):
                return {}

            streams.update({
                f"audio_{stream_codec}": HTTPStream(self.session, adaptive_streams[itag])
                for stream_codec, itag in audio_streams.items()
            })

            if best_audio_itag and adaptive_streams and MuxedStream.is_usable(self.session):
                aurl = adaptive_streams[best_audio_itag]
                for itag, name in self.adp_video.items():
                    if itag not in adaptive_streams:
                        continue
                    vurl = adaptive_streams[itag]
                    log.debug(f"MuxedStream: v {itag} a {best_audio_itag} = {name}")
                    streams[name] = MuxedStream(
                        self.session,
                        HTTPStream(self.session, vurl),
                        HTTPStream(self.session, aurl),
                    )

            return streams

        def _get_res(self, url):
            res = self.session.http.get(url)
            if urlparse(res.url).netloc == "consent.youtube.com":
                target, elems = self._schema_consent(res.text)
                c_data = {
                    elem.attrib.get("name"): elem.attrib.get("value")
                    for elem in elems
                }  # fmt: skip
                log.debug(f"consent target: {target}")
                log.debug(f"consent data: {', '.join(c_data.keys())}")
                res = self.session.http.post(target, data=c_data)
            return res

        @staticmethod
        def _get_data_from_regex(res, regex, descr):
            match = re.search(regex, res.text)
            if not match:
                log.debug(f"Missing {descr}")
                return
            return parse_json(match.group(1))

        def _get_data_from_api(self, res):
            try:
                video_id = self.match["video_id"]
            except IndexError:
                video_id = None

            if video_id is None:
                try:
                    video_id = self._schema_canonical(res.text)
                except (PluginError, TypeError):
                    return

            if m := re.search(r"""(?P<q1>["'])INNERTUBE_API_KEY(?P=q1)\s*:\s*(?P<q2>["'])(?P<data>.+?)(?P=q2)""", res.text):
                api_key = m.group("data")
            else:
                api_key = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"

            return self.session.http.post(
                "https://www.youtube.com/youtubei/v1/player",
                headers={"Content-Type": "application/json"},
                params={"key": api_key},
                json={
                    "videoId": video_id,
                    "contentCheckOk": True,
                    "racyCheckOk": True,
                    "context": {
                        "client": {
                            "clientName": "ANDROID",
                            "clientVersion": "21.08.266",
                            "platform": "DESKTOP",
                            "clientScreen": "EMBED",
                            "clientFormFactor": "UNKNOWN_FORM_FACTOR",
                            "browserName": "Chrome",
                        },
                        "user": {"lockedSafetyMode": "false"},
                        "request": {"useSsl": "true"},
                    },
                },
                schema=validate.Schema(
                    validate.parse_json(),
                ),
            )

        @staticmethod
        def _data_video_id(data):
            if not data:
                return None
            for key in ("videoRenderer", "gridVideoRenderer"):
                for videoRenderer in search_dict(data, key):
                    videoId = videoRenderer.get("videoId")
                    if videoId is not None:
                        return videoId

        def _get_streams(self):
            res = self._get_res(self.url)

            if self.matches["channel"] and not self.match["live"]:
                initial = self._get_data_from_regex(res, self._re_ytInitialData, "initial data")
                video_id = self._data_video_id(initial)
                if video_id is None:
                    log.error("Could not find videoId on channel page")
                    return
                self.url = self._url_canonical.format(video_id=video_id)
                res = self._get_res(self.url)

            # TODO: clean up the validation schemas and how they're applied

            if not (data := self._get_data_from_api(res)):
                return
            status, reason = self._schema_playabilitystatus(data)
            # assume that there's an error if reason is set (status will still be "OK" for some reason)
            if status != "OK" or reason:
                log.error(f"Could not get video info - {status}: {reason}")
                return

            # the initial player response contains the category data, which the API response does not
            init_player_response = self._get_data_from_regex(res, self._re_ytInitialPlayerResponse, "initial player response")
            self.id, self.author, self.category, self.title, is_live = self._schema_videodetails(init_player_response)
            log.debug(f"Using video ID: {self.id}")

            if is_live:
                log.debug("This video is live.")

            # TODO: remove parsing of non-HLS stuff, as we don't support this
            streams = {}
            hls_manifest, formats, adaptive_formats = self._schema_streamingdata(data)

            protected = any(url is None for url, *_ in formats + adaptive_formats)
            if protected:
                log.debug("This video may be protected.")

            for url, label in formats:
                if url is None:
                    continue
                if self.session.http.head(url, raise_for_status=False).status_code >= 400:
                    break
                streams[label] = HTTPStream(self.session, url)

            if not is_live:
                streams.update(self._create_adaptive_streams(adaptive_formats))

            if hls_manifest:
                streams.update(HLSStream.parse_variant_playlist(self.session, hls_manifest, name_key="pixels"))

            if not streams:
                if protected:
                    raise PluginError("This plugin does not support protected videos, try yt-dlp instead")
                if formats or adaptive_formats:
                    raise PluginError("This plugin does not support VOD content, try yt-dlp instead")

            return streams

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.url = self._normalize_url()
        log.debug(f"Normalized URL: {self.url}")
        ctx.session = self.session
        ctx.options = self.options

    def _normalize_url(self) -> str:
        """Normalize various YouTube URL formats to a canonical HTTPS URL."""
        parsed = urlparse(self.url)

        if parsed.netloc == "gaming.youtube.com":
            return parsed._replace(scheme="https", netloc="www.youtube.com").geturl()
        elif self.matches["shorthand"]:
            return self._url_canonical.format(video_id=self.match["video_id"])
        elif self.matches["embed"] and self.match["video_id"]:
            return self._url_canonical.format(video_id=self.match["video_id"])
        elif self.matches["embed"] and self.match["live"]:
            return self._url_channelid_live.format(channel_id=self.match["live"])
        elif self.matches["channel"] and not self.match["tab"]:
            return self.url.rstrip("/") + "/streams"
        elif parsed.scheme != "https":
            return parsed._replace(scheme="https").geturl()
        return self.url

    def _next_extract(self, prev_result: ExtractorResult = None):
        """Recursively extract stream data through extractor chain.

        YouTube extraction may require multiple steps:
        1. TabExtractor: Channel/live pages -> video URL
        2. VideoExtractor: Video page -> HLS manifest URLs

        Args:
            prev_result: Result from previous extraction step

        Returns:
            list[str]: HLS manifest URLs
        """
        if not prev_result:
            extractor = next((e for e in self._EXTRACTORS if re.match(e.valid_url_re, self.url)), None)
            url = self.url
        elif prev_result.hls:
            log.debug(f"Resolved {len(prev_result.hls)} HLS manifest(s)")
            return prev_result.hls
        else:
            extractor = next((e for e in self._EXTRACTORS if e.extractor_type == prev_result.next.extractor))
            url = prev_result.next.url

        if not extractor:
            log.warning(f"No extractor found for URL: {self.url}")
            return []

        log.debug(f"Chaining to {extractor.__name__} for {url}")
        return self._next_extract(prev_result=extractor().extract(url))

    def _check_streams(self, urls):
        """Parse and probe HLS variant playlists, discarding unreachable streams.

        Args:
            urls: HLS manifest URLs to probe

        Returns:
            list[tuple[str, HLSStream]]: Stream quality name to HLS stream object mapping
        """
        streams = []
        for m3u8_url in urls:
            try:
                variant_playlist = HLSStream.parse_variant_playlist(self.session, m3u8_url)
                v = next(iter(variant_playlist.values()))
                with v.open() as fd:
                    fd.timeout = 2
                    fd.read(64)
                streams.extend(variant_playlist.items())
            except Exception as e:
                log.warning(f"Skipping unreachable stream {m3u8_url}: {e}")

        if not streams:
            raise ValueError("No playable streams returned by extractors")
        return streams

    def _get_streams(self):
        """Extract and yield HLS streams from YouTube.

        Yields:
            tuple[str, HLSStream]: Stream quality name and HLS stream object
        """

        try:
            # Extract HLS manifest URLs through extractor chain
            m3u8_urls = self._next_extract()
            yield from self._check_streams(m3u8_urls)

        except Exception as e:
            log.error(f"Extraction failed: {e}")
            log.info("Falling back to original YouTube plugin")
            self.url = self.url.removesuffix("/streams")
            yield from self.YouTubeFallback(self.session, self.url, self.options)._get_streams().items()


__plugin__ = YouTube
