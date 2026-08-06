import os
import logging
import edge_tts

logger = logging.getLogger(__name__)

# Constant mapping for Khmer voices
VOICE_MAP = {
    "male": "km-KH-PisethNeural",
    "female": "km-KH-SreymomNeural",
}

async def generate_speech(text: str, voice_gender: str, output_path: str) -> bool:
    """
    Asynchronously generate speech from text using Microsoft Edge TTS.
    
    Args:
        text (str): The text to be converted to speech.
        voice_gender (str): The preferred voice gender ("male" or "female").
        output_path (str): The full absolute or relative path to save the .mp3 file.
        
    Returns:
        bool: True if the file was generated successfully and size > 0, False otherwise.
    """
    # Edge Case 1: Empty text input
    if not text or not text.strip():
        logger.warning("TTS failed: Empty text input provided.")
        return False
        
    # Edge Case 2: Text length exceeds 3000 characters
    if len(text) > 3000:
        logger.warning(f"TTS failed: Text input too long ({len(text)} chars). Limit is 3000.")
        return False

    # Resolve voice, fallback to "female" if the provided gender is not in map
    voice = VOICE_MAP.get(voice_gender, VOICE_MAP["female"])

    try:
        # Edge TTS Communicate object handles the async text-to-speech generation
        communicate = edge_tts.Communicate(text, voice)
        
        # Await the saving process (native async, non-blocking I/O)
        await communicate.save(output_path)
        
        # Verify the file was created and is not empty
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return True
        else:
            logger.error(f"TTS failed: File saved but is missing or empty at {output_path}")
            return False

    except Exception as e:
        # Edge Case 3: Network/API timeout or other exceptions
        logger.error(f"TTS failed due to network/API error: {e}", exc_info=True)
        return False
