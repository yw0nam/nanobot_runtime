"""TTS ports for nanobot_runtime.

These modules implement the Protocols declared in
``nanobot_runtime.hooks.tts`` (SentenceChunker, TextPreprocessor,
EmotionMapper, TTSSynthesizer) using logic ported from DesktopMatePlus.
"""

from nanobot_runtime.tts.chunker import SentenceChunker
from nanobot_runtime.tts.emotion_mapper import EmotionMapper
from nanobot_runtime.tts.irodori import IrodoriClient
from nanobot_runtime.tts.preprocessor import Preprocessor
