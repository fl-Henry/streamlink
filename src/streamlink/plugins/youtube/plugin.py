import re
from urllib.parse import urlparse, urlunparse

from streamlink.plugin import Plugin, pluginmatcher
from streamlink.stream import HLSStream

from .extractors import VideoExtractor, TabExtractor, Extractor, ExtractorResponse, ctx


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
class Youtube(Plugin):
    _EXTRACTORS: list[type[Extractor]] = [VideoExtractor, TabExtractor]

    _url_canonical = "https://www.youtube.com/watch?v={video_id}"
    _url_channelid_live = "https://www.youtube.com/channel/{channel_id}/live"

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

    def _extract_info(self, prev: ExtractorResponse = None):
        if not prev:
            extractor = next((e for e in self._EXTRACTORS if re.match(e.valid_url_re, self.url)), None)
            url = self.url
        elif prev.hls:
            return prev.hls
        else:
            extractor = next((e for e in self._EXTRACTORS if e.extractor_type == prev.next.extractor))
            url = prev.next.url

        extractor_result = extractor().extract(url)
        return self._extract_info(prev=extractor_result)

    def _get_streams(self):
        """Find a stream URL list and yield it as an iterator.

        :return: tuple[str, HLSStream]
        """

        print("here")
        ctx.session = self.session
        print(f"{id(self.session)=}")
        print(f"{id(ctx.session)=}")

        m3u8_urls = self._extract_info()

        # Yield HLSStream
        for m3u8_url in m3u8_urls.__reversed__():
            yield from HLSStream.parse_variant_playlist(self.session, m3u8_url).items()
