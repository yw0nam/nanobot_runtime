"""TTS ports for nanobot_runtime.

These modules implement the Protocols declared in
``nanobot_runtime.services.hooks.tts`` (SentenceChunker, TextPreprocessor,
EmotionMapper, TTSSynthesizer) using logic ported from DesktopMatePlus.
"""

from nanobot_runtime.services.tts.chunker import SentenceChunker as SentenceChunker
from nanobot_runtime.services.tts.emotion_mapper import EmotionMapper as EmotionMapper
from nanobot_runtime.clients.irodori import IrodoriClient as IrodoriClient
from nanobot_runtime.services.tts.preprocessor import Preprocessor as Preprocessor
