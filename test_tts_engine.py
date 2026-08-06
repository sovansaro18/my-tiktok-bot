import asyncio
import logging
import os

# Import matching the exact path of src/tts_engine.py from root
from src.tts_engine import generate_speech

# Set up basic logging to see the output in the console
logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")

async def main() -> None:
    text = "សួស្តី នេះជាការសាកល្បងមុខងារអានអត្ថបទដោយប្រើប្រាស់ Edge TTS។"
    male_output = "test_male.mp3"
    female_output = "test_female.mp3"

    print("--- Testing Male Voice ---")
    male_success = await generate_speech(text, voice_gender="male", output_path=male_output)
    if male_success:
        print(f"✅ Success! Male TTS saved to '{male_output}' (Size: {os.path.getsize(male_output)} bytes)")
    else:
        print("❌ Failed to generate Male TTS.")

    print("\n--- Testing Female Voice ---")
    female_success = await generate_speech(text, voice_gender="female", output_path=female_output)
    if female_success:
        print(f"✅ Success! Female TTS saved to '{female_output}' (Size: {os.path.getsize(female_output)} bytes)")
    else:
        print("❌ Failed to generate Female TTS.")

if __name__ == "__main__":
    asyncio.run(main())
